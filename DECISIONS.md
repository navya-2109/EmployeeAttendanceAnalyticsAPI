# DECISIONS.md

**1. Indexes**

I created a unique index on `employees.emp_code` to prevent duplicate employees. I also created a unique compound index on `attendance_logs(emp_code, date)` to prevent duplicate daily attendance and speed up employee/date searches. I considered a separate index on `attendance_logs.date`, but did not add it because the current dataset is small and extra indexes increase write and storage costs.

**2. Punch-in race**

If two identical punch-in requests arrive together, both may check that the employee exists and attempt to insert an attendance record. MongoDB's unique compound index allows only one insert. The successful request returns HTTP 201, while the other receives a duplicate-key error, which my code converts into HTTP 409.

**3. Ties**

The leaderboard uses MongoDB's `$rank`, so employees with equal late minutes receive the same rank. Results are then sorted by late minutes and employee code. Because `$limit` is applied afterward, a tie at the cutoff may be split, and only the requested number of employees is returned.

**4. Headcount**

The department summary starts with the employees collection and uses `$lookup` to find attendance logs. It preserves employees without logs and counts distinct employee codes, so they remain included.

**5. Scaling**

For 100 times more data, I would add carefully selected indexes for frequently used analytics queries and benchmark them using MongoDB execution statistics.
