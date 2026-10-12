"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (real environment variables win over a local .env)

Everything lives in this one file. The HTTP contract and the business rules
R1-R10 are defined in openapi.yaml; the stored shapes are defined in
DATA_MODEL.md. Derived fields (work_hours, late_minutes, overtime_minutes,
half_day) are written only by this API, and analytics read the stored values.
"""
import os
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, conint, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

# Real environment variables must win over .env: load_dotenv() does not override
# variables that already exist in the process environment.
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "attendance_db")

# tz_aware=True: BSON dates come back as UTC-aware datetimes. We never guess.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=20000)
db = client[MONGO_DB]
employees_col = db["employees"]
logs_col = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
STATUSES = ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"

# Epoch milliseconds: an integer in [1970-ish, 2100). Strict int rejects
# floats and strings; the bounds reject seconds-looking values and absurd ones.
EpochMs = conint(ge=100000000000, le=4102444800000, strict=True)


# --------------------------------------------------------------------------- #
# Startup: indexes (idempotent)
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    employees_col.create_index("emp_code", unique=True, name="uq_emp_code")
    employees_col.create_index("department", name="ix_emp_department")
    employees_col.create_index("joined_on", name="ix_emp_joined_on")

    # Natural key of a log; unique so concurrent punch-ins cannot both win.
    try:
        logs_col.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)],
            unique=True,
            name="uq_log_emp_date",
        )
    except Exception:
        logs_col.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)], name="ix_log_emp_date"
        )
    # List / trend / month scans sorted by date desc, emp_code asc.
    logs_col.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_log_date_emp")
    logs_col.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_log_status_date",
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Conversions and math helpers
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> UTC-aware datetime truncated to whole seconds (R1)."""
    return (EPOCH + timedelta(milliseconds=int(ms))).replace(microsecond=0)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.replace(microsecond=0)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def parse_hm(value: str) -> tuple:
    h, m = value.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    sh, sm = parse_hm(shift_start)
    eh, em = parse_hm(shift_end)
    return (eh, em) <= (sh, sm)


def round_half_up(value, places: int) -> float:
    """Half-up rounding (R4, R8) rather than banker's/float rounding."""
    if value is None:
        return None
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    quantum = Decimal(1).scaleb(-places)
    return float(d.quantize(quantum, rounding=ROUND_HALF_UP))


def round_half_up_expr(expr: dict, places: int) -> dict:
    """Same half-up rounding, as an aggregation expression (null propagates)."""
    factor = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, factor]}, 0.5]}}, factor]}


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date_cls:
    """R1: the IST calendar date a punch-in belongs to."""
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end):
        eh, em = parse_hm(shift_end)
        if (local.hour, local.minute, local.second) < (eh, em, 0):
            d = d - timedelta(days=1)
    return d


def _shift_start_dt(att_date: date_cls, shift_start: str) -> datetime:
    sh, sm = parse_hm(shift_start)
    return datetime(att_date.year, att_date.month, att_date.day, sh, sm, 0, tzinfo=IST)


def _shift_end_dt(att_date: date_cls, shift_start: str, shift_end: str) -> datetime:
    eh, em = parse_hm(shift_end)
    end = datetime(att_date.year, att_date.month, att_date.day, eh, em, 0, tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return end


def compute_late_minutes(punch_in: datetime, shift_start: str, att_date: date_cls) -> int:
    """R2: strictly more than 10:00 late -> floor minutes since shift_start."""
    start = _shift_start_dt(att_date, shift_start)
    elapsed = (punch_in.astimezone(IST) - start).total_seconds()
    if elapsed > 600:
        return int(elapsed // 60)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (out - in) hours, 2 decimals half-up."""
    seconds = Decimal((punch_out - punch_in).total_seconds())
    return round_half_up(seconds / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, att_date: date_cls) -> int:
    """R3: floor minutes past shift_end, only when >= 30."""
    end = _shift_end_dt(att_date, shift_start, shift_end)
    minutes = int((punch_out.astimezone(IST) - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: rounded work_hours < 4.50."""
    return work_hours is not None and work_hours < 4.50


def count_weekdays(start: date_cls, end: date_cls) -> int:
    """R7: Mon-Fri, inclusive."""
    if start > end:
        return 0
    days = (end - start).days + 1
    full_weeks, remainder = divmod(days, 7)
    total = full_weeks * 5
    for i in range(remainder):
        if (start + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            total += 1
    return total


def month_bounds(month: str) -> tuple:
    year, mon = map(int, month.split("-"))
    start = date_cls(year, mon, 1)
    end = date_cls(year + (1 if mon == 12 else 0), 1 if mon == 12 else mon + 1, 1) - timedelta(days=1)
    return start, end


# --------------------------------------------------------------------------- #
# Serialization: BSON -> HTTP (epoch ms)
# --------------------------------------------------------------------------- #
def serialize_history(entry: dict) -> dict:
    changes = {}
    for field, delta in (entry.get("changes") or {}).items():
        frm, to = delta.get("from"), delta.get("to")
        if field in ("punch_in", "punch_out"):
            frm, to = dt_to_ms(frm), dt_to_ms(to)
        changes[field] = {"from": frm, "to": to}
    return {
        "at": dt_to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [serialize_history(h) for h in (doc.get("history") or [])],
    }


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = {"extra": "ignore"}

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: date_cls

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = {"extra": "ignore"}

    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: str = Field(default="PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutIn(BaseModel):
    model_config = {"extra": "ignore"}

    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    model_config = {"extra": "ignore"}

    status: Optional[str] = Field(default=None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on.isoformat(),
        "created_at": now_utc(),
    }
    # Unique index is the real guarantee; the pre-check is only a fast path.
    if employees_col.find_one({"emp_code": body.emp_code}, {"_id": 1}):
        raise HTTPException(status_code=409, detail="emp_code already exists")
    try:
        employees_col.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if department is not None:
        query["department"] = department
    total = employees_col.count_documents(query)
    cursor = (
        employees_col.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance writes
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees_col.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="unknown employee")

    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    att_date = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": att_date.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], att_date),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        logs_col.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees_col.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="unknown employee")

    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    rec = logs_col.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        # No record qualifies. If the employee has any punch-in at all, the
        # timestamp is simply too early (422); otherwise there is nothing to close (404).
        if logs_col.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None}}, {"_id": 1}):
            raise HTTPException(
                status_code=422, detail="punched_at must be after punch_in"
            )
        raise HTTPException(status_code=404, detail="no punch-in found")

    punch_in = as_utc(rec["punch_in"])
    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")

    delta = (ts - punch_in).total_seconds()
    if delta <= 0 or delta > 24 * 3600:
        raise HTTPException(
            status_code=422, detail="punched_at must be after punch_in and within 24 hours"
        )

    att_date = date_cls.fromisoformat(rec["date"])
    work_hours = compute_work_hours(punch_in, ts)
    overtime = compute_overtime(ts, emp["shift_start"], emp["shift_end"], att_date)
    half_day = compute_half_day(work_hours)

    # Atomic guard: two simultaneous punch-outs -> exactly one winner.
    result = logs_col.update_one(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="already punched out")

    rec.update(
        {
            "punch_out": ts,
            "work_hours": work_hours,
            "overtime_minutes": overtime,
            "half_day": half_day,
        }
    )
    return serialize_attendance(rec)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    try:
        date_cls.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")

    emp = employees_col.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="unknown employee")

    rec = logs_col.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(status_code=404, detail="no record for that date")

    att_date = date_cls.fromisoformat(rec["date"])
    provided = body.model_fields_set

    cur_status = rec["status"]
    cur_pin = as_utc(rec.get("punch_in"))
    cur_pout = as_utc(rec.get("punch_out"))
    cur_work = rec.get("work_hours")
    cur_late = rec.get("late_minutes", 0)
    cur_ot = rec.get("overtime_minutes", 0)
    cur_hd = rec.get("half_day", False)

    new_status = body.status if ("status" in provided and body.status is not None) else cur_status
    pin_given = "punch_in" in provided and body.punch_in is not None
    pout_given = "punch_out" in provided and body.punch_out is not None
    new_pin = ms_to_dt(body.punch_in) if pin_given else cur_pin
    new_pout = ms_to_dt(body.punch_out) if pout_given else cur_pout

    if new_status in ("ABSENT", "LEAVE"):
        if pin_given or pout_given:
            raise HTTPException(
                status_code=422, detail="ABSENT/LEAVE records cannot have punch times"
            )
        new_pin, new_pout = None, None
        new_work, new_late, new_ot, new_hd = None, 0, 0, False
    else:
        if new_pin is None:
            raise HTTPException(status_code=422, detail="presence status requires punch_in")
        if attendance_date(new_pin, emp["shift_start"], emp["shift_end"]) != att_date:
            raise HTTPException(
                status_code=422, detail="punch_in must stay on the record's attendance date"
            )
        if new_pout is not None:
            delta = (new_pout - new_pin).total_seconds()
            if delta <= 0 or delta > 24 * 3600:
                raise HTTPException(
                    status_code=422, detail="punch_out must be after punch_in and within 24 hours"
                )
        new_late = compute_late_minutes(new_pin, emp["shift_start"], att_date)
        if new_pout is not None:
            new_work = compute_work_hours(new_pin, new_pout)
            new_ot = compute_overtime(new_pout, emp["shift_start"], emp["shift_end"], att_date)
            new_hd = compute_half_day(new_work)
        else:
            new_work, new_ot, new_hd = None, 0, False

    changes = {}
    for field, old, new in (
        ("status", cur_status, new_status),
        ("punch_in", cur_pin, new_pin),
        ("punch_out", cur_pout, new_pout),
        ("work_hours", cur_work, new_work),
        ("late_minutes", cur_late, new_late),
        ("overtime_minutes", cur_ot, new_ot),
        ("half_day", cur_hd, new_hd),
    ):
        if field in ("punch_in", "punch_out"):
            same = dt_to_ms(old) == dt_to_ms(new)
        else:
            same = old == new
        if not same:
            changes[field] = {"from": old, "to": new}

    if not changes:
        raise HTTPException(status_code=422, detail="request changes nothing")

    entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    # Optimistic guard on the times we read: a concurrent edit loses with 409
    # instead of silently dropping one of the two history entries.
    result = logs_col.update_one(
        {"_id": rec["_id"], "status": cur_status, "punch_in": cur_pin, "punch_out": cur_pout},
        {
            "$set": {
                "status": new_status,
                "punch_in": new_pin,
                "punch_out": new_pout,
                "work_hours": new_work,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_hd,
            },
            "$push": {"history": entry},
        },
    )
    if result.matched_count != 1:
        raise HTTPException(status_code=409, detail="record was modified concurrently")

    return serialize_attendance(logs_col.find_one({"_id": rec["_id"]}))


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    for value in (date_from, date_to):
        if value is not None:
            try:
                date_cls.fromisoformat(value)
            except ValueError:
                raise HTTPException(status_code=422, detail="dates must be YYYY-MM-DD")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")
    if status is not None and status not in STATUSES:
        raise HTTPException(status_code=422, detail="invalid status")

    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status

    total = logs_col.count_documents(query)
    cursor = (
        logs_col.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_attendance(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Analytics: shared aggregation expressions
# --------------------------------------------------------------------------- #
def _weekday_of(field: str) -> dict:
    return {"$dayOfWeek": {"$dateFromString": {"dateString": field}}}


def _present_weight(date_field: str, status_field: str, half_field: str) -> dict:
    """1 / 0.5 for a presence record on Mon-Fri, else 0 (R5-R7)."""
    return {
        "$cond": [
            {
                "$and": [
                    {"$in": [status_field, PRESENCE]},
                    {"$gte": [_weekday_of(date_field), 2]},
                    {"$lte": [_weekday_of(date_field), 6]},
                ]
            },
            {"$cond": [{"$ifNull": [half_field, False]}, 0.5, 1]},
            0,
        ]
    }


def _presence_weight(status_field: str, half_field: str) -> dict:
    """Presence weight regardless of weekday (used by the trend)."""
    return {
        "$cond": [
            {"$in": [status_field, PRESENCE]},
            {"$cond": [{"$ifNull": [half_field, False]}, 0.5, 1]},
            0,
        ]
    }


def _late_count_expr() -> dict:
    return {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}


def _has_work_hours() -> dict:
    return {
        "$and": [
            {"$in": ["$status", PRESENCE]},
            {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
        ]
    }


def _employee_monthly_pipeline(emp_code: str, month: str) -> list:
    start, end = month_bounds(month)
    start_s, end_s = start.isoformat(), end.isoformat()
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_s, "$lte": end_s}}},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": _present_weight("$date", "$status", "$half_day")
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": _late_count_expr(),
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def _department_summary_pipeline(month: str, department: Optional[str]) -> list:
    start, end = month_bounds(month)
    start_s, end_s = start.isoformat(), end.isoformat()

    emp_match = {"joined_on": {"$lte": end_s}}
    if department is not None:
        emp_match["department"] = department

    log_pipeline = [
        {"$match": {"date": {"$gte": start_s, "$lte": end_s}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.joined_on": {"$lte": end_s}}},
    ]
    if department is not None:
        log_pipeline.append({"$match": {"emp.department": department}})
    log_pipeline.append(
        {
            "$group": {
                "_id": "$emp.department",
                "present_days": {"$sum": _present_weight("$date", "$status", "$half_day")},
                "work_sum": {"$sum": {"$cond": [_has_work_hours(), {"$ifNull": ["$work_hours", 0]}, 0]}},
                "work_count": {"$sum": {"$cond": [_has_work_hours(), 1, 0]}},
                "late_count": _late_count_expr(),
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }
        }
    )

    return log_pipeline + [
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": emp_match},
                    {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
                ],
            }
        },
        {
            "$group": {
                "_id": "$_id",
                "headcount": {"$sum": {"$ifNull": ["$headcount", 0]}},
                "present_days": {"$sum": {"$ifNull": ["$present_days", 0]}},
                "work_sum": {"$sum": {"$ifNull": ["$work_sum", 0]}},
                "work_count": {"$sum": {"$ifNull": ["$work_count", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$late_count", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$total_late_minutes", 0]}},
                "leave_count": {"$sum": {"$ifNull": ["$leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$on_duty_count", 0]}},
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": 1}},
    ]


def _late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    start, end = month_bounds(month)
    start_s, end_s = start.isoformat(), end.isoformat()

    pipeline = [
        {"$match": {"date": {"$gte": start_s, "$lte": end_s}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ]
    return pipeline


def _trend_pipeline(department: str, from_d: date_cls, to_d: date_cls) -> list:
    from_s, to_s = from_d.isoformat(), to_d.isoformat()
    from_dt = datetime(from_d.year, from_d.month, from_d.day, tzinfo=UTC)
    # $densify's range upper bound is exclusive, so go one day past `to`.
    to_dt = datetime(to_d.year, to_d.month, to_d.day, tzinfo=UTC) + timedelta(days=1)

    return [
        # Anchor so $densify always has at least one document in range.
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "_id": {"$literal": from_s},
                "present_count": {"$literal": 0},
                "late_count": {"$literal": 0},
            }
        },
        {
            "$unionWith": {
                "coll": "attendance_logs",
                "pipeline": [
                    {"$match": {"date": {"$gte": from_s, "$lte": to_s}}},
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp",
                        }
                    },
                    {"$unwind": "$emp"},
                    {"$match": {"emp.department": department}},
                    {
                        "$group": {
                            "_id": "$date",
                            "present_count": {
                                "$sum": _presence_weight("$status", "$half_day")
                            },
                            "late_count": _late_count_expr(),
                        }
                    },
                ],
            }
        },
        {
            "$group": {
                "_id": "$_id",
                "present_count": {"$sum": {"$ifNull": ["$present_count", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$late_count", 0]}},
            }
        },
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        {
            "$densify": {
                "field": "day",
                "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt]},
            }
        },
        {
            "$set": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$day"},
                "pipeline": [
                    {"$match": {"department": department}},
                    {
                        "$match": {
                            "$expr": {
                                "$lte": [
                                    "$joined_on",
                                    {"$dateToString": {"date": "$$day", "format": "%Y-%m-%d"}},
                                ]
                            }
                        }
                    },
                    {"$count": "n"},
                ],
                "as": "hc",
            }
        },
        {
            "$set": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
            }
        },
        {"$project": {"hc": 0}},
        {
            "$set": {
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$day"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$day"}, 6]},
                    ]
                }
            }
        },
        {
            "$set": {
                # R8: the reported rate is already 4-decimal half-up, and the moving
                # average is taken over these reported values.
                "attendance_rate": round_half_up_expr(
                    {
                        "$cond": [
                            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                            {"$divide": ["$present_count", "$headcount"]},
                            None,
                        ]
                    },
                    4,
                )
            }
        },
        {"$sort": {"day": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}}}},
    ]


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=MONTH_PATTERN)):
    emp = employees_col.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="unknown employee")

    start, end = month_bounds(month)
    agg = list(logs_col.aggregate(_employee_monthly_pipeline(emp_code, month)))
    row = agg[0] if agg else {}

    joined = date_cls.fromisoformat(emp["joined_on"])
    wd_start = max(joined, start)
    working_days = count_weekdays(wd_start, end) if wd_start <= end else 0

    present_days = round_half_up(Decimal(str(row.get("present_days", 0) or 0)), 2)
    attendance_pct = (
        round_half_up(Decimal(str(present_days)) / Decimal(working_days) * Decimal(100), 2)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(row.get("leave_days", 0) or 0),
        "late_count": int(row.get("late_count", 0) or 0),
        "total_late_minutes": int(row.get("total_late_minutes", 0) or 0),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0) or 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=MONTH_PATTERN),
    department: Optional[str] = None,
):
    rows = list(logs_col.aggregate(_department_summary_pipeline(month, department)))
    items = []
    for row in rows:
        work_count = int(row.get("work_count", 0) or 0)
        avg = (
            round_half_up(Decimal(str(row.get("work_sum", 0) or 0)) / Decimal(work_count), 2)
            if work_count > 0
            else None
        )
        items.append(
            {
                "department": row["_id"],
                "headcount": int(row.get("headcount", 0) or 0),
                "present_days": round_half_up(row.get("present_days", 0) or 0, 2),
                "avg_work_hours": avg,
                "late_count": int(row.get("late_count", 0) or 0),
                "total_late_minutes": int(row.get("total_late_minutes", 0) or 0),
                "leave_count": int(row.get("leave_count", 0) or 0),
                "on_duty_count": int(row.get("on_duty_count", 0) or 0),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    rows = list(logs_col.aggregate(_late_leaderboard_pipeline(month, limit, department)))
    items = [
        {
            "rank": int(row["rank"]),
            "emp_code": row["_id"],
            "name": row["emp"]["name"],
            "department": row["emp"]["department"],
            "total_late_minutes": int(row.get("total_late_minutes", 0) or 0),
            "late_count": int(row.get("late_count", 0) or 0),
        }
        for row in rows
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(alias="from"),
    to: str = Query(alias="to"),
):
    try:
        from_d = date_cls.fromisoformat(from_)
        to_d = date_cls.fromisoformat(to)
    except ValueError:
        raise HTTPException(status_code=422, detail="from/to must be YYYY-MM-DD")
    if to_d < from_d:
        raise HTTPException(status_code=422, detail="to must not be before from")
    if (to_d - from_d).days + 1 > 92:
        raise HTTPException(status_code=422, detail="range exceeds 92 days")
    if not employees_col.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="unknown department")

    # The pipeline is anchored on the employees collection (its first stage matches
    # `department`, and the anchor guarantees $densify a starting document), then
    # unions the attendance_logs stream. This is also what /admin/explain runs.
    rows = list(employees_col.aggregate(_trend_pipeline(department, from_d, to_d)))
    items = []
    for row in rows:
        rate = row.get("attendance_rate")
        moving = row.get("moving_avg_7d")
        items.append(
            {
                "date": row["date"],
                "is_working_day": bool(row["is_working_day"]),
                "headcount": int(row.get("headcount", 0) or 0),
                "present_count": round_half_up(row.get("present_count", 0) or 0, 2),
                "late_count": int(row.get("late_count", 0) or 0),
                "attendance_rate": round_half_up(rate, 4) if rate is not None else None,
                "moving_avg_7d": round_half_up(moving, 4) if moving is not None else None,
            }
        )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
def _attendance_list_command(
    emp_code, date_from, date_to, status, page, page_size
) -> dict:
    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status
    return {
        "find": "attendance_logs",
        "filter": query,
        "sort": {"date": -1, "emp_code": 1},
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        command = _attendance_list_command(emp_code, date_from, date_to, status, page, page_size)
        explain = db.command("explain", command, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        pipeline = _employee_monthly_pipeline(emp_code, month)
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = _department_summary_pipeline(month, department)
        collection = "attendance_logs"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = _late_leaderboard_pipeline(month, limit, department)
        collection = "attendance_logs"
    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        try:
            from_d = date_cls.fromisoformat(from_)
            to_d = date_cls.fromisoformat(to)
        except ValueError:
            raise HTTPException(status_code=422, detail="from/to must be YYYY-MM-DD")
        pipeline = _trend_pipeline(department, from_d, to_d)
        collection = "employees"
    else:
        raise HTTPException(status_code=422, detail="unknown endpoint")

    explain = db.command(
        "explain",
        {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats",
    )
    return {"endpoint": endpoint, "collection": collection, "explain": explain}
