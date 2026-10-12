# REVIEW.md

## Employee Attendance & Analytics API — Defect Review

This report documents defects identified and fixes applied while developing and testing the Employee Attendance & Analytics API.

The review covers input validation, attendance processing, overnight shifts, attendance corrections, MongoDB consistency, and analytics.

**Note:** The original unmodified starter file was not retained. Therefore, this report records issues observed during development and the behavior of the reviewed implementation. It cannot conclusively identify every defect in the original starter.

## 1. Defect Log

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `punch_in()` — timestamp validation | An invalid epoch timestamp was accepted before range validation was added. | Submitting `punched_at: 12345` created an attendance record dated `1970-01-01`. | Added a permitted epoch-millisecond range of `100000000000` to `4102444800000`. |
| 2 | `PunchInIn` — Pydantic request model | Ordinary integer parsing could accept numeric strings or coerce some input types. | Tested `punched_at` with a string and a floating-point value. | Changed the field to `Optional[StrictInt]` to require a real integer. |
| 3 | `punch_in()` — overnight attendance date | Using only the calendar date can assign an early-morning punch to the wrong shift date. | Tested an employee working from `22:00` to `06:00`. | Added logic to assign early-morning punches before the overnight shift end to the previous attendance date. |
| 4 | `correct_attendance()` — correction implementation | Attendance corrections needed consistent validation, recalculation, and history recording. | Tested changes between `PRESENT`, `WFH`, `ON_DUTY`, and `ABSENT`. | Implemented status validation, dependent-field recalculation, and audit-history recording. |
| 5 | `correct_attendance()` — non-present statuses | `ABSENT` and `LEAVE` records must not retain punch timestamps or calculated working hours. | Changed an existing attendance record to `ABSENT` and inspected the returned fields. | Cleared punch-in, punch-out, work hours, late minutes, and overtime when changing to a non-present status. |
| 6 | `correct_attendance()` — no-op requests | A correction with no actual field changes should not create a misleading audit entry. | Submitted a correction requesting the status already stored in the record. | Compared old and new values and returned HTTP `422` when no changes existed. |
| 7 | `correct_attendance()` — concurrent modifications | Simultaneous corrections could overwrite changes or produce an inconsistent audit history without concurrency protection. | Reviewed the database update logic for competing correction requests. | Used a conditional update based on the previously read history and appended the audit entry in the same MongoDB update. |

## 2. Additional Validation and Verification

The following behaviors were checked during testing.

| Area | Test performed | Observed result |
|---|---|---|
| Employee creation | Create an employee with an existing employee code | HTTP `409` |
| Punch-in | Repeat punch-in for the same employee and attendance date | HTTP `409` |
| Punch-in timestamp | Submit an invalid timestamp | HTTP `422` after the fix |
| Punch-out | Attempt to punch out an already completed record | HTTP `409` |
| Punch-out duration | Submit a punch-out outside the allowed 24-hour duration | HTTP `422` |
| Attendance correction | Change status to `ABSENT` | Punch timestamps and derived values cleared |
| Attendance correction | Submit a correction with no changes | HTTP `422` |
| Attendance correction | Provide punch times for `LEAVE` | HTTP `422` |
| Monthly analytics | Request employee statistics for October 2026 | HTTP `200`; response inspected |
| Department summary | Request department analytics | HTTP `200`; response inspected |
| Late leaderboard | Request employee late rankings | HTTP `200`; response inspected |
| Department trend | Request daily attendance trend | HTTP `200`; response inspected |
| Department trend | Request more than 92 days | HTTP `422` |
| MongoDB query plan | Call `/admin/explain` for an indexed employee/date query | Query plan contained `IXSCAN` |

These tests provide evidence for the listed behaviors but do not constitute complete automated test coverage.

## 3. Investigated Behaviors That Were Not Defects

### Duplicate punch-in returns HTTP 409

The API rejects a second attendance record for the same employee and attendance date.

**Why this is not a defect:** The database has a unique compound index on `emp_code` and `date`. Rejecting duplicates maintains data consistency.

### No-change correction returns HTTP 422

A PATCH request that does not change any stored attendance fields is rejected.

**Why this is not a defect:** An attendance correction must represent an actual change. Creating an audit-history entry for an unchanged record would be misleading.

### Missing punch-out and work-hours values

An attendance record can have `punch_out` and `work_hours` set to `null`.

**Why this is not a defect:** These fields are not available until a successful punch-out has occurred.

### Overnight punch-in belongs to the previous date

For an overnight shift, an early-morning punch-in before the shift end can be associated with the preceding attendance date.

**Why this is not a defect:** Attendance dates represent the relevant shift date rather than always matching the punch-in calendar date.

### Indexed MongoDB query

The `/admin/explain` endpoint returned an execution plan containing `IXSCAN` for an employee/date query.

**Why this is not a defect:** This indicates MongoDB is using an index rather than performing a full collection scan for that tested query.

## 4. Remaining Limitations and Risks

The following items were identified during review but have not been fully verified or resolved.

1. **Strict timestamp validation is inconsistent.** `PunchInIn` uses `StrictInt`, but `PunchOutIn` and `AttendanceCorrectionIn` still declare timestamp fields using ordinary `int`. Pydantic may coerce some input types before the endpoint's validation runs.

2. **Leaderboard ties at the limit.** The leaderboard calculates ranks using MongoDB `$rank`, then applies `$limit`. Employees tied at the cutoff receive the same rank, but not all tied employees are necessarily returned.

3. **Large-scale performance.** Index use was confirmed for a small dataset. Performance with approximately 100,000 attendance records has not been benchmarked.

4. **Original starter comparison.** Without the unmodified starter file, the exact number of original defects and their original line numbers cannot be independently verified.

## 5. Conclusion

The API was tested across employee creation, punch-in, punch-out, attendance correction, analytics, and MongoDB query inspection.

The confirmed invalid-timestamp issue was corrected, and the tested attendance workflows produced the expected HTTP responses. The implementation also includes overnight-shift handling, correction history, and indexed database queries.

Further improvements should focus on consistent strict input validation, boundary-case testing, automated regression tests, and performance testing with larger datasets.