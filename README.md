# Bulk Certificate Generator

A FastAPI service that takes one request with many recipients, generates one PDF certificate
per valid recipient from a single predefined template, and reports progress while it works.
Every recipient ends in exactly one of two states, `ISSUED` (with a downloadable PDF) or
`REJECTED` (with a machine-readable reason code), so nothing is silently skipped. Rejected rows
can be corrected and re-queued in the same job.

**Stack:** Python 3.11+ · FastAPI · SQLAlchemy 2 · SQLite (Postgres by URL) · ReportLab · pytest.

## How the brief maps to this repo

| Requirement | Where | Proven by |
|---|---|---|
| Accept a bulk generation request | `POST /jobs` (`certgen/api.py`) | `tests/test_api.py::test_post_accepts_a_batch_and_settles_it` |
| Validate recipient data | `certgen/reasons.py::validate`, pydantic models in `api.py` | `tests/test_reasons.py::test_each_code_is_reachable`, `tests/test_api.py::test_malformed_requests_are_422_and_create_nothing` |
| Generate one certificate per valid recipient | `certgen/worker.py`, `certgen/render.py` | `tests/test_api.py::test_certificate_is_retrievable` (asserts the name, course, date and id are drawn in the PDF) |
| Track status / progress | `GET /jobs/{id}`, `certgen/settle.py` | `tests/test_api.py::test_counts_reconcile_at_every_poll`, `tests/test_settle.py` |
| One failure must not block the others | per-row `try/except` in `worker.run_once` | `tests/test_worker.py::test_one_failure_cannot_touch_another_row` |
| Retrieve generated certificates | `GET /certificates/{id}`, `GET /jobs/{id}/archive.zip` | `tests/test_api.py::test_certificate_is_retrievable`, `::test_archive_holds_exactly_the_issued_certificates` |

## Setup

Python 3.11 or newer. From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

There are no system packages to install: ReportLab is pure Python and SQLite ships with Python.

## Run the application

```bash
python -m uvicorn certgen.api:create_app --factory --port 8000
```

The service creates `certgen.db` and a `certificates/` folder in the current directory.
Interactive API docs (Swagger) are at <http://localhost:8000/docs>.

## Run the tests

```bash
python -m pytest
```

The suite uses throwaway databases and needs no running server.

## Submit a certificate generation request

One `POST` carries the whole batch, up to 10,000 recipients. `examples/batch.json` is a
five-person batch where three rows are deliberately wrong: a blank name, an email already
used by an earlier row, and a non-ISO date.

```bash
curl -X POST localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: cohort-2026-02" \
  --data-binary @examples/batch.json
# 202 {"job_id": "3f2a...", "status": "PENDING"}
```

On Windows PowerShell use `curl.exe` instead of `curl`. Passing the body as a file with
`--data-binary @file` works the same in bash, PowerShell and cmd.

Each recipient takes `name`, `email`, `course` and `date` (`YYYY-MM-DD`). Unknown extra fields
are stored and returned unchanged. `Idempotency-Key` is optional: resending the same body with
the same key returns `200` and the original `job_id`; the same key with a different body
returns `409`.

## Check progress

```bash
curl localhost:8000/jobs/<job_id>
# {"job_id": "...", "status": "PARTIAL", "total": 5, "issued": 2, "rejected": 3,
#  "pending": 0, "pct_complete": 100.0}
```

`status` moves `PENDING` → `PROCESSING` → `COMPLETED` (all issued) or `PARTIAL` (finished, some
rejected). For the per-recipient outcome:

```bash
curl localhost:8000/jobs/<job_id>/results
```

```json
{
  "job_id": "...",
  "summary": {"job_id": "...", "status": "PARTIAL", "total": 5, "issued": 2, "rejected": 3, "pending": 0, "pct_complete": 100.0},
  "issued": [
    {"row_index": 0, "name": "Ada Lovelace", "email": "ada@example.com",
     "certificate_id": "CERT-00000-<job_id>", "certificate_url": "/certificates/CERT-00000-<job_id>"}
  ],
  "rejected": [
    {"row_index": 1, "reason_code": "INVALID_NAME", "points_at": "name", "reason_detail": null,
     "reason_msg": "name is empty or contains no letters", "repairable": true, "revision": 0}
  ]
}
```

`row_index` is the recipient's position in the array you sent, so you never need to match rows
back up by name or email.

## Retrieve generated certificates

```bash
# One certificate (certificate_url from /results)
curl -o ada.pdf localhost:8000/certificates/CERT-00000-<job_id>

# Every issued certificate in the job, as one zip
curl -o certificates.zip localhost:8000/jobs/<job_id>/archive.zip

# The rejected rows as a spreadsheet, one row per rejection with its reason
curl -o rejections.csv localhost:8000/jobs/<job_id>/rejections.csv
```

## Fix rejected rows (optional)

A rejected row is corrected with a batched `PATCH` that names the row, the `revision` you
read from `/results`, and only the fields to change. The row goes back to `PENDING` and the
same worker processes it again. `examples/repair.json` fixes all three rejects from the
example batch:

```bash
curl -X PATCH localhost:8000/jobs/<job_id>/recipients \
  -H "Content-Type: application/json" \
  --data-binary @examples/repair.json
# 200 {"job_id": "...", "updated": [1, 3, 4], "skipped": []}
# then GET /jobs/<job_id> reaches "COMPLETED" with 5 issued
```

`revision` is an optimistic lock. If two clients repair the same row with the same revision,
exactly one wins, and the other gets `{"reason": "stale_revision"}` in `skipped`. Per-row
outcomes are reported in the `200` body. Only a malformed request is a `4xx`.

## API reference

| Method | Path | Returns |
|---|---|---|
| `POST` | `/jobs` | `202 {job_id, status}`; `200` on idempotent replay; `409` on key reuse with a different body; `422` on a malformed request |
| `GET` | `/jobs/{id}` | Status, counts, `pct_complete` |
| `GET` | `/jobs/{id}/results` | `summary` plus the `issued` and `rejected` rows |
| `GET` | `/jobs/{id}/rejections.csv` | Rejected rows as CSV |
| `PATCH` | `/jobs/{id}/recipients` | `{updated, skipped}` for a batch of repairs |
| `GET` | `/certificates/{certificate_id}` | The PDF |
| `GET` | `/jobs/{id}/archive.zip` | Every issued PDF for the job |
| `GET` | `/healthz` | Liveness |

Unknown job or certificate ids return `404`.

## Validation

There are two layers, and the split is deliberate:

- **The request shape is checked up front.** The service returns `422` and creates nothing
  if the request is malformed:
  - the body is not a JSON object with a `recipients` array;
  - the array is empty or has more than 10,000 recipients;
  - a field has the wrong type (e.g. `"name": 123`);
  - a field is absurdly long (name > 120, course > 160, email > 320 characters);
  - `template_id` names a template other than the one predefined template.

  These are client bugs, and pydantic's error names the offending `recipients[i]`.
- **Row content is checked per recipient** by the worker. A bad row is rejected with a reason
  code and never blocks the rest of the batch.

| Code | Points at | Meaning |
|---|---|---|
| `MISSING_FIELD` | see `reason_detail` | `name`, `course` or `date` is absent or blank; `reason_detail` names it |
| `INVALID_NAME` | `name` | empty, or contains no letters |
| `UNSUPPORTED_CHARACTERS` | see `reason_detail` | a field has characters the certificate font cannot draw (outside Latin-1, e.g. CJK, Cyrillic, emoji) |
| `INVALID_DATE` | `date` | not a real `YYYY-MM-DD` date |
| `INVALID_EMAIL` | `email` | failed syntactic validation |
| `DUPLICATE_IN_BATCH` | `email` | an earlier recipient in this job was already issued this email |
| `RENDER_FAILED` | see `reason_detail` | the renderer raised; the exception text is in `reason_detail` |

When two rows share an email, the first valid one issues and the later one is rejected. A row
that was itself invalid does not block its valid twin.

## Design decisions

**Background processing, not synchronous.**
- `POST /jobs` validates the request shape, writes one row per recipient in a single
  transaction, and returns `202` immediately.
- A background thread inside the same process then generates the certificates.
- **Why not synchronous:** rendering thousands of PDFs inside one HTTP request would time out,
  and a client could not watch progress.
- **Why not Celery + Redis:** that is a second service and a broker for anyone running this, and
  it adds nothing this needs. The recipient table already is a durable queue: the worker loops
  over `PENDING` rows. If the process restarts, it finds the same rows and carries on, so
  there is nothing to rebuild.

**Status is derived, never counted.** `GET /jobs/{id}` computes its numbers from the recipient
rows with one `GROUP BY` on every call, so the status cannot drift from reality. The
`jobs.status` column is a mirror for people reading the table; no endpoint reads it.

**Failure isolation.** Each recipient is validated, rendered, written and committed inside its
own `try`/`except`. An exception on row 7 rolls back only row 7, which is rejected as
`RENDER_FAILED` with the exception text. Every other row carries on.

**Write the file before the ledger.** Render → write to a temp file → `fsync` → rename into
place → hash → only then mark the row `ISSUED`, in the same transaction that records the
certificate. A crash can leave an orphan file, but never an `ISSUED` row without its PDF. A
test checks this at the exact moment of the hand-off, on an independent database connection.

**Repairs instead of resubmission.** A rejected row keeps its `row_index` and is fixed in
place, so a 2,000-person job with 30 typos becomes one `PATCH`, not a new job. The
compare-and-set is a single conditional `UPDATE ... WHERE state='REJECTED' AND revision=N`,
so the database, not Python, decides which of two racing repairs wins.

**Idempotent submission.** `UNIQUE(idempotency_key)` makes the database refuse a duplicate job
even when two identical `POST`s race. The loser replays the winner's response.

**Deterministic certificates.** ReportLab's invariant mode pins the PDF's timestamp and id, so
the stored SHA-256 digest identifies the content, not the moment it was rendered.

**SQLite by default.** A clone runs with zero setup. WAL mode lets the worker write while
requests read. The schema is plain SQLAlchemy, so Postgres is a URL change.

**One worker process.** Run a single uvicorn process (the default). Two processes would both
sweep the same `PENDING` rows. The unique constraint on `certificates.recipient_id` stops a
double issue, but the work is duplicated and file writes can collide. Scaling out is the third
item under *Future improvements* below.

**Performance.** On a laptop, a 2,000-recipient `POST` returns in 0.2s and the job finishes
in about 21s (roughly 95 certificates per second). Each row costs one render, one `fsync` and
one SQLite commit.

## Schema

Three tables: `jobs`, `recipients` (one row per submitted recipient, the unit of truth) and
`certificates`. Three constraints carry real weight and are tested as refusals:

1. `UNIQUE (job_id, row_index)`: a job cannot hold two rows for the same input position.
2. `UNIQUE (certificates.recipient_id)`: a retried row updates its certificate in place and is
   never issued twice.
3. `UNIQUE (jobs.idempotency_key)`: the database refuses a duplicate `POST`.

Certificate ids are public strings, `CERT-<row_index, 5 digits>-<job_id>`
(e.g. `CERT-00007-3f2a...`), rather than the table's autoincrement id. The renderer prints the
id on the PDF, so it has to exist before the database row does.

## Configuration

Environment variables, all optional:

| Variable | Default | |
|---|---|---|
| `CERTGEN_DATABASE_URL` | `sqlite:///certgen.db` | Any SQLAlchemy URL. For Postgres, `pip install psycopg` and use `postgresql+psycopg://...` |
| `CERTGEN_STORAGE_DIR` | `certificates` | Where PDFs are written |

`template_id`, `poll_interval` and `batch_size` are fields of `certgen/config.py::Settings`.

## Out of scope

- **Authentication and authorisation are out of scope.** The brief never mentions them, so this
  service trusts its caller. Put auth in front of it before exposing it.
- A template editor or multiple designs. There is one predefined template, with the fields
  `name`, `course` and `date` plus the certificate id. Adding a field starts at
  `REQUIRED_FIELDS` in `certgen/reasons.py` and the drawing code in `certgen/render.py`.
- Non-Latin scripts on the certificate. Names outside Latin-1 are rejected as
  `UNSUPPORTED_CHARACTERS` rather than printed as black boxes. Supporting them means
  embedding a Unicode TTF font in `render.py`.
- Cross-job duplicate detection, which would suppress legitimate repeat awards.

## Future improvements

In the order they are worth doing:

1. **Boot reconciliation.** Resuming already works, because unfinished rows stay `PENDING`
   and the worker picks them up again on start. What is missing is visibility: a startup pass
   that marks interrupted jobs as resumed and logs how many rows it re-queued.
2. **`certctl replay <job_id>`.** Walk `ISSUED` rows, re-hash each file against its stored
   digest, re-render mismatches, and print a failure histogram. This is the audit that would
   catch a storage fault nothing else notices.
3. **Multiple workers via Postgres `FOR UPDATE SKIP LOCKED`.** Each worker claims a disjoint set
   of `PENDING` rows, so processing scales past one process without changing the resume
   semantics.

## Repository layout

```
certgen/   api.py (HTTP) · worker.py (background sweep) · render.py (PDF template)
           reasons.py (validation + reason codes) · settle.py (status math)
           storage.py (durable file writes) · models.py · db.py · config.py
tests/     pytest suite
examples/  batch.json, repair.json used above
```
