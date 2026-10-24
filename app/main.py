"""Employee Attendance & Analytics API.

Run from the candidate_kit directory:
    python -m uvicorn app.main:app --reload --port 8000
Environment: MONGO_URI, MONGO_DB (loaded from .env in the working directory).
"""
import json
from bson import json_util
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI or "mongodb://localhost:27017", serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENT_STATUSES | {"ABSENT", "LEAVE"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

# ---------------------------- common helpers ----------------------------
def now_utc():
    return datetime.now(UTC).replace(microsecond=0)

def decimal_round(value, places=2):
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))

def parse_date(value: str, field="date") -> date:
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{field} must use YYYY-MM-DD format")

def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-\d{2}", value or ""):
        raise HTTPException(422, "month must use YYYY-MM format")
    try:
        first = date.fromisoformat(value + "-01")
    except ValueError:
        raise HTTPException(422, "month must use YYYY-MM format")
    next_month = date(first.year + (first.month == 12), 1 if first.month == 12 else first.month + 1, 1)
    return first, next_month - timedelta(days=1)

def parse_hhmm(value: str, field: str):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise HTTPException(422, f"{field} must use HH:MM format")
    h, m = map(int, value.split(":"))
    return h, m

def epoch_ms_to_utc(value):
    # Strict integer epoch milliseconds; reject seconds-looking and floats/strings.
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(422, "punched_at must be an integer epoch-milliseconds value")
    if abs(value) < 100_000_000_000:
        raise HTTPException(422, "punched_at must be epoch milliseconds, not seconds")
    try:
        dt = datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        raise HTTPException(422, "punched_at is out of range")
    return dt

def to_ms(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return int(value.timestamp() * 1000)
    return value

def serialize(value):
    if isinstance(value, datetime):
        return to_ms(value)
    if isinstance(value, dict):
        return {k: serialize(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, list):
        return [serialize(v) for v in value]
    return value

def attendance_day(punch_in_utc: datetime, shift_start: str, shift_end: str) -> date:
    local = punch_in_utc.astimezone(IST)
    sh, sm = parse_hhmm(shift_start, "shift_start")
    eh, em = parse_hhmm(shift_end, "shift_end")
    # For overnight shifts, early punches before the shift end belong to prior day.
    if (eh, em) <= (sh, sm) and (local.hour, local.minute) < (eh, em):
        return local.date() - timedelta(days=1)
    return local.date()


def late_minutes(punch_in_utc, shift_start):
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=UTC)

    local = punch_in_utc.astimezone(IST)
    h, m = parse_hhmm(shift_start, "shift_start")
    start = local.replace(
        hour=h, minute=m, second=0, microsecond=0
    )

    delta = int((local - start).total_seconds() // 60)
    return delta if delta > 10 else 0


def derive_fields(punch_in, punch_out, shift_start, shift_end, day_string):
    if not punch_in or not punch_out:
        return {"work_hours": None, "late_minutes": late_minutes(punch_in, shift_start) if punch_in else 0,
                "overtime_minutes": 0, "half_day": False}
    seconds = (punch_out - punch_in).total_seconds()
    if seconds <= 0 or seconds > 24 * 3600:
        raise HTTPException(422, "punch_out must be after punch_in and no more than 24 hours later")
    work_hours = decimal_round(seconds / 3600, 2)
    h, m = parse_hhmm(shift_end, "shift_end")
    local_in = punch_in.astimezone(IST)
    shift_end_local = datetime.combine(date.fromisoformat(day_string), time(h, m), tzinfo=IST)
    if (h, m) <= tuple(map(int, shift_start.split(":"))):
        shift_end_local += timedelta(days=1)
    out_local = punch_out.astimezone(IST)
    overtime_raw = max(0, int((out_local - shift_end_local).total_seconds() // 60))
    overtime = overtime_raw if overtime_raw >= 30 else 0
    return {"work_hours": work_hours, "late_minutes": late_minutes(punch_in, shift_start),
            "overtime_minutes": overtime, "half_day": work_hours < 4.5}

def clean_attendance(doc):
    doc = dict(doc)
    doc.pop("_id", None)
    doc["history"] = doc.get("history") or []
    doc["half_day"] = doc.get("half_day", False)
    return serialize(doc)

def ensure_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employees_emp_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employees_department_emp_code")
    db.employees.create_index([("joined_on", ASCENDING)], name="ix_employees_joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_emp_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_emp")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)], name="ix_attendance_status_date")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_emp_punchin")

@app.on_event("startup")
def startup():
    try:
        ensure_indexes()
    except PyMongoError:
        # Keep /health available even if MongoDB is not configured; DB routes will report errors.
        pass

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )


# ------------------------------- models ---------------------------------
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=150)
    email: str = Field(min_length=3, max_length=254)
    department: str = Field(min_length=1, max_length=100)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str
    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, v):
        parse_date(v, "joined_on"); return v
    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift(cls, v, info):
        parse_hhmm(v, info.field_name); return v
    @model_validator(mode="after")
    def different_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self
    @field_validator("email")
    @classmethod
    def email_shape(cls, v):
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", v): raise ValueError("invalid email")
        return v

class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    punched_at: Optional[int] = None

class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=1, max_length=500)
    regularized_by: str = Field(min_length=1, max_length=150)

# ------------------------------- employees -----------------------------
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    except PyMongoError as exc:
        raise HTTPException(503, f"Database error: {exc.__class__.__name__}")
    doc.pop("_id", None)
    return serialize(doc)

@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    try:
        total = db.employees.count_documents(q)
        items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))
    except PyMongoError as exc:
        raise HTTPException(503, f"Database error: {exc.__class__.__name__}")
    return {"items": serialize(items), "total": total, "page": page, "page_size": page_size}

# ------------------------------ attendance -----------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    ts = epoch_ms_to_utc(body.punched_at) if body.punched_at is not None else now_utc()
    d = attendance_day(ts, emp["shift_start"], emp["shift_end"]).isoformat()
    doc = {"emp_code": body.emp_code, "date": d, "status": body.status, "punch_in": ts,
           "punch_out": None, "work_hours": None, "late_minutes": late_minutes(ts, emp["shift_start"]),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this attendance date")
    doc.pop("_id", None)
    return clean_attendance(doc)

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = (
        epoch_ms_to_utc(body.punched_at)
        if body.punched_at is not None
        else now_utc()
    )

    rec = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not rec:
        raise HTTPException(404, "no punch-in found")

    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    stored_punch_in = rec["punch_in"]
    if stored_punch_in.tzinfo is None:
        stored_punch_in = stored_punch_in.replace(tzinfo=UTC)

    if (
        ts <= stored_punch_in
        or ts > stored_punch_in + timedelta(hours=24)
    ):
        raise HTTPException(
            422,
            "punched_at must be after punch_in and within 24 hours",
        )

    derived = derive_fields(
        stored_punch_in,
        ts,
        emp["shift_start"],
        emp["shift_end"],
        rec["date"],
    )

    result = db.attendance_logs.update_one(
        {
            "_id": rec["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts,
                **derived,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            409,
            "attendance record changed concurrently",
        )

    # Return the updated record, not the old database snapshot.
    rec["punch_in"] = stored_punch_in
    rec.update({
        "punch_out": ts,
        **derived,
    })

    return clean_attendance(rec)

@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {}
    if emp_code is not None: q["emp_code"] = emp_code
    if status is not None:
        if status not in ALL_STATUSES: raise HTTPException(422, "invalid status")
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from: parse_date(date_from, "date_from"); q["date"]["$gte"] = date_from
        if date_to: parse_date(date_to, "date_to"); q["date"]["$lte"] = date_to
        if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")
    try:
        total = db.attendance_logs.count_documents(q)
        docs = list(db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size))
    except PyMongoError as exc: raise HTTPException(503, f"Database error: {exc.__class__.__name__}")
    return {"items": [clean_attendance(d) for d in docs], "total": total, "page": page, "page_size": page_size}



@app.patch("/attendance/{emp_code}/{day}")
def regularize(emp_code: str, day: str, body: RegularizeIn):
    parse_date(day, "date")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    rec = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": day}
    )
    if not rec:
        raise HTTPException(404, "attendance record not found")

    updates = {}
    fields_set = body.model_fields_set

    if "status" in fields_set and body.status is not None:
        updates["status"] = body.status

    for field in ("punch_in", "punch_out"):
        if field in fields_set:
            value = getattr(body, field)
            updates[field] = (
                epoch_ms_to_utc(value) if value is not None else None
            )

    def normalize_dt(value):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    new_status = updates.get("status", rec["status"])

    if new_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in fields_set or "punch_out" in fields_set:
            raise HTTPException(
                422, "ABSENT/LEAVE cannot be sent with punch fields"
            )

        updates.update({
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        })

    else:
        pi = normalize_dt(
            updates.get("punch_in", rec.get("punch_in"))
        )
        po = normalize_dt(
            updates.get("punch_out", rec.get("punch_out"))
        )

        if pi is None:
            raise HTTPException(
                422, "presence status requires punch_in"
            )

        local_pi = pi.astimezone(IST)
        if local_pi.date().isoformat() != day:
            raise HTTPException(
                422, "punch_in must fall on the attendance date"
            )

        updates["punch_in"] = pi
        updates["punch_out"] = po

        if po is not None:
            derived = derive_fields(
                pi, po, emp["shift_start"], emp["shift_end"], day
            )
        else:
            derived = {
                "work_hours": None,
                "late_minutes": late_minutes(pi, emp["shift_start"]),
                "overtime_minutes": 0,
                "half_day": False,
            }

        updates.update(derived)

    changed = {}
    for key, value in updates.items():
        old_value = rec.get(key)

        if isinstance(old_value, datetime):
            old_value = normalize_dt(old_value)

        if old_value != value:
            changed[key] = value

    if not changed:
        raise HTTPException(
            422, "regularization makes no changes"
        )

  

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {
            key: {
                "from": serialize(rec.get(key)),
                "to": serialize(value),
            }
            for key, value in changed.items()
        },
    }

    result = db.attendance_logs.update_one(
    {
        "_id": rec["_id"],
        "history": rec.get("history", []),
        **{
            key: rec.get(key)
            for key in changed
        },
    },
    {
        "$set": changed,
        "$push": {"history": history_entry},
    },
)

    if result.modified_count != 1:
        raise HTTPException(
            409, "attendance record changed concurrently"
        )

    rec.update(changed)
    rec.setdefault("history", []).append(history_entry)

    return clean_attendance(rec)


    if result.modified_count != 1:
        raise HTTPException(
            409, "attendance record changed concurrently"
        )

    rec.update(changed)
    rec.setdefault("history", []).append(history_entry)

    return clean_attendance(rec)


# ------------------------------ analytics ------------------------------
def month_match(first, last, extra=None):
    q = {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}
    if extra: q.update(extra)
    return q

def present_day_expr():
    return {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]}, {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]}]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    first, last = parse_month(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    joined = parse_date(emp["joined_on"], "joined_on")
    working_days = 0
    d = max(first, joined)
    while d <= last:
        if d.weekday() < 5: working_days += 1
        d += timedelta(days=1)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": present_day_expr()},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            "total_work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}}}}]
    agg = list(db.attendance_logs.aggregate(pipeline))
    data = agg[0] if agg else {}
    pdays = float(data.get("present_days", 0))
    pct = decimal_round(pdays / working_days * 100, 2) if working_days else None
    return {"emp_code": emp_code, "name": emp["name"], "department": emp["department"], "month": month,
            "working_days": working_days, "present_days": pdays, "leave_days": data.get("leave_days", 0),
            "late_count": data.get("late_count", 0), "total_late_minutes": data.get("total_late_minutes", 0),
            "total_overtime_minutes": data.get("total_overtime_minutes", 0),
            "total_work_hours": decimal_round(data.get("total_work_hours", 0), 2), "attendance_pct": pct}

@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    first, last = parse_month(month)
    emp_match = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None: emp_match["department"] = department
    pipeline = [
        {"$match": emp_match},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "codes": {"$addToSet": "$emp_code"}}},
        {"$lookup": {"from": "attendance_logs", "let": {"codes": "$codes"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$in": ["$emp_code", "$$codes"]}, {"$gte": ["$date", first.isoformat()]}, {"$lte": ["$date", last.isoformat()]}]}}},
            {"$group": {"_id": None, "avg_work_hours": {"$avg": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$ne": ["$work_hours", None]}]}, "$work_hours", None]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}}}}], "as": "stats"}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "stats": {"$arrayElemAt": ["$stats", 0]}}},
        {"$sort": {"department": 1}}]
    rows = list(db.employees.aggregate(pipeline))
    result = []
    for row in rows:
        s = row.get("stats") or {}
        result.append({"department": row["department"], "headcount": row["headcount"], "avg_work_hours"
        "": decimal_round(s["avg_work_hours"], 2) if s.get("avg_work_hours") is not None else None,
                       "late_count": s.get("late_count", 0), "total_late_minutes": s.get("total_late_minutes", 0),
                       "leave_count": s.get("leave_count", 0), "on_duty_count": s.get("on_duty_count", 0)})
    return {"month": month, "items": result}



@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    first, last = parse_month(month)

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": first.isoformat(),
                    "$lte": last.isoformat()
                },
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee"
            }
        },
        {"$unwind": "$employee"}
    ]

    if department is not None:
        pipeline.append({
            "$match": {"employee.department": department}
        })

 
    pipeline += [
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
               {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1
            }
        },
        {
            "$limit": limit
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
                "rank": 1
            }
        }
    ]


    return {
        "month": month,
        "items": list(db.attendance_logs.aggregate(pipeline))
    }





@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to")
):
    start = parse_date(from_date, "from")
    end = parse_date(to_date, "to")

    if end < start:
        raise HTTPException(422, "to must be on or after from")

    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")

    pipeline = [
        {
            "$documents": [
                {
                    "date": {
                        "$dateAdd": {
                            "startDate": {
                                "$dateFromString": {
                                    "dateString": from_date
                                }
                            },
                            "unit": "day",
                            "amount": i
                        }
                    }
                }
                for i in range((end - start).days + 1)
            ]
        },
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date"
                    }
                },
                "weekday": {"$dayOfWeek": "$date"}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day"]}
                                ]
                            }
                        }
                    },
                    {"$count": "total"}
                ],
                "as": "headcount_data"
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$headcount_data.total", 0]},
                        0
                    ]
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$date", "$$day"]}
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "employee"
                        }
                    },
                    {"$unwind": "$employee"},
                    {"$match": {"employee.department": department}},
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", list(PRESENT_STATUSES)]},
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1
                                            ]
                                        },
                                        0
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": ["$late_minutes", 0]},
                                        1,
                                        0
                                    ]
                                }
                            }
                        }
                    }
                ],
                "as": "daily_counts"
            }
        },
        {
            "$set": {
                "present_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$daily_counts.present_count", 0]},
                        0
                    ]
                },
                "late_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$daily_counts.late_count", 0]},
                        0
                    ]
                },
                "is_working_day": {"$and": [
                    {"$gte": ["$weekday", 2]},
                    {"$lte": ["$weekday", 6]}
                ]}
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$round": [
                            {"$multiply": [
                                {"$divide": ["$present_count", "$headcount"]},
                                100
                            ]},
                            4
                        ]},
                        None
                    ]
                }
            }
        },
        {"$sort": {"date": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]}
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$moving_avg_7d", None]},
                        None,
                        {"$round": ["$moving_avg_7d", 4]}
                    ]
                }
            }
        }
    ]

    result = list(db.aggregate(pipeline))

    return {
        "department": department,
        "from": from_date,
        "to": to_date,
        "items": result
    }


# ----------------------------- explain plans ----------------------------
@app.get("/admin/explain/{endpoint}")
def explain(endpoint: str, month: Optional[str] = None, emp_code: Optional[str] = None,
            department: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
            limit: int = Query(10, ge=1, le=50), page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if endpoint == "attendance_list":
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from: parse_date(date_from, "date_from"); q["date"]["$gte"] = date_from
            if date_to: parse_date(date_to, "date_to"); q["date"]["$lte"] = date_to
            if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")
        pipeline = [{"$match": q}, {"$sort": {"date": -1, "emp_code": 1}}, {"$skip": (page-1)*page_size}, {"$limit": page_size}]
        coll = db.attendance_logs
    elif endpoint in {"employee_monthly", "department_summary", "late_leaderboard"}:
        if not month: raise HTTPException(422, "month is required for this endpoint")
        first, last = parse_month(month)
        if endpoint == "employee_monthly":
            if not emp_code: raise HTTPException(422, "emp_code is required for this endpoint")
            pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}}, {"$group": {"_id": "$emp_code", "count": {"$sum": 1}}}]
            coll = db.attendance_logs
        elif endpoint == "department_summary":
            match = {"joined_on": {"$lte": last.isoformat()}}
            if department: match["department"] = department
            pipeline = [{"$match": match}, {"$group": {"_id": "$department", "headcount": {"$sum": 1}}}, {"$sort": {"_id": 1}}]
            coll = db.employees
        else:
            pipeline = [{"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}}, {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}}, {"$sort": {"total_late_minutes": -1, "_id": 1}}, {"$limit": limit}]
            coll = db.attendance_logs
    elif endpoint == "department_trend":
        if not department or not date_from or not date_to: raise HTTPException(422, "department, date_from and date_to are required")
        start, end = parse_date(date_from, "date_from"), parse_date(date_to, "date_to")
        if end < start or (end-start).days + 1 > 92: raise HTTPException(422, "invalid trend date range")
        pipeline = [{"$match": {"date": {"$gte": date_from, "$lte": date_to}}}, {"$sort": {"date": 1, "emp_code": 1}}]
        coll = db.attendance_logs
    else:
        raise HTTPException(422, "endpoint must be attendance_list, employee_monthly, department_summary, late_leaderboard, or department_trend")
    try:
        explanation = coll.database.command("explain", {"aggregate": coll.name, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
    except PyMongoError as exc:
        raise HTTPException(503, f"Explain failed: {exc.__class__.__name__}")

    return json.loads(json_util.dumps({
        "endpoint": endpoint,
        "collection": coll.name,
        "explain": explanation
    }))
