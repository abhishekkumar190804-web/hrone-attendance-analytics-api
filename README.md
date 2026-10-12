# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of the HROne assignment. All application code is in
`app/main.py` and the service starts with:

```bash
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"   # or an Atlas connection string
export MONGO_DB="attendance_db"
python sample_seed.py                          # optional: load the nine sample documents
uvicorn app.main:app --port 8000
```

`MONGO_URI` and `MONGO_DB` are read from the environment; a local `.env` is loaded but
real environment variables take priority (they are never overridden). All indexes are
created idempotently at startup. MongoDB 6.0+ is required (developed and tested on 7.0;
`$setWindowFields` and `$densify` need 5.0+/5.1+).

## What is implemented

* `GET /health`, `POST/GET /employees`, `POST /attendance/punch-in`, `POST /attendance/punch-out`,
  `PATCH /attendance/{emp_code}/{date}`, `GET /attendance`.
* `GET /analytics/employees/{emp_code}/monthly`, `/analytics/departments/summary`,
  `/analytics/leaderboard/late`, `/analytics/departments/{department}/trend` - all backed by
  MongoDB aggregation pipelines (`$lookup`, `$unionWith`, `$setWindowFields`, `$densify`).
* `GET /admin/explain/{endpoint}` - `executionStats` explain of each endpoint's main query or
  pipeline (IXSCAN, no COLLSCAN).

Business rules R1-R10 (IST dates, overnight shifts, late, overtime, work hours, half day,
presence, working days, rounding, headcount, pagination) are applied as defined in `openapi.yaml`.
Analytics read the stored derived values rather than recomputing them.

## Concurrency

The unique indexes on `employees.emp_code` and `attendance_logs.(emp_code, date)` give the
required races their exact outcomes: one 201 for simultaneous punch-ins, one 200 for
simultaneous punch-outs (both guarded by atomic updates), and regularizations use an
optimistic filter so concurrent edits return 409 instead of losing history.

## Tests and verification

The endpoints were verified against a live MongoDB database with the supplied sample kit plus
hand-built edge fixtures (overnight shifts, mid-month joiners, employees with no logs, weekend
records, half days, leaderboard ties at the cutoff, missing weekdays). Coverage includes:

* R1-R5 boundary math (09:40:00 vs 09:40:01, 29 vs 30 minutes overtime, 2.345 -> 2.35 half-up,
  sub-second truncation, overnight punch dates and overtime).
* Every status code path: 200/201, 404, 409, 422 (bad timestamps, bad dates, bad status,
  duplicate punch-in/out, unknown employee/department).
* Analytics output compared against an independent Python reference implementing R6-R10.
* Concurrency: 5 parallel punch-ins -> one 201 + four 409; 5 parallel punch-outs -> one 200 + four 409.
* `GET /admin/explain/{endpoint}` uses index scans and no collection scans for all five endpoints.

To repeat the smoke test against the sample data:

```bash
export MONGO_URI="mongodb://localhost:27017"; export MONGO_DB="attendance_db"
python sample_seed.py
uvicorn app.main:app --port 8000
curl localhost:8000/health               # {"status":"ok"}
curl 'localhost:8000/employees?page_size=5'
curl 'localhost:8000/analytics/departments/summary?month=2026-07'
curl 'localhost:8000/admin/explain/attendance_list?page_size=5'
```

## Notes

See `REVIEW.md` for the Part A defect list and `DECISIONS.md` for the Part C answers.
`DATA_MODEL.md`, `openapi.yaml`, `PROBLEM_STATEMENT.docx`, `sample_data/` and `sample_seed.py`
are the original candidate-kit files and are kept for reference. No `.env`, secrets, Dockerfile,
virtual environment or data dump is committed.