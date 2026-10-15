# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | list_employees | Incorrect pagination skip calculation caused the endpoint to return the wrong set of employees | While Running "/employees" endpoint, the items list was empty, so no list of employees was not being sent. | Pagination normally starts at page 1, so the first page should skip zero documents, however because skip ignored the current 20 entries and showed next ones, so changed it to (skip - 1) so it would skip previous entries, not current 20 entries. |
| 2 | class EmployeeIn | Email field uses str allowing invalid email formats to pass validation | According to Problem statement doc if the invalid input is given, it should return 422, but the code return 201 | Changed the Email field definition from str to EmailStr in the Pydantic model. It allows proper validation and allows invalid email to return a 422 response. |
| 3 | class PunchIn | punched_at field is defined as int, but in function punch_in it is expected to be in datetime format | I observed it while trying to observe and understand the code. | Changed the field to datetime. |
| 4 | `create_employee`, `created_at` | `datetime.now()` is naive server-local time. It depends on the host timezone, and the datetime is returned unconverted. | Run the server in UTC vs IST and compare `created_at`. | `now_ist()` for storage, and return epoch milliseconds like every other timestamp. |
| 5 |  `punch_in`, `ts` | `datetime.fromtimestamp(ms / 1000)` produces a naive **server-local** datetime. It keeps fractional seconds (`/ 1000` instead of `// 1000`), and stored times shift when the host timezone changes. | The same `punched_at` gives a different `date` on a UTC host vs an IST host. A punch at 23:00 IST can land on the next date. | `ms_to_dt()`: `fromtimestamp(ms // 1000, tz=utc).astimezone(IST)`, always timezone-aware IST, truncated to whole seconds. |
| 6 | `PunchInIn`, `punched_at` | The field is an epoch-**millisecond** int with no range check. Seconds passed by mistake (`1700000000`) silently becomes 1970. Huge values raise `OverflowError` (500). The field itself should stay an `int`; the real bug is the unchecked range and the conversion in #12. | `punched_at: 1700000000` creates a record dated Jan 1970. `punched_at: 9e18` gives a 500. | `EpochMillis = StrictInt` with `ge=100_000_000_000` and `le=4_102_444_800_000`, so seconds and absurd values get 422. |
| 7 | `compute_late_minutes`, date | `punch_in.replace(hour, minute)` uses the punch's own calendar date. Overnight shifts compute the wrong shift start and return 0 or a negative number. | Shift 22:00-06:00, punch at 01:00: returns 0, should be 180. | `get_shift_datetimes()` picks the right shift start; late minutes are measured against it. |
| 8 | `compute_overtime`, shift end | `end` is built from the attendance date, so overnight shifts compare against the wrong (earlier) end time. The naive vs aware subtraction also raises `TypeError`. Also 30 minutes threshold rule is not implemented | 22:00-06:00 shift, punch-out at 06:45: overtime is computed against 06:00 *of the same day*, giving about 17 hours. | `get_shift_datetimes()` gives the true end datetime, including the next day for overnight shifts. |
| 9 | `list_attendance`, in-memory sort | `list(db.attendance_logs.find(q))` loads every matching log into memory, sorts in Python, then slices. Memory and latency grow with collection size, even for page 1. | With 1M logs, `GET /attendance` takes seconds and uses GBs; `explain` shows a full collection scan. | Sort, `skip` and `limit` in MongoDB; `count_documents` for the total. Indexes on `(date, emp_code)` and `(emp_code, date)`. |
| 10 | `health` | Always returns `ok` without touching the database. | Stop MongoDB: `/health` still says `ok`, so orchestrators keep routing traffic. | `db.command("ping")`; return 503 on failure. |
| 11 | `list_attendance` / `punch_in`, serialization | Responses return raw `datetime`s (naive) and Mongo internals (`updated_at`, a `history` full of datetimes). `punch_in` hand-sets `id`. | `punch_in` comes back as an ISO string with no timezone, while the rest of the API uses epoch milliseconds. | `serialize_record()` strips `_id`/`updated_at` and converts every timestamp (including history) to epoch milliseconds. |



Also note anything you looked at and decided was **not** a defect, and why.

- **`IST = timezone(timedelta(hours=5, minutes=30))`** is the correct UTC+5:30 offset. The problem was that it was applied inconsistently (#8, #12), not the value.
- **`.find(q, {"_id": 0})`** in `list_employees` correctly hides the Mongo ObjectId.
- **`doc.pop("_id", None)`** after `insert_one` is needed and correct: PyMongo mutates the dict by adding `_id`.
- **The `date` range filter construction** in `list_attendance` (`$gte`/`$lte` on ISO strings) is correct, because ISO dates sort lexicographically. Only the missing validation (#23) was wrong.
- **Default shift times** (`09:30`/`18:30`), the 201 status codes and the 409 message wording match the spec.
- **Environment-based Mongo configuration** via `dotenv` is fine.

### 1. Which indexes you created and why.
| # | Index Name | Reason |
| 1 | idx_employees_department | Speeds up queries that use department to filter employees like endpoints: "/analytics/departments/summary", "/analytics/departments/{department}/trend" and others |
| 2 | uq_employees_emp_code | attendance_logs | Creates a filter such that emp_codes are unique across the db and speeding up queries retrieving employee data using emp_code in "/analytics/employees/{emp_code}/monthly", "/attendance", etc |
| 3 | uq_employees_email | attendance_logs | Creates a filter in order to prevent duplicate email registrations. |
| 4 | idx_employees_joined_on | attendance_logs | Optimizes queries involving joining dates, especially calculating metrics for people joining in middle of the month in "/analytics/employees/{emp_code}/monthly", "/analytics/departments/summary", etc |
| 5 | idx_employees_dept_joined | attendance_logs | Optimizes queries which uses department and joining dates as filters such as in "/analytics/departments/summary", etc |
| 6 | uq_emp_date | attendance_logs | To make sure an employee can only have one attendance log per calendar day, to prevent duplicate punch_ins used in "/attendance/punch-in", "/attendance/punch-out"|
| 7 | emp_punch_in | attendance_logs | Optimizes queries searching for an employee's attendance logs sorted by the most recent punch-in time. used in "/attendance/punch-out" |
| 8 | attendance_date_emp | attendance_logs | Speeds up date-range queries and analytics pipelines that filter or group attendance data across all employees by date. Used in "/analytics/employees/{emp_code}/monthly", "/analytics/leaderboard/late", "/analytics/departments/{department}/trend", "/admin/explain/{endpoint}" |

### 2. what exactly happens when two identical punch-ins arrive at the same instant.
One entry wins and database decides which one. uq_emp_date index takes care of duplicate entries, so punch_in doesn't need to check if an similar entry is present, it gives 409 when duplicate entry is given.

### 3. what your leaderboard returns for a tie at the cutoff
Ties at the cutoff are all included, so the response can have more than limit rows. The pipeline uses $setWindowFields with $rank on total_late_minutes descending, then keeps rank <= limit.

### 4. how your department summary counts employees with no logs
Employees with zero logs are added in the headcount, but excluded from other metrics like present_days, late_count, leave_count, on_duty_count, total_late_minutes.

### 5. what you would change for 100 times the data
I have not yet worked on very large databases. So, I would just write, what i have read in theory, cause each system reacts differently while coming to scale, not all methods work for all systems. We can include Caching (the calculations of previous months remain same untill overwritten manually, so this would reduce the load on the db. we can cache data by month), DB Rollups(Analytics endpoints would read it instead of scanning logs), also create db function instead of these endpoints calling each time to the db, we can have the base values stored and getting updated periodically. functions like datetime.now(), $dateFromString and $isoDayOfWeek are individually not very heavy, but cummulaltively for large dataset it can be computationally intensive, so we can write a fields like is_weekday and month. adding department to attendance_logs, it would reduce the complexity required for monthly trend endpoint.