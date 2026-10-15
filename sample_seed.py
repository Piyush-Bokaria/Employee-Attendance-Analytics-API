"""Loads the nine sample documents from sample_data/ (see DATA_MODEL.md) into your database.
Usage:  python sample_seed.py            (reads MONGO_URI / MONGO_DB from the environment or .env)

Equivalent with the MongoDB tools:
  mongoimport --uri "$MONGO_URI" --db "$MONGO_DB" -c employees       --jsonArray --drop --file sample_data/employees.json
  mongoimport --uri "$MONGO_URI" --db "$MONGO_DB" -c attendance_logs --jsonArray --drop --file sample_data/attendance_logs.json

The samples show document SHAPES only. The grader uses a much larger hidden dataset with cases they do not show.
Passing on this sample does not mean your analytics are correct - re-read the business rules."""
import os
import pathlib

from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
here = pathlib.Path(__file__).parent / "sample_data"
opts = json_util.JSONOptions(tz_aware=True)
db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)[os.getenv("MONGO_DB", "attendance_db")]

for name in ("employees", "attendance_logs"):
    docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
    db[name].delete_many({})
    db[name].insert_many(docs)
    print(f"loaded {len(docs):>2} documents into '{db.name}.{name}'")

'''
pipeline = [
    {
        "$match": {
            "department": department
        }
    },
    {
        "$lookup": {
            "from": "attendance_logs",
            "let": {
                "emp_code": "$emp_code"
            },
            "pipeline": [
                {
                    "$match": {
                        "$expr": {
                            "$and": [
                                {"$eq": ["$emp_code", "$$emp_code"]},
                                {"$gte": ["$date", from_day]},
                                {"$lte": ["$date", to_day]}
                            ]
                        }
                    }
                }
            ],
            "as": "logs"
        }
    },
    {
        "$unwind": "$logs"
    },
    {
        "$set": {
            "attendance_date": {
                "$dateFromString": {
                    "dateString": "$logs.date",
                    "format": "%Y-%m-%d"
                }
            }
        }
    },
    {
        "$set": {
            "day_of_week": {
                "$isoDayOfWeek": "$attendance_date"
            },
            "is_present": {
                "$in": [
                    "$logs.status",
                    ["PRESENT", "WFH", "ON_DUTY"]
                ]
            }
        }
    },
    {
        "$group": {
            "_id": "$attendance_date",
            "present_count": {
                "$sum": {
                    "$cond": [
                        "$is_present",
                        {
                            "$cond": [
                                {"$eq": ["$logs.half_day", True]},
                                0.5,
                                1.0
                            ]
                        },
                        0
                    ]
                }
            },
            "late_count": {
                "$sum": {
                    "$cond": [
                        {"$gt": ["$logs.late_minutes", 0]},
                        1,
                        0
                    ]
                }
            },
            "is_working_day": {
                "$first": {
                    "$and": [
                        {"$gte": ["$day_of_week", 1]},
                        {"$lte": ["$day_of_week", 5]}
                    ]
                }
            }
        }
    },
    {
        "$sort": {
            "_id": 1
        }
    },
    {
        "$project": {
            "_id": 0,
            "date": {
                "$dateToString": {
                    "format": "%Y-%m-%d",
                    "date": "$_id"
                }
            },
            "is_working_day": 1,
            "present_count": 1,
            "late_count": 1
        }
    }
]

results = list(db["employees"].aggregate(pipeline))
return results

'''

