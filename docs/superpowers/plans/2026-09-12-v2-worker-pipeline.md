# V2 Whole-Field Worker Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved whole-field API v2 and isolated worker pipeline without breaking v1.

**Architecture:** V2 uses dedicated persisted entities and stage queues. Pure computation is shared where safe; legacy job handlers are not called from v2.

**Tech Stack:** FastAPI/Pydantic, PostgreSQL/SQLAlchemy/Alembic, Redis, Shapely, HiGHS/highspy, Kubernetes/KEDA, pytest, S3-compatible object storage.

**Spec:** `docs/superpowers/specs/2026-09-12-v2-worker-pipeline-design.md`

## Global Constraints

- Preserve current v1 behavior and passing tests.
- PostgreSQL owns public state.
- N counts only additional zones.
- Public v2 geometry is without anchorage.
- Solver concurrency applies only to solve jobs.
- S3 logging is entirely disabled when any required S3 field is null/empty.

---

### Task 1: V2 durable state and API models
- [ ] RED tests for task/N states, retries, async bars/verification persistence.
- [ ] Migration and PostgresStore methods.
- [ ] Models and focused tests GREEN.

### Task 2: V2 queues and isolated workers
- [ ] RED queue routing/isolation/solver-slot tests.
- [ ] Stage-specific Redis queue abstraction and worker entrypoint.
- [ ] Focused tests GREEN.

### Task 3: Whole-field preparing and task API
- [ ] RED tests proving no component decomposition and prepare reuse.
- [ ] Pure whole-field prepare helper and v2 preparing stage.
- [ ] PUT/GET/add-N/cancel v2 routes and real persisted statuses.

### Task 4: Solve/fit/baring per N
- [ ] RED state/status/anchorage tests.
- [ ] Implement isolated solve, fit and task-baring stages.
- [ ] Preserve no-anchorage public geometry and hidden anchorage mass.

### Task 5: Async bars and verification
- [ ] RED contract tests for POST/GET.
- [ ] RED unit conversion/visible-bar verification tests.
- [ ] Implement standalone baring and validation workers/results.

### Task 6: Central settings and S3 solver logs
- [ ] RED effective-config and disabled logging tests.
- [ ] Centralize v2 hard limits/threads.
- [ ] Optional local HiGHS log plus object upload when all four fields are configured.

### Task 7: Kubernetes/KEDA
- [ ] Separate deployments/queues/scaledobjects for five v2 worker types.
- [ ] Validate kustomize/yaml.

### Task 8: Verification
- [ ] Run API/OpenAPI tests.
- [ ] Run v2 pipeline tests.
- [ ] Run full pytest suite.
- [ ] Review diff for v1 regressions, credentials, state races and deployment completeness.
