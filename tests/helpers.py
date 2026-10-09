"""Shared test helpers: one definition of "a good recipient", "wait for the job to settle",
and "read the text drawn on a PDF", so every test means the same thing by them.
"""

from __future__ import annotations

import base64
import re
import sqlite3
import time
import zlib
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from certgen.api import create_app
from certgen.config import Settings
from certgen.models import Recipient


_FONTS = {"F1": "Helvetica", "F2": "Helvetica-Bold"}
_OPS = re.compile(rb"/(F\d) ([\d.]+) Tf|1 0 0 1 ([\d.]+) [\d.]+ Tm \((.*?)\) Tj", re.S)


_ESCAPE = re.compile(rb"\\([0-7]{1,3}|.)")


def _unescape(match: re.Match) -> bytes:
    """PDF string escapes: `\\351` is the octal byte 0xE9, `\\(` is a literal paren."""
    code = match.group(1)
    return bytes([int(code, 8)]) if code[:1].isdigit() else code


def pdf_lines(pdf: bytes) -> list[tuple[str, float, float, str]]:
    """Every string drawn on the page as `(font, size, x, text)`.

    ReportLab compresses the page stream (ASCII85 + Flate), so grepping the raw bytes for a
    name finds nothing. This decodes the stream and reads the text operators back out.
    """
    lines = []
    for raw in re.findall(rb"stream\r?\n(.*?)endstream", pdf, re.S):
        try:
            content = zlib.decompress(base64.a85decode(raw.strip(), adobe=True))
        except ValueError:
            continue
        font, size = "", 0.0
        for match in _OPS.finditer(content):
            if match.group(1):
                font, size = _FONTS[match.group(1).decode()], float(match.group(2))
            else:
                text = _ESCAPE.sub(_unescape, match.group(4)).decode("cp1252")
                lines.append((font, size, float(match.group(3)), text))
    return lines


def good(name: str, email: str, course: str = "Distributed Systems", date: str = "2026-02-14"):
    return {"name": name, "email": email, "course": course, "date": date}


@contextmanager
def open_client(
    root: Path, *, name: str = "db.sqlite3", start_worker: bool = True
) -> TestClient:
    """A client over a throwaway database. Reuse `name` to reopen the same database."""
    settings = Settings(
        database_url=f"sqlite:///{(root / name).as_posix()}",
        storage_dir=str(root / "certs"),
        poll_interval=0.01,
    )
    with TestClient(create_app(settings, start_worker=start_worker)) as client:
        yield client


def submit(client: TestClient, recipients: list[dict], key: str | None = None) -> str:
    headers = {"Idempotency-Key": key} if key else {}
    response = client.post(
        "/jobs",
        json={"template_id": "course-completion", "recipients": recipients},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def poll(client: TestClient, job_id: str, timeout: float = 60.0) -> list[dict]:
    """Every status response observed, so a caller can assert an invariant at each step."""
    seen: list[dict] = []
    deadline = time.monotonic() + timeout
    while True:
        body = client.get(f"/jobs/{job_id}").json()
        seen.append(body)
        if body["pending"] == 0 or time.monotonic() > deadline:
            return seen


def settle(client: TestClient, job_id: str, timeout: float = 60.0) -> dict:
    seen = poll(client, job_id, timeout)
    assert seen[-1]["pending"] == 0, f"job never settled: {seen[-1]}"
    return seen[-1]


def rows_of(app, job_id: str) -> list[Recipient]:
    with app.state.sessions() as session:
        return list(
            session.scalars(
                select(Recipient)
                .where(Recipient.job_id == job_id)
                .order_by(Recipient.row_index)
            )
        )


def db_state_of(db_path: Path, recipient_id: int) -> str:
    """Read one row on its own connection: independent of the session's identity map."""
    con = sqlite3.connect(db_path)
    try:
        found = con.execute("SELECT state FROM recipients WHERE id = ?", (recipient_id,)).fetchone()
        return found[0] if found else ""
    finally:
        con.close()


def split_certificate_id(certificate_id: str) -> tuple[int, str]:
    """`CERT-00007-<job_id>` -> `(7, job_id)`."""
    row_index, _, job_id = certificate_id.removeprefix("CERT-").partition("-")
    return int(row_index), job_id


def db_row_for_certificate(db_path: Path, certificate_id: str) -> tuple[int, str] | None:
    """The recipient row a certificate id refers to, read on its own connection.

    Returns `(recipient_id, state)`. The certificate row does not exist yet when this is
    used to observe the worker mid-commit, so the lookup goes via the public id instead.
    """
    row_index, job_id = split_certificate_id(certificate_id)
    con = sqlite3.connect(db_path)
    try:
        found = con.execute(
            "SELECT id, state FROM recipients WHERE job_id = ? AND row_index = ?",
            (job_id, row_index),
        ).fetchone()
        return found
    finally:
        con.close()
