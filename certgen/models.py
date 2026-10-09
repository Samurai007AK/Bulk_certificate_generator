"""Schema. Three tables: a job, its recipient rows, and the certificates they produced.

The recipient row is the unit of truth. Job status is a GROUP BY over these rows, never a
counter. `jobs.status` is a denormalised mirror that exists only so a reader running
`SELECT * FROM jobs` sees something readable; no endpoint reads it.
"""

from __future__ import annotations

import datetime as _dt

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    # NULL means "not idempotent". SQLite and Postgres both allow many NULLs under UNIQUE.
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    payload_hash: Mapped[str | None] = mapped_column(Text)
    template_id: Mapped[str] = mapped_column(Text)
    total: Mapped[int] = mapped_column(Integer)
    # Denormalised mirror of the derived aggregate. Never a source of truth.
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, default=utc_now)

    recipients: Mapped[list["Recipient"]] = relationship(back_populates="job")


class Recipient(Base):
    __tablename__ = "recipients"
    __table_args__ = (
        # A re-submitted batch cannot duplicate rows.
        UniqueConstraint("job_id", "row_index", name="uq_recipient_job_row"),
        # The worker's sweep and the status GROUP BY both filter on this pair.
        Index("ix_recipient_sweep", "state", "job_id", "row_index"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    # Echoes the caller's array position so results need no client-side correlation.
    row_index: Mapped[int] = mapped_column(Integer)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    reason_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Why a renderer raised. Carries the exception text for RENDER_FAILED.
    reason_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Bumped by every accepted repair; the PATCH endpoint's compare-and-set counter.
    revision: Mapped[int] = mapped_column(Integer, default=0)

    job: Mapped[Job] = relationship(back_populates="recipients")
    certificate: Mapped["Certificate | None"] = relationship(back_populates="recipient")


class Certificate(Base):
    __tablename__ = "certificates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Public, stable, and known before the row exists: the renderer stamps it into the PDF,
    # so it cannot depend on this table's autoincrement id.
    certificate_id: Mapped[str] = mapped_column(Text, unique=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    # UNIQUE, so a retried repair upserts this row and can never double-issue.
    recipient_id: Mapped[int] = mapped_column(
        ForeignKey("recipients.id"), unique=True, index=True
    )
    digest: Mapped[str] = mapped_column(Text)
    bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(Text, default=utc_now)

    recipient: Mapped[Recipient] = relationship(back_populates="certificate")
