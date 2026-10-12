"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel,StrictInt
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from decimal import Decimal, ROUND_HALF_UP

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]
db.employees.create_index("emp_code", unique=True)
db.attendance_logs.create_index(
    [("emp_code", 1), ("date", 1)],
    unique=True
)
IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    IST = timezone(timedelta(hours=5, minutes=30))

    local_punch = punch_in.astimezone(IST)

    h, m = map(int, shift_start.split(":"))

    shift_time = local_punch.replace(
        hour=h,
        minute=m,
        second=0,
        microsecond=0
    )

    # For early-morning punch-ins belonging to an overnight shift
    if local_punch < shift_time and (shift_time - local_punch).total_seconds() > 12 * 3600:
        shift_time -= timedelta(days=1)

    seconds_late = (local_punch - shift_time).total_seconds()

    if seconds_late <= 600:
        return 0

    return int(seconds_late // 60)

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (punch_out - punch_in).total_seconds()

    hours = Decimal(str(seconds)) / Decimal("3600")

    return float(
        hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )

def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    date_str: str,
    shift_start: str
) -> int:
    h, m = map(int, shift_end.split(":"))

    end = datetime.fromisoformat(date_str).replace(
        hour=h,
        minute=m,
        tzinfo=IST
    )

    # Overnight shift: shift ends on the following day
    if shift_end <= shift_start:
        end += timedelta(days=1)

    seconds_overtime = (
        punch_out.astimezone(IST) - end
    ).total_seconds()

    if seconds_overtime < 30 * 60:
        return 0

    return int(seconds_overtime // 60)
# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"
class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
class AttendanceCorrectionIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str
# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="MongoDB connection unavailable"
        )

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists"
        )

    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = 1,
    page_size: int = 20
):
    if page < 1 or not 1 <= page_size <= 100:
        raise HTTPException(
            status_code=422,
            detail="Invalid page or page_size"
        )

    q = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)

    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found"
        )

    IST = timezone(timedelta(hours=5, minutes=30))

    if body.punched_at is not None:
        if (
            type(body.punched_at) is not int
            or not 100000000000 <= body.punched_at <= 4102444800000
        ):
            raise HTTPException(
                status_code=422,
                detail="punched_at must be valid epoch milliseconds"
            )

    ts = (
        datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc)
        if body.punched_at is not None
        else datetime.now(timezone.utc)
    )

    ts = ts.replace(microsecond=0)

    local_ts = ts.astimezone(IST)
    attendance_date = local_ts.date()

    shift_start = datetime.strptime(emp["shift_start"], "%H:%M").time()
    shift_end = datetime.strptime(emp["shift_end"], "%H:%M").time()

    if shift_end <= shift_start and local_ts.time() < shift_end:
        attendance_date -= timedelta(days=1)

    d = attendance_date.isoformat()

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Already punched in for this date"
        )

    doc.pop("_id", None)
    return doc

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        if (
            type(body.punched_at) is not int
            or not 100000000000 <= body.punched_at <= 4102444800000
        ):
            raise HTTPException(
                422,
                "punched_at must be valid epoch milliseconds"
            )

    ts = (
        datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc)
        if body.punched_at is not None
        else datetime.now(timezone.utc)
    ).replace(microsecond=0)

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts}
        },
        sort=[("punch_in", -1)]
    )

    if record is None:
        raise HTTPException(404, "No matching punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")

    punch_in_time = record["punch_in"].replace(tzinfo=timezone.utc)
    seconds = (ts - punch_in_time).total_seconds()

    if seconds <= 0 or seconds > 24 * 3600:
        raise HTTPException(422, "Invalid punch-out time")

    work_hours = compute_work_hours(punch_in_time, ts)

    overtime = compute_overtime(
        ts,
        emp["shift_end"],
        record["date"],
        emp["shift_start"]
    )

    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": work_hours < 4.50
            }
        }
    )

    if result.modified_count == 0:
        raise HTTPException(409, "Already punched out")

    updated = db.attendance_logs.find_one(
        {"_id": record["_id"]},
        {"_id": 0}
    )

    return updated

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    if page < 1 or not 1 <= page_size <= 100:
        raise HTTPException(422, "Invalid page or page_size")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be after date_to")

    q = {}

    if emp_code:
        q["emp_code"] = emp_code

    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)

    items = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }

@app.patch("/attendance/{emp_code}/{date}")
def correct_attendance(emp_code: str, date: str, body: AttendanceCorrectionIn):
    from decimal import Decimal, ROUND_HALF_UP
    import re

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date
    })

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="Attendance record not found"
        )

    if not 5 <= len(body.reason.strip()) <= 200:
        raise HTTPException(422, "Reason must contain 5 to 200 characters")

    if not 1 <= len(body.regularized_by.strip()) <= 50:
        raise HTTPException(422, "Regularized_by must contain 1 to 50 characters")

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise HTTPException(422, "Invalid attendance date")

    employee = db.employees.find_one({"emp_code": emp_code})

    if employee is None:
        raise HTTPException(404, "Employee not found")

    valid_statuses = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
    present_statuses = {"PRESENT", "WFH", "ON_DUTY"}

    new_status = (
        body.status
        if "status" in body.model_fields_set
        else record.get("status")
    )

    if new_status not in valid_statuses:
        raise HTTPException(422, "Invalid attendance status")

    def normalize_datetime(value):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def parse_timestamp(value):
        if value is None:
            return None

        if type(value) is not int:
            raise HTTPException(422, "Timestamp must be an integer")

        if not 100000000000 <= value <= 4102444800000:
            raise HTTPException(422, "Invalid epoch milliseconds")

        return datetime.fromtimestamp(
            value / 1000,
            tz=timezone.utc
        ).replace(microsecond=0)

    old_punch_in = normalize_datetime(record.get("punch_in"))
    old_punch_out = normalize_datetime(record.get("punch_out"))

    new_punch_in = old_punch_in
    new_punch_out = old_punch_out

    if "punch_in" in body.model_fields_set:
        new_punch_in = parse_timestamp(body.punch_in)

    if "punch_out" in body.model_fields_set:
        new_punch_out = parse_timestamp(body.punch_out)

    if new_status in {"ABSENT", "LEAVE"}:
        if (
            ("punch_in" in body.model_fields_set and body.punch_in is not None)
            or
            ("punch_out" in body.model_fields_set and body.punch_out is not None)
        ):
            raise HTTPException(
                422,
                "ABSENT and LEAVE cannot have punch times"
            )

        new_punch_in = None
        new_punch_out = None

    if new_status in present_statuses and new_punch_in is None:
        raise HTTPException(422, "Present status requires punch-in")

    if new_punch_out is not None:
        if new_punch_in is None:
            raise HTTPException(422, "Punch-out requires punch-in")

        duration = (new_punch_out - new_punch_in).total_seconds()

        if duration <= 0 or duration > 86400:
            raise HTTPException(
                422,
                "Punch-out must be after punch-in and within 24 hours"
            )

    if new_punch_in is not None:
        local_time = new_punch_in.astimezone(IST)

        shift_start = datetime.strptime(
            employee["shift_start"], "%H:%M"
        ).time()

        shift_end = datetime.strptime(
            employee["shift_end"], "%H:%M"
        ).time()

        attendance_date = local_time.date()

        if shift_end <= shift_start and local_time.time() < shift_end:
            attendance_date -= timedelta(days=1)

        if attendance_date.isoformat() != date:
            raise HTTPException(
                422,
                "Punch-in does not match attendance date"
            )

    changes = {}

    for field, old_value, new_value in [
        ("status", record.get("status"), new_status),
        ("punch_in", old_punch_in, new_punch_in),
        ("punch_out", old_punch_out, new_punch_out)
    ]:
        if old_value != new_value:
            changes[field] = {
                "from": old_value,
                "to": new_value
            }

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="No attendance changes provided"
        )

    new_work_hours = None
    new_half_day = False
    new_late_minutes = 0
    new_overtime_minutes = 0

    if new_status in present_statuses:
        new_late_minutes = compute_late_minutes(
            new_punch_in,
            employee["shift_start"]
        )

        if new_punch_out is not None:
            new_work_hours = compute_work_hours(
                new_punch_in,
                new_punch_out
            )

            new_half_day = new_work_hours < 4.50

            new_overtime_minutes = compute_overtime(
                new_punch_out,
                employee["shift_end"],
                date,
                employee["shift_start"]
            )

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    update_data = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_work_hours,
        "half_day": new_half_day,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes
    }

    original_history = record.get("history", [])

    history_filter = (
        {"history": original_history}
        if "history" in record
        else {"history": {"$exists": False}}
    )

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            **history_filter
        },
        {
            "$set": update_data,
            "$push": {"history": history_entry}
        }
    )

    if result.matched_count == 0:
        raise HTTPException(
            status_code=409,
            detail="Attendance was modified by another request"
        )

    updated_record = db.attendance_logs.find_one(
        {"_id": record["_id"]},
        {"_id": 0}
    )

    return updated_record
@app.get("/analytics/employee/{emp_code}/monthly")
def employee_monthly_analytics(emp_code: str, month: str):
    import calendar
    from datetime import date as date_type

    try:
        year, month_num = map(int, month.split("-"))
        if (
            len(month) != 7
            or month[4] != "-"
            or not 1 <= month_num <= 12
        ):
            raise ValueError()

        first_day = date_type(year, month_num, 1)
        last_day = date_type(
            year,
            month_num,
            calendar.monthrange(year, month_num)[1]
        )

    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail="Month must be in YYYY-MM format"
        )

    employee = db.employees.find_one({"emp_code": emp_code})

    if employee is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found"
        )

    joined_on = employee["joined_on"]

    if isinstance(joined_on, datetime):
        joined_on = joined_on.date()
    elif isinstance(joined_on, str):
        joined_on = date_type.fromisoformat(joined_on)

    start_day = max(first_day, joined_on)

    workdays = 0
    current_day = start_day

    while current_day <= last_day:
        if current_day.weekday() < 5:
            workdays += 1
        current_day += timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": first_day.isoformat(),
                    "$lte": last_day.isoformat()
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"]
                                        ]
                                    },
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            [2, 3, 4, 5, 6]
                                        ]
                                    },
                                    {
                                        "$gte": [
                                            "$date",
                                            joined_on.isoformat()
                                        ]
                                    }
                                ]
                            },
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
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                }
            }
        }
    ]

    results = list(db.attendance_logs.aggregate(pipeline))

    summary = results[0] if results else {}

    present_days = summary.get("present_days", 0)

    attendance_pct = (
        float(
            (
                Decimal(str(present_days * 100))
                / Decimal(str(workdays))
            ).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP
            )
        )
        if workdays > 0
        else 0.0
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "workdays": workdays,
        "present_days": present_days,
        "leave_days": summary.get("leave_days", 0),
        "late_count": summary.get("late_count", 0),
        "total_late_minutes": summary.get("total_late_minutes", 0),
        "total_overtime_minutes": summary.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct
    }
@app.get("/analytics/departments/summary")
def department_summary(month: str):
    import calendar
    import re

    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "Month must be in YYYY-MM format")

    year, month_num = map(int, month.split("-"))
    first_day = datetime(year, month_num, 1)
    last_day = datetime(
        year, month_num,
        calendar.monthrange(year, month_num)[1]
    )

    month_start = first_day.strftime("%Y-%m-%d")
    month_end = last_day.strftime("%Y-%m-%d")

    pipeline = [
        {
            "$match": {
                "joined_on": {"$lte": month_end}
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employee_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": {
                                                            "$ifNull": ["$logs.date", month_start]
                                                        }
                                                    }
                                                }
                                            },
                                            [2, 3, 4, 5, 6]
                                        ]
                                    },
                                    {"$gte": ["$logs.date", "$joined_on"]}
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]},
                            0
                        ]
                    }
                },
                "completed_hours": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            None
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employee_codes"},
                "present_days": 1,
                "completed_hours": {
                    "$filter": {
                        "input": "$completed_hours",
                        "as": "hours",
                        "cond": {"$ne": ["$$hours", None]}
                    }
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {
            "$addFields": {
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$completed_hours"}, 0]},
                        {"$round": [{"$avg": "$completed_hours"}, 2]},
                        0
                    ]
                }
            }
        },
        {"$project": {"completed_hours": 0}},
        {"$sort": {"department": 1}}
    ]

    return list(db.employees.aggregate(pipeline))
@app.get("/analytics/late-leaderboard")
def late_leaderboard(
    month: str,
    department: Optional[str] = None,
    limit: int = 10
):
    import re
    import calendar

    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "Month must be in YYYY-MM format")

    if not 1 <= limit <= 100:
        raise HTTPException(422, "Limit must be between 1 and 100")

    year, month_num = map(int, month.split("-"))

    month_start = f"{year:04d}-{month_num:02d}-01"
    month_end = (
        f"{year:04d}-{month_num:02d}-"
        f"{calendar.monthrange(year, month_num)[1]:02d}"
    )

    employee_match = {
        "joined_on": {"$lte": month_end}
    }

    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {
            "$match": employee_match
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]}
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "total_late_minutes": {
                                "$sum": {
                                    "$ifNull": ["$late_minutes", 0]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                {"$ifNull": ["$late_minutes", 0]},
                                                0
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            }
                        }
                    }
                ],
                "as": "late_stats"
            }
        },
        {
            "$addFields": {
                "total_late_minutes": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$late_stats.total_late_minutes", 0]},
                        0
                    ]
                },
                "late_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$late_stats.late_count", 0]},
                        0
                    ]
                }
            }
        },
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
                "emp_code": 1
            }
        },
        {
            "$limit": limit
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
                "late_count": 1,
                "rank": 1
            }
        }
    ]

    return list(db.employees.aggregate(pipeline))
@app.get("/analytics/departments/{department}/trend")
def department_attendance_trend(
    department: str,
    start_date: str,
    end_date: str
):
    from datetime import date as date_type

    try:
        start = date_type.fromisoformat(start_date)
        end = date_type.fromisoformat(end_date)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must be in YYYY-MM-DD format"
        )

    if start > end:
        raise HTTPException(422, "start_date must be before end_date")

    if (end - start).days + 1 > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "Department not found")

    start_str = start.isoformat()
    end_str = end.isoformat()

    pipeline = [
        {
            "$match": {
                "department": department
            }
        },
        {
            "$group": {
                "_id": None,
                "employees": {
                    "$push": {
                        "emp_code": "$emp_code",
                        "joined_on": "$joined_on"
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "pipeline": [
                    {
                        "$match": {
                            "date": {
                                "$gte": start_str,
                                "$lte": end_str
                            }
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
                    {
                        "$match": {
                            "employee.department": department
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$project": {
                "employees": 1,
                "logs": 1,
                "dates": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        {
                                            "$dateDiff": {
                                                "startDate": {
                                                    "$dateFromString": {
                                                        "dateString": start_str
                                                    }
                                                },
                                                "endDate": {
                                                    "$dateFromString": {
                                                        "dateString": end_str
                                                    }
                                                },
                                                "unit": "day"
                                            }
                                        },
                                        1
                                    ]
                                }
                            ]
                        },
                        "as": "offset",
                        "in": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": {
                                    "$dateAdd": {
                                        "startDate": {
                                            "$dateFromString": {
                                                "dateString": start_str
                                            }
                                        },
                                        "unit": "day",
                                        "amount": "$$offset"
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
        {"$unwind": "$dates"},
        {
            "$addFields": {
                "day_logs": {
                    "$filter": {
                        "input": "$logs",
                        "as": "log",
                        "cond": {"$eq": ["$$log.date", "$dates"]}
                    }
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$employees",
                            "as": "emp",
                            "cond": {
                                "$lte": ["$$emp.joined_on", "$dates"]
                            }
                        }
                    }
                },
                "weekday": {
                    "$in": [
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {
                                    "dateString": "$dates"
                                }
                            }
                        },
                        [2, 3, 4, 5, 6]
                    ]
                }
            }
        },
        {
            "$addFields": {
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$day_logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {
                                                "$in": [
                                                    "$$log.status",
                                                    ["PRESENT", "WFH", "ON_DUTY"]
                                                ]
                                            },
                                            {
                                                "$lte": [
                                                    "$$log.employee.joined_on",
                                                    "$dates"
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {"$eq": ["$$log.half_day", True]},
                                            0.5,
                                            1
                                        ]
                                    },
                                    0
                                ]
                            }
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$day_logs",
                            "as": "log",
                            "cond": {
                                "$gt": [
                                    {"$ifNull": ["$$log.late_minutes", 0]},
                                    0
                                ]
                            }
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$weekday",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {
                            "$round": [
                                {
                                    "$divide": [
                                        "$present_count",
                                        "$headcount"
                                    ]
                                },
                                4
                            ]
                        },
                        None
                    ]
                }
            }
        },
        {
            "$sort": {
                "dates": 1
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"dates": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$dates",
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$round": ["$moving_avg_7d", 4]
                }
            }
        }
    ]

    return list(db.employees.aggregate(pipeline))
@app.get("/admin/explain")
def admin_explain(
    collection: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None
):
    import re
    import calendar

    if collection not in {"employees", "attendance_logs"}:
        raise HTTPException(
            status_code=422,
            detail="Invalid collection"
        )

    query = {}

    if emp_code is not None:
        query["emp_code"] = emp_code

    if month is not None:
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(
                status_code=422,
                detail="Month must be in YYYY-MM format"
            )

        year, month_num = map(int, month.split("-"))

        if collection == "attendance_logs":
            query["date"] = {
                "$gte": f"{year:04d}-{month_num:02d}-01",
                "$lte": (
                    f"{year:04d}-{month_num:02d}-"
                    f"{calendar.monthrange(year, month_num)[1]:02d}"
                )
            }

    try:
        explanation = db.command({
            "explain": {
                "find": collection,
                "filter": query
            },
            "verbosity": "executionStats"
        })

        return explanation

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=str(exc)
        )
# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
