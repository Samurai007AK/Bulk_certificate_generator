"""The HTTP surface a client actually touches."""

import csv
import io
import threading
import zipfile

from sqlalchemy import event

from certgen.api import MAX_RECIPIENTS
from certgen.models import Job
from tests.helpers import good, pdf_lines, poll, rows_of, settle, submit


def test_post_accepts_a_batch_and_settles_it(env):
    batch = [good(f"R{i}", f"r{i}@example.com") for i in range(12)]
    with env() as (_app, client):
        response = client.post("/jobs", json={"recipients": batch})
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]

        final = settle(client, job_id)
        assert final["total"] == 12
        assert final["issued"] == 12
        assert final["pct_complete"] == 100.0
        assert final["status"] == "COMPLETED"


def test_empty_batch_is_rejected(env):
    with env() as (_app, client):
        assert client.post("/jobs", json={"recipients": []}).status_code == 422


def test_malformed_requests_are_422_and_create_nothing(env):
    """Shape errors fail the request; content errors become per-row rejections."""
    one = good("A", "a@example.com")
    bad_bodies = [
        {},
        {"recipients": one},
        {"recipients": [{**one, "name": 123}]},
        {"recipients": [{**one, "name": "N" * 121}]},
        {"recipients": [one] * (MAX_RECIPIENTS + 1)},
        {"template_id": "diploma", "recipients": [one]},
    ]
    with env(start_worker=False) as (app, client):
        for body in bad_bodies:
            response = client.post("/jobs", json=body)
            assert response.status_code == 422, (str(body)[:80], response.text)
        with app.state.sessions() as session:
            assert session.query(Job).count() == 0

        # Naming the one predefined template explicitly is fine.
        ok = client.post("/jobs", json={"template_id": "course-completion", "recipients": [one]})
        assert ok.status_code == 202


def test_unknown_job_is_404_everywhere(env):
    with env() as (_app, client):
        missing = "does-not-exist"
        for path in (
            f"/jobs/{missing}",
            f"/jobs/{missing}/results",
            f"/jobs/{missing}/rejections.csv",
            f"/jobs/{missing}/archive.zip",
        ):
            assert client.get(path).status_code == 404, path
        assert client.patch(
            f"/jobs/{missing}/recipients",
            json={"repairs": [{"row_index": 0, "revision": 0, "patch": {}}]},
        ).status_code == 404
        assert client.get("/certificates/CERT-nope").status_code == 404


def test_counts_reconcile_at_every_poll(env):
    batch = [good(f"R{i}", f"r{i}@example.com") for i in range(30)] + [good("", "x@example.com")]
    with env() as (_app, client):
        job_id = submit(client, batch)
        for body in poll(client, job_id):
            assert body["issued"] + body["rejected"] + body["pending"] == body["total"]
        assert settle(client, job_id)["status"] == "PARTIAL"


def test_status_ignores_the_stored_column(env):
    """Status is derived from recipient rows; jobs.status is a mirror nobody reads."""
    with env() as (app, client):
        job_id = submit(client, [good("A", "a@example.com"), good("", "b@example.com")])
        assert settle(client, job_id)["status"] == "PARTIAL"

        with app.state.sessions() as session:
            session.query(Job).filter(Job.id == job_id).update({"status": "COMPLETED"})
            session.commit()

        assert client.get(f"/jobs/{job_id}").json()["status"] == "PARTIAL"


def test_results_are_keyed_by_the_original_row_index(env):
    batch = [good("A", "a@example.com"), good("", "b@example.com"), good("C", "c@example.com")]
    with env() as (_app, client):
        job_id = submit(client, batch)
        settle(client, job_id)
        results = client.get(f"/jobs/{job_id}/results").json()

        assert results["summary"]["issued"] == 2 and results["summary"]["rejected"] == 1
        assert [row["row_index"] for row in results["issued"]] == [0, 2]
        assert [row["row_index"] for row in results["rejected"]] == [1]
        rejected = results["rejected"][0]
        assert rejected["reason_code"] == "INVALID_NAME"
        assert rejected["points_at"] == "name"
        assert rejected["repairable"] is True


def test_missing_field_says_which_field(env):
    with env() as (_app, client):
        job_id = submit(client, [{"name": "A", "email": "a@example.com", "date": "2026-01-01"}])
        settle(client, job_id)
        rejected = client.get(f"/jobs/{job_id}/results").json()["rejected"][0]
        assert rejected["reason_code"] == "MISSING_FIELD"
        assert rejected["reason_detail"] == "course is required"


def test_certificate_is_retrievable(env):
    with env() as (_app, client):
        job_id = submit(client, [good("Ada", "ada@example.com")])
        settle(client, job_id)
        results = client.get(f"/jobs/{job_id}/results").json()
        url = results["issued"][0]["certificate_url"]

        response = client.get(url)
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.content.startswith(b"%PDF")

        # The certificate carries the recipient's details, not just a valid PDF header.
        drawn = [text for _font, _size, _x, text in pdf_lines(response.content)]
        assert "Ada" in drawn
        assert "Distributed Systems" in drawn
        assert "Date: 2026-02-14" in drawn
        assert any(results["issued"][0]["certificate_id"] in text for text in drawn)


def test_archive_holds_exactly_the_issued_certificates(env):
    batch = [good("A", "a@example.com"), good("", "b@example.com"), good("C", "c@example.com")]
    with env() as (_app, client):
        job_id = submit(client, batch)
        settle(client, job_id)
        issued = {
            f"{row['certificate_id']}.pdf"
            for row in client.get(f"/jobs/{job_id}/results").json()["issued"]
        }

        response = client.get(f"/jobs/{job_id}/archive.zip")
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            assert set(zf.namelist()) == issued
            assert zf.testzip() is None


def test_rejections_csv_points_at_the_field_to_fix(env):
    with env() as (_app, client):
        job_id = submit(client, [good("A", "a@example.com"), good("", "b@example.com")])
        settle(client, job_id)

        response = client.get(f"/jobs/{job_id}/rejections.csv")
        rows = list(csv.DictReader(io.StringIO(response.text)))
        assert len(rows) == 1
        assert rows[0]["reason_code"] == "INVALID_NAME"
        assert rows[0]["points_at"] == "name"
        assert rows[0]["revision"] == "0"


def test_rejections_csv_neutralises_formulas(env):
    with env() as (_app, client):
        job_id = submit(client, [good("=HYPERLINK(1)", "not-an-email")])
        settle(client, job_id)
        row = next(csv.DictReader(io.StringIO(client.get(f"/jobs/{job_id}/rejections.csv").text)))
        assert row["name"] == "'=HYPERLINK(1)"


def test_batched_repair_uses_compare_and_set(env):
    batch = [good("A", "a@example.com"), good("", "b@example.com"), good("C", "c@example.com")]
    with env() as (_app, client):
        job_id = submit(client, batch)
        assert settle(client, job_id)["status"] == "PARTIAL"
        rejected = client.get(f"/jobs/{job_id}/results").json()["rejected"][0]

    repair = {
        "row_index": rejected["row_index"],
        "revision": rejected["revision"],
        "patch": {"name": "Bee"},
    }

    # No worker in this window, so the replay loses the compare-and-set instead of racing
    # the sweep.
    with env(start_worker=False) as (_app, quiet):
        first = quiet.patch(f"/jobs/{job_id}/recipients", json={"repairs": [repair]})
        assert first.json()["updated"] == [rejected["row_index"]]

        stale = quiet.patch(f"/jobs/{job_id}/recipients", json={"repairs": [repair]})
        assert stale.json()["updated"] == []
        assert stale.json()["skipped"][0]["reason"] == "stale_revision"

    with env() as (app, client):
        final = settle(client, job_id)
        assert final["status"] == "COMPLETED", final
        assert rows_of(app, job_id)[rejected["row_index"]].name == "Bee"


def test_patch_validates_the_patch(env):
    with env() as (_app, client):
        job_id = submit(client, [good("", "a@example.com")])
        settle(client, job_id)
        response = client.patch(
            f"/jobs/{job_id}/recipients",
            json={"repairs": [{"row_index": 0, "revision": 0, "patch": {"name": {"x": 1}}}]},
        )
        assert response.status_code == 422, response.text


def test_racing_repairs_with_one_revision_update_once(env):
    """Both requests read revision 0 before either writes; the conditional UPDATE lets
    exactly one win. A read-check-write in Python would let both through."""
    with env() as (app, client):
        job_id = submit(client, [good("", "a@example.com")])
        settle(client, job_id)

    with env(start_worker=False) as (app, client):
        barrier = threading.Barrier(2, timeout=10)

        def hold_updates_until_both_have_read(_conn, _cursor, statement, *_args):
            if statement.lstrip().upper().startswith("UPDATE RECIPIENTS"):
                barrier.wait()

        event.listen(app.state.engine, "before_cursor_execute", hold_updates_until_both_have_read)
        responses = []

        def repair(name):
            body = {"repairs": [{"row_index": 0, "revision": 0, "patch": {"name": name}}]}
            responses.append(client.patch(f"/jobs/{job_id}/recipients", json=body).json())

        threads = [threading.Thread(target=repair, args=(n,)) for n in ("Ann", "Bob")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(len(r["updated"]) for r in responses) == [0, 1], responses
        loser = next(r for r in responses if not r["updated"])
        assert loser["skipped"][0]["reason"] == "stale_revision", loser
        assert rows_of(app, job_id)[0].revision == 1


def test_idempotency_key_replays_and_conflicts(env):
    batch = [good("A", "a@example.com")]
    with env() as (app, client):
        first = client.post("/jobs", json={"recipients": batch}, headers={"Idempotency-Key": "k"})
        assert first.status_code == 202
        job_id = first.json()["job_id"]

        replay = client.post("/jobs", json={"recipients": batch}, headers={"Idempotency-Key": "k"})
        assert replay.status_code == 200
        assert replay.json()["job_id"] == job_id

        conflict = client.post(
            "/jobs",
            json={"recipients": [good("Other", "other@example.com")]},
            headers={"Idempotency-Key": "k"},
        )
        assert conflict.status_code == 409

        with app.state.sessions() as session:
            assert session.query(Job).count() == 1


def test_key_order_does_not_break_idempotency(env):
    batch = [{"email": "a@example.com", "name": "A", "course": "C", "date": "2026-01-01"}]
    reordered = [{"date": "2026-01-01", "course": "C", "name": "A", "email": "a@example.com"}]
    with env() as (_app, client):
        first = client.post("/jobs", json={"recipients": batch}, headers={"Idempotency-Key": "k"})
        second = client.post("/jobs", json={"recipients": reordered}, headers={"Idempotency-Key": "k"})
        assert first.status_code == 202
        assert second.status_code == 200
        assert second.json()["job_id"] == first.json()["job_id"]


def test_healthz(env):
    with env() as (_app, client):
        assert client.get("/healthz").json() == {"ok": True}
