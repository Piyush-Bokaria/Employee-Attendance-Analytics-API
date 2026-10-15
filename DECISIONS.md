# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
**Ans**:
| # | Index Name | Reason |
| 1 | idx_employees_department | Speeds up queries that use department to filter employees like endpoints: "/analytics/departments/summary", "/analytics/departments/{department}/trend" and others |
| 2 | uq_employees_emp_code | attendance_logs | Creates a filter such that emp_codes are unique across the db and speeding up queries retrieving employee data using emp_code in "/analytics/employees/{emp_code}/monthly", "/attendance", etc |
| 3 | uq_employees_email | attendance_logs | Creates a filter in order to prevent duplicate email registrations. |
| 4 | idx_employees_joined_on | attendance_logs | Optimizes queries involving joining dates, especially calculating metrics for people joining in middle of the month in "/analytics/employees/{emp_code}/monthly", "/analytics/departments/summary", etc |
| 5 | idx_employees_dept_joined | attendance_logs | Optimizes queries which uses department and joining dates as filters such as in "/analytics/departments/summary", etc |
| 6 | uq_emp_date | attendance_logs | To make sure an employee can only have one attendance log per calendar day, to prevent duplicate punch_ins used in "/attendance/punch-in", "/attendance/punch-out"|
| 7 | emp_punch_in | attendance_logs | Optimizes queries searching for an employee's attendance logs sorted by the most recent punch-in time. used in "/attendance/punch-out" |
| 8 | attendance_date_emp | attendance_logs | Speeds up date-range queries and analytics pipelines that filter or group attendance data across all employees by date. Used in "/analytics/employees/{emp_code}/monthly", "/analytics/leaderboard/late", "/analytics/departments/{department}/trend", "/admin/explain/{endpoint}" |

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
**Ans**: 
a. One entry wins and database decides which one. uq_emp_date index takes care of duplicate entries, so punch_in doesn't need to check if an similar entry is present, it gives 409 when duplicate entry is given. 
b. As punch_in doesn't check for an existing record, it builds the documents and calls the insert_one function.
c. While initiating the apps the indexes are created in lifespan function. so the uq_emp_date makes sure that no duplicate entry is taken.
d. The first insert to commit returns 201 with the serialized record. The other raises DuplicateKeyError, which is caught and returned as 409 “Already punched in for that date”. Nothing is written. For overnight shifts, date is the shift’s start date.

3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
**Ans**: 
a. Ties at the cutoff are all included, so the two employees tied on late_minutes at cutoff of limit are shown. b/b. Based on statement: * `limit` applies **after** ranking: return every employee whose `rank` <= `limit`. The response can therefore contain more than `limit` rows when employees tie at a rank <= `limit`. from openapi.yaml.
c. With limit=3 and totals of 100, 80, 50, 50, 20:

| total_late_minutes | rank | returned |
| 100 | 1 | yes |
| 80 | 2 | yes |
| 50 | 3 | yes |
| 50 | 3 | yes |
| 20 | 4 | no |

d. That returns 4 rows. Within a tie, rows are ordered by emp_code ascending, and both show the same rank. Employees with zero late minutes are filtered out before ranking, so they never appear.

4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
**Ans**: 
a. They count toward headcount and contribute zeros everywhere else. like in the function build_department_summary we have this filter for counting headcount "headcount": {"$addToSet": "$emp_code"} where do not check if they have any logs or not.
b. But for other metrics like present_days, avg_work_hours(work_hour_sum and work_hour_count), late_count we have filters like this:
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
} where the condition "{"$ne": ["$logs", None]}" checks if the employees has any logs, if not they are ignored.

5. **One thing you would change** if this had to serve 100x the data.
**Ans**: I have not yet worked on very large databases. So, I would just write, what i have read in theory, cause each system reacts differently while coming to scale, not all methods work for all systems. 
We can include Caching (the calculations of previous months remain same untill overwritten manually, so this would reduce the load on the db. we can cache data by month)
DB Rollups(Analytics endpoints would read it instead of scanning logs)
create db function instead of these endpoints calling each time to the db, we can have the base values stored and getting updated periodically.
Functions like datetime.now(), $dateFromString and $isoDayOfWeek are individually not very heavy, but cummulaltively for large dataset it can be computationally intensive, so we can write a fields like is_weekday and month.
Adding department to attendance_logs, it would reduce the complexity required for monthly trend endpoint.