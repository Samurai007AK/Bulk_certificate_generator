"""HTTP surface. Everything a client needs to submit a batch, watch it settle, collect both
ledgers, and repair the rejects back into the same job.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
import threading
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from starlette.background import BackgroundTask
from sqlalchemy.exc import IntegrityError

from .config import Settings
from .db import make_engine, make_session_factory
from .models import Certificate, Job, Recipient, utc_now
from .reasons import REASONS
from .settle import ISSUED, PENDING, REJECTED, counts_from_db, pct_complete, status_of
from .storage import path_for
from .worker import worker_loop


# One request may carry this many recipients. Bigger lists are split client-side; the cap
# keeps one POST body (and its single insert transaction) bounded.
MAX_RECIPIENTS = 10_000


class RecipientIn(BaseModel):
    """One recipient. Shape errors (wrong type, absurd length) are a 422 for the request;
    content errors (blank name, bad email, ...) are a per-row rejection with a reason code."""

    # extra="allow" keeps unknown template fields in the stored payload.
    model_config = ConfigDict(extra="allow")

    name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=320)
    course: str | None = Field(default=None, max_length=160)
    date: str | None = Field(default=None, max_length=32)


class JobIn(BaseModel):
    template_id: str | None = None
    recipients: list[RecipientIn] = Field(min_length=1, max_length=MAX_RECIPIENTS)


class RepairIn(BaseModel):
    row_index: int
    revision: int
    # Validated with the same rules as a submitted recipient; only the fields sent are merged.
    patch: RecipientIn = Field(default_factory=RecipientIn)


class RepairsIn(BaseModel):
    repairs: list[RepairIn] = Field(min_length=1)


def _canonical_hash(template_id: str, recipients: list[dict[str, Any]]) -> str:
    blob = json.dumps(
        {"template_id": template_id, "recipients": recipients},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _rejection_view(row: Recipient) -> dict[str, Any]:
    code = row.reason_code or ""
    spec = REASONS.get(code, {"repairable": False, "points_at": None, "msg": ""})
    return {
        "row_index": row.row_index,
        "name": row.name,
        "email": row.email,
        "reason_code": code or None,
        "reason_msg": row.reason_msg,
        "reason_detail": row.reason_detail,
        "repairable": spec["repairable"],
        "points_at": spec["points_at"],
        "revision": row.revision,
    }


def _csv_cell(value: Any) -> Any:
    """Neutralise spreadsheet formulas: a name like `=HYPERLINK(...)` must stay text."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def create_app(settings: Settings | None = None, *, start_worker: bool = True) -> FastAPI:
    settings = settings or Settings()
    engine = make_engine(settings.database_url)
    sessions = make_session_factory(engine)
    storage_dir = Path(settings.storage_dir)
    storage_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        stop = threading.Event()
        thread = None
        if start_worker:
            thread = threading.Thread(
                target=worker_loop,
                args=(
                    sessions,
                    storage_dir,
                    stop,
                    settings.poll_interval,
                    settings.batch_size,
                ),
                name="certgen-worker",
                daemon=True,
            )
            thread.start()
        try:
            yield
        finally:
            stop.set()
            if thread is not None:
                thread.join(timeout=5)

    app = FastAPI(
        title="Bulk Certificate Generator",
        version="1.0.0",
        summary="Every recipient settles as ISSUED or REJECTED. Nothing is silently skipped.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessions = sessions
    app.state.storage_dir = storage_dir

    def require_job(session, job_id: str) -> Job:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, f"unknown job {job_id}")
        return job

    def derived(session, job_id: str) -> dict[str, Any]:
        counts = counts_from_db(session, job_id)
        return {
            "job_id": job_id,
            # Derived from recipient rows every time. The stored jobs.status column is a
            # mirror for humans reading the table, and is never read here.
            "status": status_of(counts),
            "total": counts.total,
            "issued": counts.issued,
            "rejected": counts.rejected,
            "pending": counts.pending,
            "pct_complete": pct_complete(counts),
        }

    def replay(session, existing: Job, payload_hash: str, response: Response) -> Any:
        if existing.payload_hash != payload_hash:
            raise HTTPException(
                409, "Idempotency-Key was already used with a different request body"
            )
        response.status_code = 200
        return {"job_id": existing.id, "status": derived(session, existing.id)["status"]}

    @app.post("/jobs", status_code=202)
    def create_job(
        body: JobIn,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Any:
        # One predefined template. Naming another is a client error, not a silent fallback.
        if body.template_id not in (None, settings.template_id):
            raise HTTPException(
                422, f"unknown template_id {body.template_id!r}; use {settings.template_id!r}"
            )
        template_id = settings.template_id
        payloads = [r.model_dump(mode="json") for r in body.recipients]
        payload_hash = _canonical_hash(template_id, payloads)

        with sessions() as session:
            if idempotency_key:
                existing = session.scalar(
                    select(Job).where(Job.idempotency_key == idempotency_key)
                )
                if existing is not None:
                    return replay(session, existing, payload_hash, response)

            job = Job(
                id=uuid.uuid4().hex,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                template_id=template_id,
                total=len(payloads),
                status="PENDING",
                created_at=utc_now(),
            )
            session.add(job)
            # One row per submitted recipient, always. A rejection settles a row, it never
            # removes one, so the row count can never silently disagree with the input array.
            session.add_all(
                Recipient(
                    job_id=job.id,
                    row_index=index,
                    email=payload.get("email"),
                    name=payload.get("name"),
                    payload=json.dumps(payload),
                    state=PENDING,
                    revision=0,
                )
                for index, payload in enumerate(payloads)
            )
            try:
                session.commit()
            except IntegrityError:
                # Lost a race on UNIQUE(idempotency_key): the database refused the duplicate
                # POST. Replay the winner instead of creating a second job.
                session.rollback()
                if not idempotency_key:
                    raise
                existing = session.scalar(
                    select(Job).where(Job.idempotency_key == idempotency_key)
                )
                if existing is None:
                    raise
                return replay(session, existing, payload_hash, response)

            response.status_code = 202
            return {"job_id": job.id, "status": derived(session, job.id)["status"]}

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str) -> Any:
        with sessions() as session:
            require_job(session, job_id)
            return derived(session, job_id)

    @app.get("/jobs/{job_id}/results")
    def get_results(job_id: str) -> Any:
        """Both ledgers, keyed by the caller's original row_index."""
        with sessions() as session:
            require_job(session, job_id)
            summary = derived(session, job_id)
            cert_by_recipient = {
                cert.recipient_id: cert.certificate_id
                for cert in session.scalars(
                    select(Certificate).where(Certificate.job_id == job_id)
                ).all()
            }
            rows = session.scalars(
                select(Recipient)
                .where(
                    Recipient.job_id == job_id,
                    Recipient.state.in_((ISSUED, REJECTED)),
                )
                .order_by(Recipient.row_index)
            ).all()

            issued, rejected = [], []
            for row in rows:
                if row.state == ISSUED:
                    certificate_id = cert_by_recipient.get(row.id)
                    issued.append(
                        {
                            "row_index": row.row_index,
                            "name": row.name,
                            "email": row.email,
                            "certificate_id": certificate_id,
                            "certificate_url": f"/certificates/{certificate_id}",
                        }
                    )
                else:
                    rejected.append(_rejection_view(row))
            return {
                "job_id": job_id,
                "summary": summary,
                "issued": issued,
                "rejected": rejected,
            }

    @app.get("/jobs/{job_id}/rejections.csv")
    def get_rejections_csv(job_id: str) -> Any:
        """The reject ledger, with a fix column holding a literal repair body per row."""
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(
            [
                "row_index",
                "name",
                "email",
                "reason_code",
                "reason_msg",
                "reason_detail",
                "repairable",
                "points_at",
                "revision",
            ]
        )
        with sessions() as session:
            require_job(session, job_id)
            rows = session.scalars(
                select(Recipient)
                .where(Recipient.job_id == job_id, Recipient.state == REJECTED)
                .order_by(Recipient.row_index)
            ).all()
            for row in rows:
                view = _rejection_view(row)
                writer.writerow(
                    [
                        _csv_cell(cell)
                        for cell in (
                            view["row_index"],
                            row.name or "",
                            row.email or "",
                            view["reason_code"] or "",
                            view["reason_msg"] or "",
                            view["reason_detail"] or "",
                            view["repairable"],
                            view["points_at"] or "",
                            view["revision"],
                        )
                    ]
                )
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{job_id}-rejections.csv"'
            },
        )

    @app.patch("/jobs/{job_id}/recipients")
    def repair_recipients(job_id: str, body: RepairsIn) -> Any:
        """Batched repair: compare-and-set on (state=REJECTED, revision=N), then re-queue
        through the identical worker path. The dominant real case is many rows in one call."""
        updated, skipped = [], []
        with sessions() as session:
            require_job(session, job_id)
            for repair in body.repairs:
                key = (Recipient.job_id == job_id, Recipient.row_index == repair.row_index)
                payload = session.scalar(select(Recipient.payload).where(*key))
                if payload is None:
                    skipped.append({"row_index": repair.row_index, "reason": "unknown_row"})
                    continue

                merged = {**json.loads(payload), **repair.patch.model_dump(exclude_unset=True)}
                # The compare-and-set is this one conditional UPDATE, so two racing repairs
                # carrying the same revision cannot both win: the database decides.
                won = session.execute(
                    update(Recipient)
                    .where(
                        *key,
                        Recipient.state == REJECTED,
                        Recipient.revision == repair.revision,
                    )
                    .values(
                        payload=json.dumps(merged),
                        name=merged.get("name"),
                        email=merged.get("email"),
                        state=PENDING,
                        reason_code=None,
                        reason_msg=None,
                        reason_detail=None,
                        revision=Recipient.revision + 1,
                    )
                    .execution_options(synchronize_session=False)
                ).rowcount
                if won:
                    updated.append(repair.row_index)
                    continue

                # Lost: say why. Revision first, since it is the client's optimistic lock.
                state, revision = session.execute(
                    select(Recipient.state, Recipient.revision).where(*key)
                ).one()
                if revision != repair.revision:
                    skipped.append(
                        {
                            "row_index": repair.row_index,
                            "reason": "stale_revision",
                            "expected_revision": revision,
                            "state": state,
                        }
                    )
                else:
                    skipped.append(
                        {"row_index": repair.row_index, "reason": "not_rejected", "state": state}
                    )
            session.commit()
        return {"job_id": job_id, "updated": sorted(updated), "skipped": skipped}

    @app.get("/certificates/{certificate_id}")
    def get_certificate(certificate_id: str) -> Any:
        with sessions() as session:
            cert = session.scalar(
                select(Certificate).where(Certificate.certificate_id == certificate_id)
            )
            if cert is None:
                raise HTTPException(404, f"unknown certificate {certificate_id}")
            path = path_for(storage_dir, cert.certificate_id)
            if not path.exists():
                # The ordering rule says this cannot happen. Say so loudly instead of
                # serving an empty 200.
                raise HTTPException(
                    500, f"certificate row exists but its file is missing: {path}"
                )
            return FileResponse(
                path,
                media_type="application/pdf",
                filename=f"{cert.certificate_id}.pdf",
                content_disposition_type="inline",
            )

    @app.get("/jobs/{job_id}/archive.zip")
    def get_archive(job_id: str) -> Any:
        """Every issued certificate for this job in one artifact. Rejects are excluded by
        construction: the zip is built from the certificates table, not from the input."""
        with sessions() as session:
            require_job(session, job_id)
            certs = session.scalars(
                select(Certificate)
                .where(Certificate.job_id == job_id)
                .order_by(Certificate.certificate_id)
            ).all()

        # Built in a temp file and streamed, so a 20,000-certificate archive is not held in
        # memory. PDFs are already compressed, so ZIP_STORED costs little and saves CPU.
        fd, tmp = tempfile.mkstemp(suffix=".zip")
        with os.fdopen(fd, "wb") as fh, zipfile.ZipFile(fh, "w", zipfile.ZIP_STORED) as zf:
            for cert in certs:
                zf.write(
                    path_for(storage_dir, cert.certificate_id),
                    f"{cert.certificate_id}.pdf",
                )
        return FileResponse(
            tmp,
            media_type="application/zip",
            filename=f"{job_id}-archive.zip",
            background=BackgroundTask(os.unlink, tmp),
        )

    @app.get("/healthz")
    def healthz() -> Any:
        return {"ok": True}

    return app
