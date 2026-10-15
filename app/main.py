"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from calendar import Calendar, monthrange
from datetime import datetime, timedelta, timezone
from datetime import date as Date, time
from typing import Optional, Literal, Annotated
from decimal import Decimal, ROUND_HALF_UP

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from contextlib import asynccontextmanager

from bson import json_util
import json


load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
AttendanceStatus = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE",]
EpochMillis = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
explain_options = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def ms_to_dt(ms: int) -> datetime:
    # truncates to whole seconds (R1)
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc).astimezone(IST)

def dt_to_ms(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000

def now_ist() -> datetime:
    return datetime.now(IST).replace(microsecond=0)

def round_half_up(value, places="0.01"):
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP))

def month_range(month: str) -> tuple[str, str]:
    year, mon = map(int, month.split("-"))
    start = Date(year, mon, 1)
    end = start + timedelta(monthrange(year, mon)[1] - 1)
    return start.isoformat(), end.isoformat()

def require(**params):
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise HTTPException(422, f"Missing required parameter(s): {', '.join(missing)}")

def serialize_record(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k not in ("_id", "updated_at")}
    out["punch_in"] = dt_to_ms(out.get("punch_in"))
    out["punch_out"] = dt_to_ms(out.get("punch_out"))
    history = []
    for h in out.get("history", []):
        changes = {}
        for k, c in h["changes"].items():
            if k in ("punch_in", "punch_out"):
                c = {"from": dt_to_ms(c["from"]), "to": dt_to_ms(c["to"])}
            changes[k] = c
        history.append({"at": dt_to_ms(h["at"]), "by": h["by"],
                        "reason": h["reason"], "changes": changes})
    out["history"] = history
    return out

def normalize(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)

def comparable_value(key, value):
    if key in {"punch_in", "punch_out"} and value is not None:
        return normalize(value)
    return value

def count_working_days(year: int, month: int, joined_on: str) -> int:
    joined = Date.fromisoformat(joined_on)
    last = monthrange(year, month)[1]
    if joined > Date(year, month, last):
        return 0
    start = joined.day if (joined.year, joined.month) == (year, month) else 1
    return sum(1 for d in range(start, last + 1) if Date(year, month, d).weekday() < 5)

def parse_attendance_date(value: str) -> Date:
    try:
        parsed = Date.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="date must be in YYYY-MM-DD format",
        )

    if parsed.isoformat() != value:
        raise HTTPException(
            status_code=422,
            detail="date must be in YYYY-MM-DD format",
        )

    return parsed

def build_attendance_list(emp_code, date_from, date_to, status, page, page_size):
    for d in (date_from, date_to):
        if d is not None:
            parse_attendance_date(d)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
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
    return {"find": "attendance_logs", "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size, "limit": page_size}

def build_employee_monthly(emp_code, start_str, end_str):
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_str, "$lte": end_str}}},
        {"$addFields": {
            "_dow": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}},
            "_present": {"$in": ["$status", PRESENCE]},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": ["$_present", {"$lte": ["$_dow", 5]}]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    return {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}

def build_department_summary(department, start_date_str, end_date_str):
    employee_match = {"joined_on": {"$lte": end_date_str}}
    if department is not None:
        employee_match["department"] = department
        
    pipeline = [
        {
            "$match": employee_match
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
                                    {"$gte": ["$date", start_date_str]},
                                    {"$lte": ["$date", end_date_str]}
                                ]
                            }
                        }
                    },
                    {
                        "$set": {
                            "_is_weekday": {"$lte": [
                                {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5
                            ]},
                            "_is_present": {
                                "$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    "$logs._is_weekday",
                                    "$logs._is_present"
                                ]
                            },
                            {
                                "$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]
                            },
                            0
                        ]
                    }
                },

                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    "$logs._is_present",
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            0
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    "$logs._is_present",
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },

                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}
                                ],
                            },
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
            "$set": {
                "headcount": {"$size": "$headcount"},
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                        None
                    ]
                }
            }
        },
        {
            "$match": {"headcount": {"$gt": 0}}
        },
        {
            "$sort": {"_id": 1}
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        }
    ]
    return {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}

def build_late_leaderboard(department, limit, start_str, end_str):

    employee_match = {"joined_on": {"$lte": end_str}}
    if department is not None:
        employee_match["department"] = department
    pipeline = [
            {"$match": employee_match},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", start_str]},
                                    {"$lte": ["$date", end_str]},
                                    ]
                                }
                            }
                        },
                        {
                            "$project": {"_id": 0, "late_minutes": 1}
                        },
                    ],
                    "as": "logs",
                }
            },
            {
                "$set": {
                    "total_late_minutes": {"$sum": "$logs.late_minutes"},
                    "late_count": {"$size": {"$filter": {"input": "$logs", "cond": {"$gt": ["$$this.late_minutes", 0]}}}}
                }
            },
            {
                "$match": {
                    "total_late_minutes": {"$gt": 0}
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {
                            "$rank": {}
                        }
                    },
                }
                },
            {
                "$match": {
                    "rank": {"$lte": limit}
                }
            },
            {
                "$sort": {"total_late_minutes": -1, "emp_code": 1}
            },
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": 1,
                    "name": 1,
                    "department": 1,
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    
    
    return {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}

def build_department_trend(department, from_day, to_day):
    try:
        start_date = Date.fromisoformat(from_day)
        end_date = Date.fromisoformat(to_day)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="from and to must use YYYY-MM-DD format",
        )

    if start_date.isoformat() != from_day or end_date.isoformat() != to_day:
        raise HTTPException(
            status_code=422,
            detail="from and to must use YYYY-MM-DD format",
        )

    days_count = (end_date - start_date).days + 1
    if days_count <= 0:
        raise HTTPException(
            status_code=422,
            detail="to must be on or after from",
        )

    if days_count > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range must not exceed 92 days",
        )

    if not db.employees.find_one({"department": department}):
        raise HTTPException(
            status_code=404,
            detail="Department not found",
        )

    def round4(expression):
        return {
            "$divide": [
                {
                    "$floor": {
                        "$add": [{"$multiply": [expression, 10000]}, 0.5]
                    }
                },
                10000,
            ]
        }

    pipeline = [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": None,
                "employees": {
                    "$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}
                },
            }
        },
        {
            "$set": {
                "calendar": {
                    "$map": {
                        "input": {"$range": [0, days_count]},
                        "as": "offset",
                        "in": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": {
                                    "$dateAdd": {
                                        "startDate": {
                                            "$dateFromString": {"dateString": from_day}
                                        },
                                        "unit": "day",
                                        "amount": "$$offset",
                                    }
                                },
                            }
                        },
                    }
                }
            }
        },
        {"$unwind": "$calendar"},
        {
            "$set": {
                "eligible_employees": {
                    "$filter": {
                        "input": "$employees",
                        "as": "emp",
                        "cond": {"$lte": ["$$emp.joined_on", "$calendar"]},
                    }
                }
            }
        },
        {
            "$set": {
                "headcount": {"$size": "$eligible_employees"},
                "eligible_codes": {
                    "$map": {
                        "input": "$eligible_employees",
                        "as": "emp",
                        "in": "$$emp.emp_code",
                    }
                },
                "day_of_week": {
                    "$isoDayOfWeek": {
                        "$dateFromString": {
                            "dateString": "$calendar",
                        }
                    }
                },
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "day": "$calendar",
                    "employee_code": "$eligible_codes",
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$day"]},
                                    {"$in": ["$emp_code","$$employee_code"]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": ["$status",["PRESENT", "WFH", "ON_DUTY"]]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1.0,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": ["$late_minutes",0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "daily_stats",
            }
        },
        {
            "$set": {
                "present_count": {
                    "$ifNull": [
                        {
                            "$arrayElemAt": ["$daily_stats.present_count", 0]
                        },
                        0,
                    ]
                },
                "late_count": {
                    "$ifNull": [
                        {
                            "$arrayElemAt": ["$daily_stats.late_count", 0]
                        },
                        0,
                    ]
                },
                "is_working_day": {
                    "$and": [
                        {"$gte": ["$day_of_week", 1]},
                        {"$lte": ["$day_of_week", 5]},
                    ]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        round4({
                            "$divide": ["$present_count", "$headcount"]
                        }),
                        None,
                    ]
                }
            }
        },

        {
            "$setWindowFields": {
                "sortBy": {"calendar": 1},
                "output": {
                    "moving_avg_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },

        {
            "$project": {
                "_id": 0,
                "date": "$calendar",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_raw", None]},
                        round4("$moving_avg_raw"),
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]
    return {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}

def get_shift_datetimes(punch_in: datetime,shift_start: str,shift_end: str) -> tuple[datetime, datetime]:

    punch_in = normalize(punch_in)

    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    start_time = (start_h, start_m)
    end_time = (end_h, end_m)
    overnight = end_time <= start_time
    shift_date = punch_in.date()

    if overnight and (punch_in.hour, punch_in.minute) < end_time:
        shift_date -= timedelta(days=1)

    shift_start_dt = datetime.combine(shift_date, datetime.min.time(),tzinfo=IST).replace(hour=start_h, minute=start_m)
    shift_end_date = shift_date + (timedelta(days=1) if overnight else timedelta(days=0))
    shift_end_dt = datetime.combine(shift_end_date,datetime.min.time(),tzinfo=IST,).replace(hour=end_h, minute=end_m)

    return shift_start_dt, shift_end_dt

def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str) -> int:
    punch_in = normalize(punch_in)

    shift_start_dt, _ = get_shift_datetimes(punch_in, shift_start, shift_end)
    elapsed_seconds = int((punch_in - shift_start_dt).total_seconds())

    if elapsed_seconds <= 600:
        return 0

    return elapsed_seconds // 60

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    punch_in = normalize(punch_in)
    punch_out = normalize(punch_out)

    if punch_out < punch_in:
        raise ValueError("punch_out cannot be earlier than punch_in")

    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)

    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def compute_overtime(punch_in: datetime,punch_out: datetime,shift_start: str,shift_end: str) -> int:
    punch_in = normalize(punch_in)
    punch_out = normalize(punch_out)

    _, shift_end_dt = get_shift_datetimes(punch_in, shift_start, shift_end)
    elapsed_seconds = int((punch_out - shift_end_dt).total_seconds())
    overtime_minutes = elapsed_seconds // 60

    return max(0, overtime_minutes) if overtime_minutes >= 30 else 0

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM)
    shift_end: str = Field("18:30", pattern=HHMM)
    joined_on: str

class PunchIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

class PunchOut(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None

class UpdateAttendance(BaseModel):
    status: Optional[AttendanceStatus] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
     
def create_indexes():
    db["employees"].create_index(
        [("department", 1)],
        name="idx_employees_department"
    )
    db["employees"].create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employees_emp_code"
    )
    db["employees"].create_index(
        [("email", 1)],
        unique=True,
        name="uq_employees_email"
    )
    db["employees"].create_index(
        [("joined_on", 1)], 
        name="idx_employees_joined_on"
    )
    db["employees"].create_index(
        [("department", 1), ("joined_on", 1)], 
        name="idx_employees_dept_joined"
    )
    db["attendance_logs"].create_index(
        [("emp_code", 1), ("date", 1)], 
        unique=True, 
        name="uq_emp_date"
    )
    db["attendance_logs"].create_index(
        [("emp_code", 1), ("punch_in", -1)], 
        name="emp_punch_in"
    )
    db["attendance_logs"].create_index(
        [("date", -1), ("emp_code", 1)], 
        name="attendance_date_emp"
    )

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
            )

@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    if doc["shift_start"] == doc["shift_end"]:
        raise HTTPException(422, "shift_start must differ from shift_end")
    try:
        datetime.strptime(doc["joined_on"], "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, "joined_on must be in YYYY-MM-DD format")
    doc["created_at"] = now_ist()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = dt_to_ms(doc["created_at"])
    return doc

@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None, 
    page: int = 1, 
    page_size: int = 20
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size))
    for i in items:
        i["created_at"] = dt_to_ms(i["created_at"])
        
    return {"items": items, "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at else now_ist()
    shift_start_dt, _ = get_shift_datetimes(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": shift_start_dt.date().isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for that date")
    return serialize_record(doc)
    
@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOut):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at else now_ist()

    doc = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if doc is None:
        raise HTTPException(404, "No punch-in record found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")

    punch_in_dt = normalize(doc["punch_in"])
    if ts <= punch_in_dt:
        raise HTTPException(422, "punch_out must be after punch_in")
    if ts - punch_in_dt > timedelta(hours=24):
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    work_hours = compute_work_hours(punch_in_dt, ts)
    updates = {
        "punch_out": ts,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(punch_in_dt, ts, emp["shift_start"], emp["shift_end"]),
        "half_day": work_hours < 4.50,
    }
    res = db.attendance_logs.update_one({"_id": doc["_id"], "punch_out": None}, {"$set": updates})
    if res.matched_count == 0:
        raise HTTPException(409, "Already punched out")
    doc.update(updates)
    return serialize_record(doc)

@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def update_attendance(
    emp_code: str, 
    date: str, 
    body: UpdateAttendance,
):
    try:
        Date.fromisoformat(date)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="date must be in YYYY-MM-DD format",
            )
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    
    if employee is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found"
            )

    doc = db["attendance_logs"].find_one({"emp_code": emp_code, "date": date}, {"_id": 0})
    
    if doc is None:
        raise HTTPException(
            status_code=404,
            detail="Attendance record not found",
            )
    supplied = body.model_fields_set
    requested = body.model_dump(exclude={"regularized_by", "reason"}, exclude_unset=True)
    
    for k in ("punch_in", "punch_out"):
       if requested.get(k) is not None:
           requested[k] = ms_to_dt(requested[k])

    punch_in_supplied = "punch_in" in supplied
    punch_out_supplied = "punch_out" in supplied
    requested_status = requested.get("status", doc.get("status"))

    if requested_status in {"ABSENT", "LEAVE"} and (punch_in_supplied or punch_out_supplied):
        raise HTTPException(
            status_code=422,
            detail="Cannot supply punch times with ABSENT or LEAVE",
            )
    
    final = {key: doc.get(key) for key in ("status", "punch_in", "punch_out")}
    final.update(requested)

    if final["status"] in {"ABSENT", "LEAVE"}:
        final["punch_in"] = None
        final["punch_out"] = None

    
    punch_in = (normalize(final["punch_in"]) if final["punch_in"] is not None else None)
    punch_out = (normalize(final["punch_out"]) if final["punch_out"] is not None else None)
    final["punch_in"] = punch_in
    final["punch_out"] = punch_out

    attendance_date = Date.fromisoformat(date)

    if final["status"] in {"ABSENT", "LEAVE"}:
        final["punch_in"] = None
        final["punch_out"] = None
        punch_in = None
        punch_out = None
    else:
        if punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="A presence status requires punch_in",
            )

        if get_shift_datetimes(punch_in, employee["shift_start"], employee["shift_end"])[0].date() != attendance_date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must fall on the attendance date",
            )

        if punch_out is not None:
            if punch_out <= punch_in:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in",
                )

            if punch_out - punch_in > timedelta(hours=24):
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be within 24 hours of punch_in",
                )

    if final["status"] in {"ABSENT", "LEAVE"}:
        metrics = {
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }
    else:
        late_minutes = compute_late_minutes(punch_in, employee["shift_start"], employee["shift_end"])

        if punch_out is None:
            metrics = {
                "late_minutes": late_minutes,
                "work_hours": None,
                "overtime_minutes": 0,
                "half_day": False,
            }
        else:
            work_hours = compute_work_hours(punch_in, punch_out)
            overtime_minutes = compute_overtime(punch_in, punch_out, employee["shift_start"], employee["shift_end"])
            metrics = {
                "late_minutes": late_minutes,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": work_hours < 4.50,
            }

    
    changes = {}

    for key, new_value in final.items():
        old_value = comparable_value(key, doc.get(key))
        new_value = comparable_value(key, new_value)

        if old_value != new_value:
            changes[key] = {
                "from": old_value,
                "to": new_value,
            }

    for key, new_value in metrics.items():
        old_value = comparable_value(key, doc.get(key))
        new_value = comparable_value(key, new_value)

        if old_value != new_value:
            changes[key] = {
                "from": old_value,
                "to": new_value,
            }

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="The request does not change the attendance record",
        )

    updated_at = now_ist()
    history_entry = {
        "at": updated_at,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    concurrency_filter = {
        "status": doc.get("status"),
        "punch_in": doc.get("punch_in"),
        "punch_out": doc.get("punch_out"),
    }

    if "history" in doc:
        concurrency_filter["history"] = doc["history"]
    else:
        concurrency_filter["history"] = {"$exists": False}
    
    result = db.attendance_logs.find_one_and_update(
        concurrency_filter,
        {
            "$set": {
                **final,
                **metrics,
                "updated_at": updated_at,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if result is None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record changed concurrently; retry the request",
        )
    
    return serialize_record(result)

@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[AttendanceStatus] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    cmd = build_attendance_list(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(cmd["filter"])
    items = list(db.attendance_logs.find(cmd["filter"])
                 .sort(list(cmd["sort"].items())).skip(cmd["skip"]).limit(cmd["limit"]))
    return {"items": [serialize_record(i) for i in items],
            "total": total, "page": page, "page_size": page_size}

@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def get_employee_analytics(emp_code: str, month: str = Query(..., pattern=MONTH_RE)):
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "Unknown Employee")
    start_str, end_str = month_range(month)
    rows = list(db.attendance_logs.aggregate(build_employee_monthly(emp_code, start_str, end_str)["pipeline"]))
    s = rows[0] if rows else {}
    year, mon = map(int, month.split("-"))
    working_days = count_working_days(year, mon, emp["joined_on"])
    present_days = s.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days),
        "leave_days": s.get("leave_days", 0),
        "late_count": s.get("late_count", 0),
        "total_late_minutes": s.get("total_late_minutes", 0),
        "total_overtime_minutes": s.get("total_overtime_minutes", 0),
        "attendance_pct": round_half_up(present_days / working_days * 100) if working_days else None,
    }

@app.get("/analytics/departments/summary", tags=["analytics"])
def get_departments_summary(
    department: str,
    month: str = Query(..., pattern=MONTH_RE),
):
    start_str, end_str = month_range(month)
    cmd = build_department_summary(department, start_str, end_str)
    results = list(db.employees.aggregate(cmd["pipeline"]))
    for r in results:
        r["present_days"] = round_half_up(r["present_days"])
        r["avg_work_hours"] = round_half_up(r["avg_work_hours"])
    return {"month": month, "items": results}

@app.get("/analytics/leaderboard/late", tags=["analytics"])
def get_late_leaderboard(
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
    month: str = Query(..., pattern=MONTH_RE),
):
    start_str, end_str = month_range(month)
    cmd = build_late_leaderboard(department, limit, start_str, end_str)
    return {"month": month, "items": list(db.employees.aggregate(cmd["pipeline"]))}

@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def get_department_trends(department: str,
                          from_day: str = Query(alias="from"),
                          to_day: str = Query(alias="to")):
    cmd = build_department_trend(department, from_day, to_day)
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Department not found")
    return {"department": department, "items": list(db.employees.aggregate(cmd["pipeline"]))}

@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: explain_options,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[AttendanceStatus] = None,
    from_day: Optional[str] = Query(None, alias="from"),
    to_day: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        cmd = build_attendance_list(emp_code, date_from, date_to, status, page, page_size)
    elif endpoint == "employee_monthly":
        require(emp_code=emp_code, month=month)
        cmd = build_employee_monthly(emp_code, *month_range(month))
    elif endpoint == "department_summary":
        require(month=month)
        cmd = build_department_summary(department, *month_range(month))
    elif endpoint == "late_leaderboard":
        require(month=month)
        cmd = build_late_leaderboard(department, limit, *month_range(month))
    else:  # department_trend
        require(department=department, **{"from": from_day, "to": to_day})
        cmd = build_department_trend(department, from_day, to_day)

    collection = cmd.get("aggregate") or cmd["find"]
    raw = db.command({"explain": cmd, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection,
            "explain": json.loads(json_util.dumps(raw))}