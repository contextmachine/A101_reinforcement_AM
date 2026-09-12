# Local-N Incremental Results Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make requested N values local per selected solver unit, combine every reachable component total incrementally, preserve best-so-far solutions, and distinguish normal infeasible outcomes from execution errors.

**Architecture:** Component and whole-field scheduling are independent. Component frontier updates continuously feed the existing min-plus combiner without filtering aggregate totals by the input N list. Solutions remain durable and best selection is monotonic by mass.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/PostgreSQL, Redis/KEDA worker queue, pytest.

**Spec:** `docs/superpowers/specs/2026-09-11-local-n-incremental-results-design.md`

## Global Constraints

- Input `n` values are local solver values, never aggregate-total filters.
- `[-3]` = components + aggregate; `[-1]` = direct whole only; `[-2]` = both.
- Direct whole-field N starts at 1 regardless of component count; its upper useful bound is the sum of real-component max-N values.
- Never schedule a local N above that unit's `max_useful_n`.
- Local infeasible N is normal and must not create `completed_with_errors`.
- Aggregate output totals must not pollute `task_n_requests`.
- Existing feasible results remain available and may be improved by later runs.
- No database schema migration.

---

### Task 1: Scheduling semantics

**Files:**
- Modify: `rebar_service/pipeline.py`
- Test: `tests/test_aggregate_component_planning.py`

**Interfaces:**
- Consumes: `component_selection`, component `max_useful_n`, user local N values.
- Produces: component/whole solve jobs capped independently per unit.

- [x] Write failing tests proving `n=[1..5]` queues `1..min(5,max_n)` for each selected real component, `[-1]` queues only whole, and `[-2]` queues both.
- [x] Run targeted tests and verify they fail under aggregate-total scheduling.
- [x] Update `schedule_requested_for_all` and task preparation selection handling.
- [x] Run targeted tests until green.

### Task 2: Unfiltered incremental aggregate totals

**Files:**
- Modify: `rebar_service/pipeline.py`
- Test: `tests/test_aggregate_component_planning.py`

**Interfaces:**
- Consumes: all currently persisted feasible component frontiers.
- Produces: layout candidates for every reachable `total_N`.

- [x] Write failing test where requested local N `[1]` across two components yields aggregate total `2`.
- [x] Write failing test proving a newly added local frontier can improve/create an aggregate total without changing the original local request set.
- [x] Remove requested-total filtering and aggregate-bounds status generation from `handle_combine_frontiers`.
- [x] Verify incremental combine tests pass.

### Task 3: Request/result separation and best-so-far

**Files:**
- Modify: `rebar_service/pipeline.py`
- Modify: `rebar_service/postgres_store.py`
- Test: `tests/test_aggregate_component_planning.py`
- Test: `tests/test_scene_audit_regressions.py`

**Interfaces:**
- Consumes: laid-out component aggregate or whole solution.
- Produces: durable solution plus best-result event without adding aggregate totals to local request plan.

- [x] Write failing test proving component aggregate `total_N` does not call `set_n_status` as a local request status.
- [x] Write failing test proving a worse recomputation cannot replace a better persisted solution with the same identity.
- [x] Make aggregate layout publish/update result without mutating request rows; preserve whole direct request status behavior.
- [x] Make PostgreSQL solution upsert monotonic by feasibility/mass/optimality.
- [x] Verify targeted tests pass.

### Task 4: Early scheduling and completion semantics

**Files:**
- Modify: `rebar_service/pipeline.py`
- Modify: `rebar_service/postgres_store.py`
- Test: `tests/test_aggregate_component_planning.py`
- Test: `tests/test_scene_audit_regressions.py`

**Interfaces:**
- Consumes: completed max-N for one solver unit and queue state.
- Produces: immediate local solve jobs and correct terminal task state.

- [x] Write failing test proving one unit begins solving immediately after its max-N becomes ready instead of waiting for every unit.
- [x] Write failing test proving normal infeasible local results with no job failures end as `completed`, not `completed_with_errors`.
- [x] Schedule initial local plans from max-N handlers; avoid duplicate scheduling at final analysis-prepared transition.
- [x] Base `completed_with_errors` on actual execution failure evidence.
- [x] Verify targeted tests pass.

### Task 5: Regression verification

**Files:**
- Modify docs/tests only if contract descriptions are stale.

- [x] Run aggregate, scene pipeline, manual component, overlay, and result tests.
- [x] Run full `python -m pytest -q` with an external timeout guard and record pass/fail counts.
- [x] Run `python -m compileall -q A101 rebar_service`.
- [x] Package only changed/new files relative to the user's current matrix-barrier/task-component baseline.

### Task 6: Whole-field bound independent of component count

**Files:**
- Modify: `rebar_service/pipeline.py`
- Test: `tests/test_aggregate_component_planning.py`
- Modify: `rebar_service/api_docs_ru.py`

**Interfaces:**
- Consumes: prepared real-component max-N values and whole-field problem.
- Produces: direct whole-field bound `sum(component.max_useful_n)` and whole solve jobs for requested N starting at 1.

- [x] Add regression with 30 real components and requested local N `[1,2,3,4,5]`; prove direct whole schedules all five although aggregate component totals start at 30.
- [x] For `[-1]`, prepare real components in max-bound-only mode without scheduling component solves.
- [x] Finalize waiting whole max-N when the last real component max-N becomes ready.
- [x] Preserve legacy whole max-N behavior for historical tasks.
- [x] Run scheduling and scene regression tests.
