# REVIEW.md

The following defects were identified in the available earlier implementation (`app/main_backup.py`). Confirm that this file matches the supplied starter before treating every item as a starter defect.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | punch_in | Duplicate detection uses a read-before-insert check, which is not atomic under concurrent requests. | Send simultaneous punch-in requests for the same employee and attendance date; both may pass the initial check. | Added a unique compound index on employee code and date, and convert DuplicateKeyError to HTTP 409. |
| 2 | punch_in / date handling | `datetime.fromtimestamp()` uses the machine's local timezone, and the date is taken directly from `ts.date()`. This does not reliably enforce UTC storage, IST attendance dates, or overnight shifts. | Test epoch-millisecond punches around midnight and across an overnight shift. | Added strict epoch-millisecond conversion to UTC, IST-based attendance-day logic, and overnight-shift handling. |
| 3 | list_attendance | The earlier implementation loads all matching records and sorts and paginates in Python. This can use excessive memory and work on large datasets. | Query attendance against a large collection and observe the amount of data loaded. | Apply sorting, skip, and limit in MongoDB, with indexes supporting common filters and ordering. |
| 4 | API coverage | The earlier file contains TODO comments for punch-out, regularization, and analytics endpoints. | Compare its routes with the required API contract; the required routes are missing. | Implemented the punch-out, regularization, monthly analytics, department summary, late leaderboard, and department trend routes in the current implementation. |
| 5 | health | The endpoint returns `{"status": "ok"}` without checking MongoDB connectivity. | Stop or disconnect MongoDB and call `/health`; it can still report success. | Added a MongoDB ping and return HTTP 503 when the database is unavailable. |

## Reviewed and not treated as defects

- `decimal_round` uses `Decimal` with `ROUND_HALF_UP`, matching the required rounding rule.
- `late_minutes` applies the 10-minute grace period and only counts lateness greater than 10 minutes.
- The current `department_summary` starts from eligible employees before looking up attendance logs, allowing employees with no logs to contribute to headcount.
- The current leaderboard gives equal ranks to employees tied on total late minutes in the tested result.

