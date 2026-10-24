1. Indexes. I created a unique index on employee code to prevent duplicate employees, and a unique compound index on employee code and attendance date to prevent duplicate daily records. I added department plus employee code for department queries, joined_on for eligible headcount, date plus employee code for attendance listings, status plus date for status filters, and employee code plus descending punch-in for recent attendance lookup. I avoided adding another index with the same leading fields because redundant indexes increase write and storage costs.

2. Punch-in race. Both requests may check the employee and build a record, but the unique attendance index allows only one record for the same employee and date. The other insert raises DuplicateKeyError, which the API converts to HTTP 409 Conflict.

3. Ties. The leaderboard uses rank-based ordering, so employees with equal total late minutes receive the same rank. The tested employees with 80 minutes both received rank 4.

4. Headcount. The summary starts from eligible employees grouped by department, then looks up attendance logs. Employees with no logs can still contribute to headcount.

5. At 100 times the data, I would benchmark realistic workloads, inspect query plans, and optimize aggregation pipelines and indexes based on measured bottlenecks.
