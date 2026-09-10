# Task-scoped Components and Virtual Whole Aggregate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make calculation components task-scoped after background reinforcement is known, replace new-task `whole/-1` solver work with a virtual aggregate, and treat infeasible as an N-local outcome rather than a component failure.

**Architecture:** Scene upload stores only source plus raw/smooth polygon variants. Task creation synchronously builds the field/decomposition from scene+config+overlay in the API process, persists real task components, validates selectors, and only then queues component preparation/solver work. Whole `-1` is represented virtually from real component frontiers; legacy whole/materialize handlers remain dispatchable for old jobs.

**Tech Stack:** Python 3.12+, FastAPI, SQLAlchemy/PostgreSQL, Redis/KEDA queue, Shapely, existing A101 reinforcement component solver, pytest.

**Spec:** `docs/superpowers/specs/2026-09-10-task-components-aggregate-whole-design.md`

## Global Constraints

- New scenes must not populate `scene_components`.
- Real task component IDs are `0..K-1` after background filtering.
- New tasks must never enqueue `prepare_whole`, `compute_max_n_whole`, `solve_whole`, or `fit_whole`.
- With one real component, `-1` is an API alias to component `0` and must not duplicate jobs or stored component rows.
- Component-level solver infeasible is stored only for `(component_id, N)` and must not mark the component broken.
- `max_useful_n=0` is reserved for a genuinely empty analysis with zero real components; component preparation failures must not publish zero as a valid max-N.
- Aggregate requested total `T` uses feasible local `n_i` combinations satisfying `sum(n_i)=T`; infeasible local N values are ignored.
- Legacy job kinds remain dispatchable for old queued records.
- No KEDA, Redis topology, or Kubernetes manifest change is part of this feature.

---

### Task 1: Remove scene-level calculation components

**Files:**
- Modify: `rebar_service/postgres_store.py`
- Test: `tests/test_scene_store_contract.py`
- Test: `tests/test_scene_api.py`

**Interfaces:**
- Consumes: `PostgresStore.create_scene(scene_id, meta, input_obj)`.
- Produces: ready scenes with raw/smooth variants and zero new `scene_components` rows.

- [x] Add a failing store-contract test asserting `create_scene()` does not call/build/insert stable scene components and `get_scene()` remains valid with an empty `components` list.
- [x] Run the targeted scene tests and verify the new assertion fails because current `create_scene()` inserts `scene_components`.
- [x] Remove stable-component generation/inserts from the new `create_scene()` path while retaining `save_scene_components()` and `ensure_scene_variants()` for legacy compatibility.
- [x] Run targeted scene tests until green.

### Task 2: Build task decomposition synchronously before worker jobs

**Files:**
- Modify: `rebar_service/pipeline.py`
- Modify: `rebar_service/api.py`
- Test: `tests/test_task_start_api.py`
- Test: `tests/test_manual_component_api.py`
- Test: `tests/test_overlay_pipeline.py`

**Interfaces:**
- Produces: `PipelineWorkflow.prepare_task_components(task_id, *, auto_solve, smooth, overlay_id) -> dict` that resolves config/background, builds field/decomposition, stores real components, validates selection, and queues only `prepare_component` jobs.
- Legacy `prepare_task()` remains available and can continue to enqueue `prepare_field` for old callers/jobs.

- [x] Add failing API/workflow tests asserting new task creation produces stored task components before returning and never queues `prepare_field`/whole jobs.
- [x] Add failing tests for explicit selector validation after decomposition and background-only polygons producing zero components.
- [x] Extract the light body of `handle_prepare_field()` into an internal builder that can operate synchronously, disable `scene_components` stable-def reuse for new-task calls, and persist `field` plus real components.
- [x] Make `_build_task()` call the synchronous preparation method after task insertion and map selector validation failures to the existing 422 path.
- [x] Keep `handle_prepare_field()` delegating to the shared builder for legacy jobs.
- [x] Run targeted API/pipeline tests until green.

### Task 3: Make whole `-1` a virtual aggregate and single-component alias

**Files:**
- Modify: `rebar_service/pipeline.py`
- Modify: `rebar_service/api.py`
- Modify: `rebar_service/postgres_store.py` only if a small helper is needed
- Test: `tests/test_manual_component_api.py`
- Test: `tests/test_scene_pipeline_end_to_end.py`

**Interfaces:**
- Produces: `PipelineWorkflow.aggregate_component_info(...)` and `resolve_component_alias(...)` behavior.
- `GET .../components` returns only real stored components.
- `GET .../components/-1` returns component `0` metadata for K=1, virtual aggregate metadata for K>1, and 404/empty-analysis metadata as specified for K=0.

- [x] Add failing tests proving K=1 exposes only component `0`, `-1` reads the same component without a stored `-1`, and no whole job kinds are queued.
- [x] Add failing tests proving K>1 aggregate max-N equals the sum of real prepared max-N values.
- [x] Implement virtual aggregate metadata and alias resolution without writing a `components.component_id=-1` row for new tasks.
- [x] Update component endpoints and scheduling to route `-1` through aggregate/alias logic.
- [x] Run targeted component API and end-to-end tests until green.

### Task 4: Plan aggregate totals from local component ranges

**Files:**
- Modify: `rebar_service/pipeline.py`
- Test: `tests/test_round_robin_scheduler.py`
- Create/Modify: `tests/test_aggregate_component_planning.py`

**Interfaces:**
- Produces: pure helper `aggregate_local_n_requirements(maxima: Mapping[int, int], totals: Sequence[int]) -> tuple[dict[int, list[int]], list[int]]`.
- Returns local N values required to construct requested totals plus structurally unreachable totals.

- [x] Add failing unit tests for two-component examples such as max `{0:7,1:11}`, total `10` requiring ranges `0:1..7`, `1:3..9`, and unreachable totals outside `[K, sum(max)]`.
- [x] Implement the range formulas from the design and deterministic edge-to-middle ordering per component.
- [x] Change `schedule_requested_for_all()` so selections `[-1]`/`[-2]` schedule local component N requirements for requested aggregate totals rather than scheduling `solve_whole`.
- [x] Preserve `[-3]`/explicit-component scheduling semantics for real components.
- [x] Run scheduler tests until green.

### Task 5: Make solver infeasible local to N

**Files:**
- Modify: `rebar_service/pipeline.py`
- Modify: `rebar_service/worker.py` only if exception classification needs adjustment
- Test: `tests/test_aggregate_component_planning.py`
- Test: `tests/test_fit_recovery.py`

**Interfaces:**
- `save_frontier_result(... is_feasible=False ...)` is a normal completed frontier row.
- Aggregate combination ignores infeasible local rows and marks only unreachable requested aggregate totals infeasible.

- [x] Add failing tests where component 0/N=3 is infeasible but N=2/N=4 still schedule/combine, and another component continues normally.
- [x] Add failing test where aggregate total 11 is infeasible while totals 10 and 12 can still produce candidates.
- [x] Remove component-level state transitions that convert one max/candidate-cover failure into `max_useful_n=0` for non-empty components; represent preparation failure distinctly (`max_useful_n=None`) while keeping zero only for zero-component analysis.
- [x] Ensure combine logic filters `is_feasible=false` rows rather than abandoning a component if at least one useful local result exists, and updates requested aggregate N statuses independently.
- [x] Run targeted infeasible/recovery tests until green.

### Task 6: Documentation and regression verification

**Files:**
- Modify: `rebar_service/api_docs_ru.py`
- Modify relevant tests: `tests/test_openapi_ru.py`, `tests/test_scene_pipeline_end_to_end.py`, `tests/test_scene_audit_regressions.py`

**Interfaces:**
- Public docs explain task-scoped components, virtual `-1`, and N-local infeasible semantics.

- [x] Update OpenAPI/Russian docs and contract tests for scene-without-components and virtual aggregate behavior.
- [x] Run `python -m compileall -q rebar_service A101`.
- [x] Run `python -m pytest -q` and require zero failures (the existing environment-dependent PostgreSQL integration test may remain skipped).
- [x] Package the modified source tree and a focused patch against the previously supplied API-side-preparation archive.
