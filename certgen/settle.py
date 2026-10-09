"""The settlement reducer. Pure functions only: no session, no clock, no filesystem.

Every recipient reaches exactly one terminal state, so at rest issued + rejected == total.
Job status is derived here and nowhere else.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

PENDING = "PENDING"
ISSUED = "ISSUED"
REJECTED = "REJECTED"
TERMINAL = frozenset({ISSUED, REJECTED})
STATES = (PENDING, ISSUED, REJECTED)


@dataclass(frozen=True)
class Counts:
    total: int
    issued: int
    rejected: int

    @property
    def pending(self) -> int:
        return self.total - self.issued - self.rejected

    @property
    def settled(self) -> int:
        return self.issued + self.rejected


def counts_from_states(states: Iterable[str]) -> Counts:
    """Tally a sequence of recipient states."""
    total = issued = rejected = 0
    for state in states:
        total += 1
        if state == ISSUED:
            issued += 1
        elif state == REJECTED:
            rejected += 1
    return Counts(total=total, issued=issued, rejected=rejected)


def counts_from_db(session, job_id: str) -> Counts:
    """Tally a job's recipients with one GROUP BY over their states."""
    from sqlalchemy import func, select

    from .models import Recipient

    tally = {
        state: n
        for state, n in session.execute(
            select(Recipient.state, func.count())
            .where(Recipient.job_id == job_id)
            .group_by(Recipient.state)
        )
    }
    return Counts(
        total=sum(tally.values()),
        issued=tally.get(ISSUED, 0),
        rejected=tally.get(REJECTED, 0),
    )


def status_of(counts: Counts) -> str:
    """Derive the job status. PARTIAL is a real terminal state, not an error."""
    pending = counts.pending
    if pending > 0:
        return "PENDING" if counts.settled == 0 else "PROCESSING"
    if counts.rejected > 0:
        return "PARTIAL"
    return "COMPLETED"


def pct_complete(counts: Counts) -> float:
    if counts.total == 0:
        return 0.0
    return round(counts.settled * 100 / counts.total, 2)


def settle_row(
    current: tuple[str, str | None], outcome: tuple[str, str | None]
) -> tuple[str, str | None]:
    """Apply one settlement to a row's `(state, reason_code)` and return the new pair.

    Idempotent: the first settlement wins and later ones change nothing at all, which is
    what makes a retried repair safe.
    """
    state, reason_code = current
    target, target_reason = outcome
    if target not in TERMINAL:
        raise ValueError(f"not a terminal state: {target!r}")
    if state in TERMINAL:
        return state, reason_code
    return target, target_reason
