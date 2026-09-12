# Scenes / Analysis / Solver / Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate reusable scene source data from immutable analysis tasks, add scene uploads/overlays/verification, make task results context-stable, and update solver max-N/candidate scheduling without breaking the current upload/result frontend contract.

**Architecture:** PostgreSQL gains scene-owned source/variant/component/overlay tables while tasks keep immutable `scene_id + variant + resolved_overlay_id` context. Scene materialization is a worker job; analysis workers consume already-materialized scene geometry and enqueue a two-phase max-N then edge-to-middle solve plan. Existing task upload routes remain adapters that create a scene plus task, and legacy task result routes keep accepting old query parameters while canonicalizing migrated history to raw/overlay-0.

**Tech Stack:** Python 3.12, FastAPI/Pydantic, SQLAlchemy/PostgreSQL/Alembic, Redis/KEDA queue semantics, Shapely 2.1 STRtree, PuLP/HiGHS, pytest.

**Spec:** `docs/superpowers/specs/2026-09-10-scenes-analysis-solver-scheduler-design.md`

## Global Constraints

- Do not modify any Kubernetes YAML/manifests under `deploy/` or elsewhere.
- Existing `/v1/tasks/upload` multipart request shape remains accepted unchanged.
- Existing task result-reading routes retain legacy `smooth`/`overlay` query parameters; new task context remains authoritative.
- Default task-upload `whole` becomes `true`; explicit `whole=false` still wins.
- Default `anchor_factor` becomes `40.0`.
- Positive overlay IDs are opaque existing event IDs; negative selectors resolve by append order and responses return the resolved real ID.
- Stable scene component membership is computed once and is not recomputed for smooth or overlays.
- Per-solver hard N cap is 100; combined task-wide N may exceed 100.
- Redis terminal acknowledgement remains atomic via `pipeline(transaction=True)`.

---

### Task 1: Scene persistence and legacy migration

**Files:**
- Create: `migrations/versions/0003_scenes_immutable_tasks.py`
- Modify: `rebar_service/postgres_store.py`
- Test: `tests/test_scene_store_contract.py`
- Test: `tests/test_scene_migration_contract.py`

**Interfaces:**
- Produces: `create_scene(scene_id, meta, input_obj)`, `get_scene(scene_id)`, `set_scene_state`, `load_scene_variant_polygons`, `save_scene_variant`, `save_scene_components`, `scene_components`, `scene_overlay_events`, `append_scene_overlay_events`, `resolve_scene_overlay_id`, `resolved_scene_polygons`.
- Produces task metadata keys: `scene_id`, `analysis_variant`, `analysis_smooth`, `analysis_overlay_id`, `component_selection`.

- [ ] Write migration/store contract tests asserting scene tables, task context columns, scene overlay FK/indexes, and canonical legacy raw/0 migration SQL priority.
- [ ] Run focused tests and verify they fail because scene persistence is absent.
- [ ] Add Alembic revision `0003` creating `scenes`, `scene_sources`, `scene_variants`, `scene_components`, `scene_overlay_events`; add nullable task context columns, backfill one scene per legacy task, copy task source/variants/components/overlays, establish canonical raw/0 compatibility analysis, then make `tasks.scene_id` non-null.
- [ ] Add scene CRUD/resolution methods to `PostgresStore`; negative overlay resolution indexes append-ordered events and raises on out-of-range selectors.
- [ ] Run focused tests and the existing PostgreSQL contract tests.

### Task 2: Scene materialization and upload API

**Files:**
- Modify: `rebar_service/models.py`
- Modify: `rebar_service/api.py`
- Modify: `rebar_service/pipeline.py`
- Modify: `rebar_service/worker.py`
- Modify: `rebar_service/api_docs_ru.py`
- Test: `tests/test_scene_api.py`
- Test: `tests/test_upload_documented_contract.py`
- Test: `tests/test_upload_split.py`

**Interfaces:**
- Produces models `SceneCreated`, `SceneInfo`.
- Produces job kind `materialize_scene` whose payload contains `scene_id` only.
- Produces routes `POST /v1/scenes/dxf_upload`, `/json_upload`, `/tables_upload`, `/pkl_upload`, `GET /v1/scenes/{scene_id}`, `GET /v1/scenes/{scene_id}/polygons`, `GET/POST /v1/scenes/{scene_id}/overlays`.

- [ ] Add failing API/worker tests for asynchronous scene creation, stable raw/smooth component membership, scene state, polygons and relative overlays.
- [ ] Run the focused tests to verify the new routes/job are missing.
- [ ] Refactor source materialization so a scene worker parses source bytes once, writes stable source polygons, raw/smooth variants, computes component membership once from source/raw identity, and marks the scene ready.
- [ ] Add four `/v1/scenes/*_upload` adapters preserving deferred parsing; they return `scene_id` and `state=preparing`.
- [ ] Keep task upload multipart shapes unchanged; internally create a scene and task, add `scene_id` to `TaskCreated`, and set upload `whole=true` only when caller/config did not explicitly set it.
- [ ] Run upload, scene and OpenAPI focused tests.

### Task 3: Immutable PUT task start and result compatibility

**Files:**
- Modify: `rebar_service/models.py`
- Modify: `rebar_service/api.py`
- Modify: `rebar_service/postgres_store.py`
- Modify: `rebar_service/pipeline.py`
- Test: `tests/test_task_start_api.py`
- Test: `tests/test_result_context_compat.py`

**Interfaces:**
- Produces `TaskStartRequest` with `scene_id`, `overlay_id`, `smooth`, `n`, `components`, and the same solver/rebar config fields as `TaskParameters`.
- `components=[-2]`: all + whole; `[-3]`: all without whole; `[-1]`: whole only; otherwise explicit nonnegative IDs. Special values cannot be mixed.
- Produces `PUT /v1/tasks` returning task/scene/resolved overlay/smooth/state.
- Result read methods resolve immutable task context first and use legacy query inputs only for legacy compatibility rows.

- [ ] Add failing selector validation and immutable snapshot tests, including `overlay=-1` resolving to a positive event ID and staying fixed after new events.
- [ ] Add failing legacy result tests for raw/0 defaults and new-task authoritative context.
- [ ] Implement task-start validation, scene-ready 409, component existence validation, resolved overlay snapshot storage and task creation from scene data.
- [ ] Update result/component/solution/event routes and store lookups so returned envelopes include resolved `overlay_id`; keep old query parameters accepted.
- [ ] Run focused task/result tests and existing overlay/smooth tests.

### Task 4: Compact bar-zone serialization and solution verification

**Files:**
- Create: `rebar_service/compact_zones.py`
- Create: `rebar_service/solution_verification.py`
- Modify: `rebar_service/models.py`
- Modify: `rebar_service/api.py`
- Modify: `rebar_service/pipeline.py`
- Test: `tests/test_compact_zones.py`
- Test: `tests/test_solution_verification.py`

**Interfaces:**
- Produces `compact_zones_from_layout(layout) -> list[dict]` with keys exactly `origin,direction,length,step,right,left,d`; no `count`.
- Produces `verify_compact_zones(polygons, zones, back_grid=None) -> list[float | None]` using Shapely STRtree and the agreed rectangle formula.
- Produces stored-solution and arbitrary-JSON verification endpoints; responses include `scene_id`, `smooth`, resolved `overlay_id`, and stable polygon-order `coverage`.

- [ ] Write failing compact conversion tests for regular and irregular track groups and full unclipped anchored length.
- [ ] Write failing verification tests for overlap addition, omitted/present background, active/background_only/removed values, rotated directions and relative overlay resolution.
- [ ] Implement compact conversion from final layout track metadata, splitting non-arithmetic track sequences exactly.
- [ ] Implement oriented zone rectangles, reinforcement area formula and STRtree intersection accumulation.
- [ ] Attach `compact_zones` to concrete saved solutions and add both verification API flows.
- [ ] Run focused tests and existing layout/unclipped regression tests.

### Task 5: Physical candidate filtering and symmetric anchorage objective

**Files:**
- Modify: `rebar_service/pipeline.py`
- Modify: `A101/reinforcement_components.py`
- Modify: candidate generation/solver module selected by `prepare_component_problem` after code inspection.
- Modify: `rebar_service/models.py`
- Test: `tests/test_solver_physical_candidates.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Solver preparation receives/derives physical material geometry where active + background_only are material and removed/outside are void.
- Candidate cost assumes `2 * anchor_factor * diameter` added to bar length regardless of boundary location.
- Default `TaskParameters.anchor_factor == 40.0`.

- [ ] Add failing tests proving candidates crossing void are excluded and edge/interior equal geometry have symmetric anchorage cost.
- [ ] Add failing default-anchor test expecting 40.
- [ ] Pass physical material mask/geometry into candidate generation and reject any coverage rectangle not covered by material geometry.
- [ ] Replace field-clipped anchorage objective length with two-ended nominal anchorage for every candidate.
- [ ] Run solver-focused tests and existing N=1/layout/rebar-config regressions.

### Task 6: Exact layer-mask MILP maximum N

**Files:**
- Create: `A101/max_n_milp.py`
- Modify: `rebar_service/pipeline.py`
- Modify: `rebar_service/models.py` only if result/state models need fields.
- Test: `tests/test_max_n_milp.py`

**Interfaces:**
- Produces `estimate_max_useful_n(prepared, *, recipes, physical_geometry, hard_cap=250) -> dict` with `max_useful_n`, per-layer counts and feasibility.
- Max-N job is explicit worker job and stores component/whole bound before solves are expanded.

- [ ] Add failing small exact-cover tests where minimum rectangles are analytically known and tests for hard cap 100/infeasible physical cover.
- [ ] Implement recipe primitive-layer expansion and candidate rectangle set for each binary demand mask.
- [ ] Solve minimum-cardinality set cover with PuLP/HiGHS; surface infeasible as domain state rather than exception.
- [ ] Integrate as `compute_max_n_component` / `compute_max_n_whole` worker jobs and persist bounds.
- [ ] Run max-N and existing capacity-infeasible tests.

### Task 7: Two-phase edge-to-middle round-robin scheduler

**Files:**
- Modify: `rebar_service/planner.py`
- Modify: `rebar_service/pipeline.py`
- Test: `tests/test_planner.py`
- Create: `tests/test_round_robin_scheduler.py`

**Interfaces:**
- Produces `edge_to_middle_order(values) -> list[int]`.
- Produces `round_robin_unit_plans(dict[unit, list[int]])` preserving unit order and interleaving by plan position.
- Analysis start enqueues only max-N jobs first; after all selected unit bounds are present, one scheduler expansion enqueues solve jobs in the global interleaved order.

- [ ] Add failing order tests `[1..7] -> [1,7,2,6,3,5,4]` and `[2,4,8,12] -> [2,12,4,8]`, plus unequal unit maxima.
- [ ] Add failing pipeline test proving max-N jobs precede all solve jobs and user-unrequested Ns are never synthesized.
- [ ] Implement planner helpers and analysis-level persisted max-N readiness barrier.
- [ ] Enqueue solve jobs in edge-to-middle round-robin order once all selected bounds resolve; preserve Redis FIFO semantics used by current queue.
- [ ] Run planner/pipeline/Redis queue regression tests.

### Task 8: Swagger/docs and full regression verification

**Files:**
- Modify: `rebar_service/api_docs_ru.py`
- Modify: `README.md` only for public API examples if needed.
- Test: `tests/test_openapi_ru.py`
- Test: all tests.

**Interfaces:**
- OpenAPI documents scene lifecycle, immutable task context, component selectors, relative overlays, verification formula/semantics and compact zone schema.

- [ ] Add/adjust failing OpenAPI assertions for all new and changed routes/models.
- [ ] Update Russian Swagger operation descriptions/examples without changing Kubernetes manifests.
- [ ] Run `python -m pytest` and require all tests pass.
- [ ] Run `python -m compileall rebar_service A101 migrations`.
- [ ] Verify `git diff -- deploy` equivalent is empty by hashing `deploy/` before/after in this archive workspace.
