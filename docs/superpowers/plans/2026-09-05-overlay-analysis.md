# Overlay-aware analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add append-only source polygon overlays, lazy one-time preparation, and overlay-scoped solve/results while preserving the existing no-overlay behavior.

**Architecture:** PostgreSQL stores overlay events and analysis state. All derived calculation state is keyed by `(task_id, variant, overlay_id)`. Redis remains queue-only; jobs carry overlay id so KEDA and workers remain stateless with respect to durable analysis data.

**Tech Stack:** Python 3.12+, FastAPI, SQLAlchemy 2, psycopg 3, Alembic, PostgreSQL 16, Redis/KEDA, pytest.

**Spec:** `docs/superpowers/specs/2026-09-05-overlay-analysis-design.md`

## Global Constraints

- Existing `public.*` tables are untouched; application DB objects live under PostgreSQL schema `rebar`.
- Overlay 0 is the backward-compatible base analysis with no overlay events applied.
- Source polygon indices never change after task creation.
- `clean real=false` removes physical geometry; `clean real=true` preserves physical geometry but removes demand.
- `/components/prepare` is removed; task-level `/n` triggers idempotent lazy preparation.
- Existing calls that omit `overlay` continue to target overlay 0.

---

### Task 1: Schema and overlay resolver

**Files:**
- Create: `migrations/versions/0002_overlay_analyses.py`
- Create: `rebar_service/overlays.py`
- Modify: `rebar_service/config.py`
- Modify: `rebar_service/database.py`
- Modify: `migrations/env.py`
- Test: `tests/test_overlay_resolver.py`
- Test: `tests/test_postgres_schema.py`

**Interfaces:**
- Produces: `normalize_overlay_id(value) -> int`, `resolve_overlay(polygons, events, through_id) -> list[dict]`.
- Produces PostgreSQL tables/columns required by overlay-aware storage.

- [ ] Write resolver/schema tests and verify failure.
- [ ] Implement resolver and migration.
- [ ] Verify tests pass.

### Task 2: Overlay-aware PostgreSQL store

**Files:**
- Modify: `rebar_service/postgres_store.py`
- Test: `tests/test_postgres_store_unit.py`
- Create: `tests/test_overlay_store_contract.py`

**Interfaces:**
- Produces overlay event append/list/resolve methods.
- Extends field/component/frontier/artifact/solution/N APIs with optional `overlay_id=0`.
- Produces idempotent analysis state methods: `ensure_analysis`, `analysis_state`, `mark_analysis_preparing`, `mark_analysis_prepared`.

- [ ] Write failing storage contract tests.
- [ ] Implement overlay-aware storage methods and SQL keys.
- [ ] Verify storage tests pass.

### Task 3: Lazy preparation and physical-vs-demand overlay semantics

**Files:**
- Modify: `rebar_service/pipeline.py`
- Test: `tests/test_overlay_pipeline.py`
- Modify: `tests/test_manual_component_api.py`

**Interfaces:**
- Task-level schedule persists Ns for the selected overlay and lazily enqueues prepare once.
- Pipeline jobs propagate `overlay_id` through prepare/solve/fit/combine/layout.
- Prepared field uses active polygons for demand and active+background-only polygons for physical clipping.

- [ ] Write failing lazy-prepare and geometry-semantics tests.
- [ ] Implement overlay-aware pipeline.
- [ ] Verify pipeline tests pass.

### Task 4: API contract

**Files:**
- Modify: `rebar_service/models.py`
- Modify: `rebar_service/api.py`
- Test: `tests/test_overlay_api_contract.py`
- Modify: `tests/test_smooth_api_contract.py`

**Interfaces:**
- Adds `GET/POST /v1/tasks/{task_id}/overlays`.
- Adds `overlay` query parameters to overlay-sensitive endpoints.
- Removes `/components/prepare`.
- Source polygons return stable indices plus overlay state.
- `load_column` remains optional/default empty in upload OpenAPI.

- [ ] Write failing OpenAPI/API tests.
- [ ] Implement endpoints and query routing.
- [ ] Verify API tests pass.

### Task 5: Regression/verification and delivery

**Files:**
- Modify: docs only if verification exposes mismatches.

**Interfaces:**
- Produces a clean repository archive and migration/deploy commands.

- [ ] Run full pytest suite.
- [ ] Compile Python modules.
- [ ] Render Alembic SQL and inspect `0002` operations.
- [ ] Render kustomize manifests.
- [ ] Package modified repository.
