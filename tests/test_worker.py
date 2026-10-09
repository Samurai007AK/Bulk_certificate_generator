"""Worker behaviour: the ordering rule, per-recipient isolation, resumability."""

import hashlib
import json

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from certgen import storage, worker
from certgen.models import Certificate, Job, Recipient, utc_now
from certgen.render import _fit, render_certificate
from certgen.settle import ISSUED, PENDING, REJECTED
from certgen.storage import path_for
from tests.helpers import db_row_for_certificate, db_state_of, good, pdf_lines, rows_of, submit


def plant(app, recipients: list[dict]) -> str:
    """Insert a job and its rows directly, so the worker can be driven by hand."""
    job_id = "planted-job"
    with app.state.sessions() as session:
        session.add(
            Job(
                id=job_id,
                template_id="course-completion",
                total=len(recipients),
                status="PENDING",
                created_at=utc_now(),
            )
        )
        session.add_all(
            Recipient(
                job_id=job_id,
                row_index=index,
                email=payload.get("email"),
                name=payload.get("name"),
                payload=json.dumps(payload),
                state=PENDING,
                revision=0,
            )
            for index, payload in enumerate(recipients)
        )
        session.commit()
    return job_id


def run_once(app) -> int:
    return worker.run_once(app.state.sessions, app.state.storage_dir)


def test_issue_ordering_is_file_first_then_ledger(env):
    """Render -> fsync -> hash -> only then flip the row. Asserted, not trusted.

    Observed at the seam between "file is durable" and "row is updated", on a connection
    that is not the worker's own.
    """
    observed: list[tuple[str, str]] = []
    real = storage.digest_of

    def spy(path, data):
        found = db_row_for_certificate(env.db_path, path.stem)
        assert found is not None, f"{path.stem}: no recipient row"
        recipient_id, state = found
        assert path.exists(), "the hash ran before the file was written"
        assert path.read_bytes().startswith(b"%PDF"), "the file on disk is not a PDF"
        assert state == "PENDING", f"{path.stem}: the row was already {state}"
        assert path.read_bytes() == data, "the hashed bytes are not the bytes on disk"
        digest = real(path, data)
        observed.append((path.stem, digest))
        return digest

    with env(start_worker=False) as (app, _client):
        plant(app, [good("A", "a@example.com")])
        storage.digest_of = spy
        try:
            assert run_once(app) == 1
        finally:
            storage.digest_of = real

        assert [cid for cid, _ in observed] == ["CERT-00000-planted-job"]
        digest = observed[0][1]
        with app.state.sessions() as session:
            assert session.query(Certificate).filter_by(job_id="planted-job").one().digest == digest
        assert db_state_of(env.db_path, rows_of(app, "planted-job")[0].id) == ISSUED


def test_one_failure_cannot_touch_another_row(env):
    batch = [good(f"R{i}", f"r{i}@example.com") for i in range(5)]
    real = worker.render_certificate

    def exploding(payload, certificate_id, template_id):
        if payload["name"] == "R2":
            raise RuntimeError("injected")
        return real(payload, certificate_id, template_id)

    with env(start_worker=False) as (app, _client):
        job_id = plant(app, batch)
        worker.render_certificate = exploding
        try:
            run_once(app)
        finally:
            worker.render_certificate = real

        states = {row.row_index: (row.state, row.reason_code) for row in rows_of(app, job_id)}
        assert states[2] == (REJECTED, "RENDER_FAILED"), states
        assert all(states[i] == (ISSUED, None) for i in (0, 1, 3, 4)), states
        assert db_state_of(env.db_path, rows_of(app, job_id)[2].id) == REJECTED

        with app.state.sessions() as session:
            assert session.query(Certificate).filter_by(job_id=job_id).count() == 4


def test_run_once_is_idempotent(env):
    with env(start_worker=False) as (app, _client):
        job_id = plant(app, [good("A", "a@example.com")])
        assert run_once(app) == 1
        assert run_once(app) == 0
        with app.state.sessions() as session:
            assert session.query(Certificate).filter_by(job_id=job_id).count() == 1


def test_pending_rows_are_picked_up_again(env):
    """A restart re-finds the same rows: the recipient row is the queue."""
    with env(start_worker=False) as (app, client):
        job_id = submit(client, [good("A", "a@example.com")])
        assert client.get(f"/jobs/{job_id}").json()["pending"] == 1
        assert run_once(app) == 1
        assert client.get(f"/jobs/{job_id}").json()["status"] == "COMPLETED"


def test_certificate_row_and_file_agree(env):
    with env(start_worker=False) as (app, _client):
        job_id = plant(app, [good("A", "a@example.com")])
        run_once(app)
        with app.state.sessions() as session:
            cert = session.query(Certificate).filter_by(job_id=job_id).one()
        path = path_for(app.state.storage_dir, cert.certificate_id)
        assert path.exists()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == cert.digest
        assert cert.bytes == path.stat().st_size
        assert not list(app.state.storage_dir.glob("*.tmp")), "a temp file was left behind"


def test_a_second_attempt_upserts_rather_than_double_issues(env):
    """The retry path: a row that was written but not committed gets re-settled once."""
    with env(start_worker=False) as (app, _client):
        job_id = plant(app, [good("", "a@example.com")])
        run_once(app)
        assert rows_of(app, job_id)[0].state == REJECTED

        with app.state.sessions() as session:
            row = session.query(Recipient).filter_by(job_id=job_id).one()
            row.payload = json.dumps(good("Ada", "a@example.com"))
            row.state = PENDING
            row.reason_code = None
            session.commit()

        run_once(app)
        with app.state.sessions() as session:
            assert session.query(Certificate).filter_by(job_id=job_id).count() == 1
        assert rows_of(app, job_id)[0].state == ISSUED


def test_constraints_refuse_the_three_duplicates(env):
    """These three live in the schema, not in application code."""
    with env(start_worker=False) as (app, _client):
        job_id = plant(app, [good("A", "a@example.com")])

        # 1. UNIQUE (job_id, row_index)
        with pytest.raises(IntegrityError):
            with app.state.sessions() as session:
                session.add(
                    Recipient(job_id=job_id, row_index=0, payload="{}", state=PENDING)
                )
                session.commit()

        # 2. UNIQUE on certificates.recipient_id
        run_once(app)
        recipient_id = rows_of(app, job_id)[0].id
        with pytest.raises(IntegrityError):
            with app.state.sessions() as session:
                session.add(
                    Certificate(
                        certificate_id="CERT-dup",
                        job_id=job_id,
                        recipient_id=recipient_id,
                        digest="d",
                        bytes=1,
                    )
                )
                session.commit()

        # 3. UNIQUE (idempotency_key)
        with pytest.raises(IntegrityError):
            with app.state.sessions() as session:
                for name in ("second", "third"):
                    session.add(
                        Job(
                            id=name,
                            idempotency_key="k",
                            template_id="t",
                            total=1,
                            status="PENDING",
                        )
                    )
                session.commit()


def test_a_failed_commit_does_not_poison_the_duplicate_cache(env, monkeypatch):
    """Row 0's commit fails, so nothing was issued for its email; its valid twin must issue
    rather than be rejected as DUPLICATE_IN_BATCH."""
    real_commit = Session.commit
    calls = []

    def flaky_commit(self):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return real_commit(self)

    with env(start_worker=False) as (app, _client):
        job_id = plant(app, [good("A", "a@example.com"), good("A Twin", "a@example.com")])
        monkeypatch.setattr(Session, "commit", flaky_commit)
        run_once(app)
        monkeypatch.undo()

        states = [(row.state, row.reason_code) for row in rows_of(app, job_id)]
        assert states == [(REJECTED, "RENDER_FAILED"), (ISSUED, None)], states


def test_issued_emails_are_queried_once_per_job_per_pass(env, monkeypatch):
    calls = []
    real = worker.issued_emails_in_job
    monkeypatch.setattr(
        worker, "issued_emails_in_job", lambda s, j: calls.append(j) or real(s, j)
    )
    with env(start_worker=False) as (app, _client):
        plant(app, [good(f"R{i}", f"r{i}@example.com") for i in range(20)])
        assert run_once(app) == 20
    assert calls == ["planted-job"], calls


def test_long_text_shrinks_to_fit_inside_the_border():
    payload = good("W" * 120, "w@example.com", course="M" * 160)
    lines = pdf_lines(render_certificate(payload, "CERT-1", "course-completion"))
    inner_left, inner_right = 53.85, 53.85 + 734.17  # the inner border in render.py
    checked = set()
    for font, size, x, text in lines:
        if text in (payload["name"], payload["course"]):
            checked.add(text)
            assert inner_left < x and x + _fit_width(text, font, size) < inner_right, text
    assert checked == {payload["name"], payload["course"]}


def _fit_width(text, font, size):
    from reportlab.pdfbase.pdfmetrics import stringWidth

    return stringWidth(text, font, size)


def test_fit_keeps_short_text_at_full_size():
    assert _fit("Ada", "Helvetica-Bold", 34, 600) == 34


def test_render_is_deterministic():
    payload = good("Ada", "ada@example.com")
    first = render_certificate(payload, "CERT-1", "course-completion")
    assert first.startswith(b"%PDF")
    assert first == render_certificate(payload, "CERT-1", "course-completion")
