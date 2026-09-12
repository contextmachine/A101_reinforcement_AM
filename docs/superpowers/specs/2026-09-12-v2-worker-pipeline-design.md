# V2 Whole-Field Worker Pipeline Design

## Goal

Add an API v2 that treats the complete field as one optimization problem, removes public component semantics, separates heavy stages into dedicated worker pools/queues, makes bar layout and verification asynchronous resources, centralizes runtime limits, and supports optional HiGHS log upload to S3-compatible object storage.

## Fixed semantics

- N counts only additional reinforcement zones. Background is excluded.
- Public v2 optimization has no component concept; one whole-field matrix/problem is prepared.
- PostgreSQL is the source of truth for public execution state. Redis is transport only.
- N states: pending, preparing, solving, fitting, baring, error, success, cancelled.
- Solver status: optimal, feasible, infeasible. A time limit with an incumbent is feasible; operational failure without a usable incumbent is error; infeasible is only mathematically proven infeasibility.
- GET /v2/tasks/{task_id} returns every requested N and its real persisted state, even before a final result exists.
- POST /v2/bars and POST /v2/verification are asynchronous and return IDs. Matching GET endpoints return persisted state/result/error.
- Public/persisted v2 zone/bar geometry is without anchorage expansion. start_anchorage/end_anchorage are authoritative metadata. Hidden anchorage contributes to mass but not verification coverage.
- Verification uses visible clipped bar axes. Thickness t is always millimetres.
- Re-adding N: success/active => no duplicate; error/cancelled => new attempt reusing prepared problem.
- Per-task concurrency limit applies specifically to solver jobs.
- S3 solver-log configuration contains only bucket, prefix, access key, secret key. If any is null/empty, HiGHS log_file is not configured and no log file/upload is attempted.

## Worker topology

Separate Redis queues/deployments/KEDA scalers for preparing, solving, fitting, baring, validation. Shared PostgreSQL/Redis. Existing v1 worker remains intact.

Preparing executes scene/overlay resolution, whole-field matrix, exact max-N, then full prepare_problem using max-N as upper bound. It never invokes old handlers.

Solving is one (task,n,attempt) job; fitting is one (task,n,attempt); baring finishes task solutions or standalone bars requests; validation performs bar layout internally via shared pure layout functions and then source-polygon reinforcement validation.
