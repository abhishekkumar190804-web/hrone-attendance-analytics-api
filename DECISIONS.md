# DECISIONS.md

**1. Indexes.** On `employees`: unique `emp_code` (lookup and the create race), `department`, `joined_on` (headcount/trend filters). On `attendance_logs`: unique `(emp_code, date)` (natural key and punch-in race), `(date desc, emp_code asc)` (list/month/trend scans sorted), and `(status, date desc, emp_code asc)` (status-filtered lists). I rejected a `late_minutes` index for the leaderboard: low selectivity, already narrowed by `date`, and it would only add write cost.

**2. Punch-in race.** Both requests resolve the same IST date (R1) and each calls `insert_one`. The unique `(emp_code, date)` index lets exactly one succeed; the loser's `DuplicateKeyError` maps to 409. There is no check-then-insert, so timing never decides the outcome.

**3. Ties.** `$setWindowFields` `$rank` yields competition ranking (1, 2, 2, 4); rows with `rank <= limit` survive, so a tie at the cutoff can exceed `limit`. Rows sort by `total_late_minutes` desc, then `emp_code` asc.

**4. Headcount.** The summary unions a headcount pipeline over `employees` (`joined_on <= month end`, grouped by department) with the log pipeline, then sums the two. Employees with zero logs still appear; late joiners are excluded.

**5. At 100x.** Keyset pagination instead of skip/limit; precomputed daily rollups (or time-series collections) so trend/summary stop scanning raw logs; cache the monthly leaderboard; shard on hashed `emp_code`.