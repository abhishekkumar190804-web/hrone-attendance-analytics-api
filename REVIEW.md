# REVIEW.md

Defects found in the starter `app/main.py`. The rule numbers refer to `openapi.yaml` (R1-R10).

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` (33-37) | Compares floored minutes with `> 10`, so exactly 09:40:01 gives 0 instead of 10 (grace is *strictly more than* 10:00). | Shift 09:30, punch-in 09:40:01 -> returns 0, expected 10. | Compare exact elapsed seconds `> 600`, then return `floor(seconds/60)`. |
| 2 | `compute_late_minutes` (33-37) | Anchors the shift start on the punch-in's own calendar date and never considers overnight shifts; after midnight the shift started the previous day. | Overnight 22:00-06:00, punch-in 22:15 -> late is computed off the wrong "09:30-style" day. | Pass the record's attendance date (R1) and anchor the shift start to it. |
| 3 | `compute_work_hours` (40-41) | Uses Python `round()` (banker's rounding) instead of half-up (R4/R8). | 2.345 hours -> 2.34, expected 2.35. | Round with `decimal` `ROUND_HALF_UP`. |
| 4 | `compute_overtime` (44-47) | No `>= 30` threshold (R3): any positive overtime is stored (e.g. 5). | Punch out 18:35 on an 18:30 shift -> stored 5, expected 0. | Return the minutes only when they are `>= 30`. |
| 5 | `compute_overtime` (44-47) | Builds shift_end on the record's date for every shift, so overnight overtime is anchored to the wrong day; also mixes a tz-aware `end` with a possibly naive `punch_out`. | Overnight 22:00-06:00, out 06:40 -> wrong/negative or TypeError. | Build the shift end relative to the attendance date and add a day for overnight shifts. |
| 6 | `create_employee` (77-85) | No unique index and no `DuplicateKeyError` handling: two concurrent creates for the same `emp_code` both pass the check-then-insert. Contract says the DB enforces uniqueness. | Two parallel `POST /employees` with the same `emp_code` both return 201. | Unique index on `employees.emp_code`; catch `DuplicateKeyError` -> 409. |
| 7 | `create_employee` (77-85) | No validation that `shift_start != shift_end`, and no request validation for `emp_code`, `email`, shifts or `joined_on`. | `shift_start == shift_end` is accepted; `POST` with `email: "x"` returns 201. | Pydantic field patterns/constraints plus a model validator (422). |
| 8 | `create_employee` (82) + responses | `created_at` is naive local `datetime.now()` and every response returns BSON datetimes, but the contract requires epoch milliseconds. | `GET /employees` returns `created_at` as a string/datetime object, not an int. | Store UTC-aware; convert to epoch ms at the boundary. |
| 9 | `list_employees` (93) | `skip = page * page_size` is off by one page. | With `page_size=3`, page 1 skips the first 3 employees. | `skip = (page - 1) * page_size`. |
| 10 | `list_employees` (94) | `total = count_documents({})` ignores the `department` filter. | `?department=Sales` reports `total` = all employees. | Count with the same filter (`q`). |
| 11 | `list_employees` (95) | No sort; contract requires `emp_code` ascending. | Items come back in insertion order. | `.sort("emp_code", 1)`. |
| 12 | `list_employees` (89) | `page`/`page_size` are unvalidated (`page=0`, `page_size=1000` accepted). | `?page=0&page_size=1000` returns a negative skip / oversized page. | FastAPI `Query(ge=1, le=100)` (R10). |
| 13 | `punch_in` (113) | Unknown `emp_code` -> `emp["shift_start"]` raises KeyError -> HTTP 500. | `POST /attendance/punch-in` with a made-up code returns 500, expected 404. | Look up the employee first and return 404. |
| 14 | `punch_in` (102) | `datetime.fromtimestamp(ms/1000)` is naive local time and is not truncated to whole seconds (R1). | Server in a non-IST zone stores the wrong wall time; sub-second kept. | Convert epoch ms to UTC-aware and truncate to seconds. |
| 15 | `punch_in` (63-66) | `punched_at` has no type/range validation, so seconds-looking values, floats and strings are accepted. | `"punched_at": 1783312500` or `1783312500.5` is accepted, expected 422. | Strict bounded integer `conint(ge=10**11, le=4102444800000, strict=True)`. |
| 16 | `punch_in` (103) | `date` is the naive-local `ts.date()`: wrong for IST and for overnight shifts (R1). | A 21:55 IST punch and a 05:30 IST punch both record the wrong day. | Compute the IST attendance date, with the overnight rule. |
| 17 | `punch_in` (104-118) | Check-then-insert is not race safe; two simultaneous punch-ins both insert. | Two parallel punch-ins for the same employee/date both get 201. | Unique index on `attendance_logs (emp_code, date)`; `DuplicateKeyError` -> 409. |
| 18 | `punch_in` (63-66) | Accepts any `status`, including `ABSENT`/`LEAVE` (which have no punch times). | `"status": "ABSENT"` creates a present-looking record. | Restrict to `PRESENT`/`WFH`/`ON_DUTY` (422 otherwise). |
| 19 | `list_attendance` (144-147) | Loads the whole collection into memory and sorts in Python. | ~100k records -> slow, high memory (contract forbids it). | Query with DB sort/skip/limit backed by an index. |
| 20 | `list_attendance` (145) | Sorts by `date` only; contract wants `date` desc then `emp_code` asc. | Two records on the same date come back in arbitrary order. | Compound sort in MongoDB. |
| 21 | `list_attendance` (149) | Adds an `id` field to every record; the contract has no `id` (records are keyed by `emp_code`+`date`). | Response contains an unexpected `id`. | Removed; serialize only contract fields. |
| 22 | `list_attendance` (148-149) | Returns raw BSON datetimes instead of epoch ms. | `punch_in` is not an integer. | Serialize datetimes to epoch ms. |
| 23 | `list_attendance` (133-143) | No `date_from > date_to` check and no `status` enum validation. | `?date_from=2026-07-06&date_to=2026-07-01` returns data, expected 422. | Validate and return 422. |
| 24 | `/health` (72-74) | Always returns 200 without pinging MongoDB. | Stop MongoDB -> `/health` still says ok; the runner needs 503. | Ping via `client.admin.command("ping")`, else 503. |
| 25 | module level (18, 22-23) | No indexes are created at startup, and `MongoClient` is not `tz_aware`, so BSON dates come back naive (the root of several tz bugs). | Explain shows COLLSCAN on 100k records; time math is ambiguous. | Create indexes idempotently at startup; open the client with `tz_aware=True`. |

## Looked at and decided was **not** a defect

* `load_dotenv()` order: by default it does not override existing variables, so real `MONGO_URI`/`MONGO_DB` already win over `.env` - correct.
* `status_code=201` on `POST /employees` and `POST /attendance/punch-in`, and 200 on punch-out - correct.
* `create_employee` returning the document with `_id` popped, and punch-in starting with `history: []`, `overtime_minutes: 0`, `half_day: false` - correct.
* The `IST = timezone(timedelta(hours=5, minutes=30))` constant: India has a fixed +05:30 offset, no DST, so a fixed offset is right.
* Pydantic's default "ignore extra fields" behaviour: derived fields sent by a client are ignored, as the contract asks.
* Field names and the page envelope (`items`, `total`, `page`, `page_size`) in the starter - already match the contract.
