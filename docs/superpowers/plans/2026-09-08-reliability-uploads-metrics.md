# Reliability, Upload Split, and Layout Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make worker recovery and capacity failures reliable, split source upload endpoints, reduce API memory use, and expose correct anchored/unclipped layout metrics.

**Architecture:** PostgreSQL remains the durable source of truth and Redis remains queue-only. Upload endpoints store opaque source bytes immediately; worker source materialization converts them to canonical raw/smooth polygons. List APIs query SQL summary columns, while full solution JSON is loaded only when required.

**Tech Stack:** Python 3.12+, FastAPI, SQLAlchemy/psycopg, PostgreSQL 16, Redis/KEDA, Shapely, NumPy, openpyxl, pytest.

**Spec:** `docs/superpowers/specs/2026-09-08-reliability-uploads-metrics-design.md`

## Global Constraints

- Preserve legacy DXF multipart fields `config` and `file`.
- Do not alter N semantics or recipe/zone merging.
- Do not use unrestricted pickle deserialization.
- No PostgreSQL schema migration is required.

---

### Task 1: Solution summary queries and API memory
**Files:** `rebar_service/postgres_store.py`, `deploy/k8s/base/api.yaml`, `tests/test_solution_summary_queries.py`

- [ ] Add a failing store test proving list/snapshot paths do not select `result` JSONB.
- [ ] Add `solution_summaries()` and make `best_solution()` issue a single ordered `SELECT result ... LIMIT 1`.
- [ ] Route result metadata/snapshot/list-solutions to summaries.
- [ ] Set API requests to 1Gi and limits to 4Gi.
- [ ] Run focused tests and full pytest.

### Task 2: Capacity infeasibility
**Files:** `A101/calculate_mass.py`, `rebar_service/pipeline.py`, `tests/test_capacity_infeasible.py`

- [ ] Add a failing test for an unsupported load.
- [ ] Add `ReinforcementCapacityError` carrying load/capacity context.
- [ ] Catch it in prepare, mark analysis/N statuses infeasible, publish `analysis_infeasible`, and return without exception.
- [ ] Run focused tests and full pytest.

### Task 3: Idempotent fit recovery
**Files:** `rebar_service/pipeline.py`, `rebar_service/postgres_store.py`, `tests/test_fit_recovery.py`

- [ ] Add failing tests for replay after frontier persistence and replay with missing solver artifact.
- [ ] Detect existing frontier and resume downstream scheduling.
- [ ] If no frontier and no solver artifact, enqueue solve again when problem exists.
- [ ] Move solver-artifact cleanup after downstream persistence/scheduling.
- [ ] Run focused tests and full pytest.

### Task 4: Split uploads and worker source materialization
**Files:** `rebar_service/api.py`, `rebar_service/postgres_store.py`, `rebar_service/pipeline.py`, `rebar_service/source_polygons.py`, `rebar_service/safe_pickle.py`, `tests/test_upload_split.py`, `tests/test_pickle_upload.py`

- [ ] Add API contract tests for four upload endpoints and rejection of wrong formats.
- [ ] Add restricted pickle loader tests using the supplied NumPy/Shapely shape.
- [ ] Store all source bytes/metadata as deferred `source_pending` variants.
- [ ] Generalize worker materialization for DXF/XLSX bundle/JSON/pickle.
- [ ] Ensure `start=false` queues source materialization only; `start=true` continues prepare/solve.
- [ ] Run supplied pickle through the restricted loader in a test and full pytest.

### Task 5: Anchored and unclipped metrics
**Files:** `A101/axis_orientation.py`, `A101/rebar_field_layout.py`, `rebar_service/pipeline.py`, `tests/test_layout_mass_metrics.py`

- [ ] Add failing tests showing pre-anchorage and anchored bounds differ and unclipped mass exceeds/equal clipped near a boundary.
- [ ] Preserve unclipped anchored bounds in `add_box_anchorage()`.
- [ ] Build zone-level clipped/unclipped pre/post anchorage bar metrics without changing canonical layout.
- [ ] Add four solution mass metrics and fix compatibility zone fields.
- [ ] Run focused tests and full pytest.
