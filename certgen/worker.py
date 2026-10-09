"""The worker. A recipient row is the queue, so there is no broker and nothing to rebuild.

Per-recipient isolation is structural: each render sits in its own try/except, so a raise on
row 7 is incapable of affecting rows 1-6 or 8-N. The guarantee is a property of the loop,
not of a catch block someone remembered to add.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import Event

from sqlalchemy import select

from .models import Job, Recipient
from .reasons import REASONS, validate
from .render import render_certificate
from .settle import (
    ISSUED,
    PENDING,
    REJECTED,
    counts_from_db,
    settle_row,
    status_of,
)
from .storage import certificate_id_for, commit_certificate

log = logging.getLogger(__name__)


def issued_emails_in_job(session, job_id: str) -> set[str]:
    """Emails already issued in this job, so the first valid occurrence wins."""
    rows = session.scalars(
        select(Recipient.email).where(Recipient.job_id == job_id, Recipient.state == ISSUED)
    )
    return {str(email).strip().lower() for email in rows if email}


def reject(session, recipient: Recipient, code: str, detail: str | None = None) -> None:
    """Settle a row as REJECTED with a code from the closed taxonomy. First settlement wins."""
    state, _ = settle_row((recipient.state, recipient.reason_code), (REJECTED, code))
    if state != REJECTED:
        return
    recipient.state = REJECTED
    recipient.reason_code = code
    recipient.reason_msg = REASONS[code]["msg"]
    recipient.reason_detail = detail


def process_recipient(
    session, recipient: Recipient, storage_dir: Path, issued: set[str]
) -> str | None:
    """Validate, render, and stage one row in the session. Raises to signal RENDER_FAILED.

    Returns the lowercased email it issued, for the caller to record once the commit holds.
    """
    payload = json.loads(recipient.payload)
    rejection = validate(payload, issued)
    if rejection is not None:
        reject(session, recipient, *rejection)
        return None

    pdf_bytes = render_certificate(
        payload,
        certificate_id_for(recipient.job_id, recipient.row_index),
        recipient.job.template_id,
    )
    commit_certificate(session, recipient, pdf_bytes, storage_dir)
    return str(payload.get("email") or "").strip().lower()


def run_once(sessions, storage_dir: Path, batch_size: int = 256) -> int:
    """Sweep one batch of PENDING rows, isolating each. Returns how many rows it attempted."""
    attempted = 0
    touched: set[str] = set()
    with sessions() as session:
        rows = (
            session.scalars(
                select(Recipient)
                .where(Recipient.state == PENDING)
                .order_by(Recipient.job_id, Recipient.row_index)
                .limit(batch_size)
            )
            .all()
        )

        seen: dict[str, set[str]] = {}
        for row in rows:
            attempted += 1
            touched.add(row.job_id)
            try:
                issued = seen.get(row.job_id)
                if issued is None:
                    # Once per job per pass. The set is then kept current in memory.
                    issued = seen[row.job_id] = issued_emails_in_job(session, row.job_id)
                email = process_recipient(session, row, storage_dir, issued)
                session.commit()
                # Only after the commit holds: a failed commit must not leave an email here
                # that would wrongly reject a later twin as DUPLICATE_IN_BATCH.
                if email is not None:
                    issued.add(email)
            except Exception as exc:
                # Isolate: this row is the only thing that fails. Everything else carries on.
                session.rollback()
                failed = session.get(Recipient, row.id)
                if failed is not None:
                    reject(session, failed, "RENDER_FAILED", f"{type(exc).__name__}: {exc}")
                    session.commit()
                log.exception("row %s/%s rejected as RENDER_FAILED", row.job_id, row.row_index)

        _mirror_job_statuses(session, touched)
    return attempted


def worker_loop(sessions, storage_dir: Path, stop: Event, interval: float, batch_size: int) -> None:
    while not stop.is_set():
        try:
            attempted = run_once(sessions, storage_dir, batch_size)
        except Exception:
            # A database-level fault (locked file, disk full) must not kill the thread.
            # Note: fixed-interval retry with no backoff; add backoff and a fault counter
            # if a flaky disk makes this spin hot.
            log.exception("worker pass failed; retrying")
            attempted = 0
        if attempted == 0:
            stop.wait(interval)


def _mirror_job_statuses(session, job_ids: set[str]) -> None:
    """Refresh the denormalised jobs.status mirror for the jobs this pass touched.

    No endpoint reads this column; the status endpoint derives status from recipient rows.
    """
    for job_id in job_ids:
        job = session.get(Job, job_id)
        if job is not None:
            job.status = status_of(counts_from_db(session, job_id))
    session.commit()
