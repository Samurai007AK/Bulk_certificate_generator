"""The reducer is the part that is hard to change later, so it is pinned first."""

import pytest

from certgen.settle import (
    ISSUED,
    PENDING,
    REJECTED,
    Counts,
    counts_from_db,
    counts_from_states,
    pct_complete,
    settle_row,
    status_of,
)


def test_counts_from_states():
    counts = counts_from_states([PENDING, ISSUED, ISSUED, REJECTED])
    assert counts == Counts(total=4, issued=2, rejected=1)
    assert counts.pending == 1
    assert counts.settled == 3


@pytest.mark.parametrize(
    "states,expected",
    [
        ([PENDING, PENDING], "PENDING"),
        ([ISSUED, PENDING], "PROCESSING"),
        ([REJECTED, PENDING], "PROCESSING"),
        ([ISSUED, ISSUED], "COMPLETED"),
        ([ISSUED, REJECTED], "PARTIAL"),
        ([REJECTED, REJECTED], "PARTIAL"),
    ],
)
def test_status_is_derived(states, expected):
    assert status_of(counts_from_states(states)) == expected


@pytest.mark.parametrize("states", [[ISSUED], [REJECTED], [ISSUED, REJECTED, ISSUED]])
def test_every_recipient_settles_exactly_once(states):
    """The invariant the whole model rests on: at rest, issued + rejected == total."""
    counts = counts_from_states(states)
    assert counts.pending == 0
    assert counts.issued + counts.rejected == counts.total


def test_double_settle_is_a_no_op():
    once = settle_row((PENDING, None), (ISSUED, None))
    assert once == (ISSUED, None)

    twice = settle_row(once, (ISSUED, None))
    assert twice == once

    # Even a conflicting second settlement cannot rewrite a terminal row.
    conflicting = settle_row(twice, (REJECTED, "INVALID_NAME"))
    assert conflicting == (ISSUED, None)


def test_double_settle_preserves_the_first_reason_code():
    once = settle_row((PENDING, "MISSING_FIELD"), (REJECTED, "MISSING_FIELD"))
    assert once == (REJECTED, "MISSING_FIELD")
    assert settle_row(once, (REJECTED, "INVALID_NAME")) == once


def test_pending_is_not_terminal():
    with pytest.raises(ValueError):
        settle_row((PENDING, None), (PENDING, None))


def test_pct_complete():
    assert pct_complete(Counts(total=0, issued=0, rejected=0)) == 0.0
    assert pct_complete(Counts(total=4, issued=1, rejected=1, )) == 50.0
    assert pct_complete(Counts(total=3, issued=1, rejected=2)) == 100.0
    assert pct_complete(Counts(total=200, issued=100, rejected=0)) == 50.0


def test_counts_from_db_matches_the_state_list(env):
    from tests.helpers import good, submit, settle

    with env(start_worker=False) as (app, client):
        job_id = submit(client, [good("A", "a@example.com"), good("", "b@example.com")])
        from certgen.worker import run_once

        run_once(app.state.sessions, app.state.storage_dir)
        settle(client, job_id)

        with app.state.sessions() as session:
            counts = counts_from_db(session, job_id)
    assert counts == Counts(total=2, issued=1, rejected=1)
