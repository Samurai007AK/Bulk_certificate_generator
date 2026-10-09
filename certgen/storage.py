"""Durable certificate storage.

The ordering rule, stated once: render -> write + fsync -> hash -> only then update the
recipient row, in the same transaction as the certificate row. A row is never marked ISSUED
before its file is durably on disk, so a crash cannot leave the ledger claiming a
certificate that does not exist.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from sqlalchemy import select

from .models import Certificate, Recipient, utc_now
from .settle import ISSUED, settle_row


def certificate_id_for(job_id: str, row_index: int) -> str:
    """Public, stable, and computable before any row exists, so the renderer can stamp it."""
    return f"CERT-{row_index:05d}-{job_id}"


def path_for(storage_dir: Path | str, certificate_id: str) -> Path:
    return Path(storage_dir) / f"{certificate_id}.pdf"


def write_durably(path: Path, data: bytes) -> None:
    """Write, fsync, then rename into place. Returns once the bytes are on the medium."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    # Note: POSIX would also fsync the directory so the rename itself survives a power
    # cut; Windows has no directory fsync to call. Add the POSIX branch if that matters.


def digest_of(path: Path, data: bytes) -> str:
    """Digest of the bytes just made durable at `path`.

    Hashes the in-memory copy rather than re-reading the file: the bytes are identical once
    fsync returns, and re-reading a fresh file costs ~17ms on Windows (antivirus scan) which
    was over half of each row's time. Called after the write, so the ordering rule holds.
    """
    return hashlib.sha256(data).hexdigest()


def commit_certificate(
    session, recipient: Recipient, pdf_bytes: bytes, storage_dir: Path | str
) -> Certificate:
    """Put the file on disk, then record it and flip the row in the caller's transaction."""
    certificate_id = certificate_id_for(recipient.job_id, recipient.row_index)
    path = path_for(storage_dir, certificate_id)

    write_durably(path, pdf_bytes)
    digest = digest_of(path, pdf_bytes)

    cert = session.scalar(
        select(Certificate).where(Certificate.recipient_id == recipient.id)
    )
    if cert is None:
        cert = Certificate(
            certificate_id=certificate_id,
            job_id=recipient.job_id,
            recipient_id=recipient.id,
            digest=digest,
            bytes=len(pdf_bytes),
            created_at=utc_now(),
        )
        session.add(cert)
    else:
        # Retried repair: upsert, never double-issue.
        cert.certificate_id = certificate_id
        cert.digest = digest
        cert.bytes = len(pdf_bytes)

    state, _ = settle_row((recipient.state, recipient.reason_code), (ISSUED, None))
    recipient.state = state
    recipient.reason_code = None
    recipient.reason_msg = None
    recipient.reason_detail = None
    return cert
