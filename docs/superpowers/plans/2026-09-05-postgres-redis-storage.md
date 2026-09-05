# PostgreSQL / Redis Storage Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move all durable rebar task state to PostgreSQL while leaving Redis only as the KEDA job queue and worker coordination layer.

**Architecture:** Preserve the storage interface used by the pipeline but implement it as a composition of `PostgresStore` and `RedisQueue`. Persist canonical raw/smooth polygons before scheduling work, store JSON-safe frontiers/solutions in PostgreSQL, and keep only opaque algorithm checkpoints as BYTEA runtime artifacts.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, psycopg 3, Alembic, PostgreSQL 16, Redis 8/KEDA, FastAPI, pytest.

**Spec:** `docs/superpowers/specs/2026-09-05-postgres-redis-storage-design.md`

## Global Constraints

- PostgreSQL is the only durable source of truth.
- Durable task data has no TTL.
- Redis contains only queue/lease/dedupe/pending/slot/reaper state.
- Existing Redis data is not migrated and is flushed during cutover.
- Raw and smooth polygon variants are persisted before worker execution.
- Existing HTTP contracts remain compatible; legacy results are derived from solutions.

---

### Task 1: Database configuration and Alembic schema

**Files:**
- Modify: `requirements.txt`
- Modify: `rebar_service/config.py`
- Create: `rebar_service/database.py`
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/versions/0001_postgres_storage.py`
- Test: `tests/test_postgres_schema.py`

**Interfaces:**
- Produces: `Database(settings)`, `Settings.database_url`, Alembic tables from the approved spec.

- [ ] Write schema/config tests that compile the expected PostgreSQL URL and assert migration table/index names.
- [ ] Run the tests and verify failure before implementation.
- [ ] Add SQLAlchemy, psycopg and Alembic dependencies plus lazy database engine creation.
- [ ] Add the initial Alembic migration for all durable tables and indexes.
- [ ] Run the schema/config tests and verify they pass.

### Task 2: Canonical polygon persistence helpers

**Files:**
- Create: `rebar_service/polygon_storage.py`
- Test: `tests/test_polygon_storage.py`

**Interfaces:**
- Produces: `build_polygon_variants(input_obj)`, `canonicalize_polygons(rows)`, `geometry_polygons(rows)`.

- [ ] Write tests proving raw and smooth are JSON-safe, keep `points/load/color`, and omit Shapely/smoothing-debug fields.
- [ ] Run tests to verify failure.
- [ ] Implement canonicalization, geometry reconstruction and one-time smooth generation.
- [ ] Run tests and verify pass.

### Task 3: PostgreSQL durable store

**Files:**
- Create: `rebar_service/postgres_store.py`
- Modify: `tests/test_smooth_variant_storage.py`
- Modify: `tests/test_store.py`

**Interfaces:**
- Produces: task/meta/plan/N/event/source/variant/component/frontier/artifact/solution methods currently consumed by API and pipeline.

- [ ] Replace Redis-storage-specific tests with architecture tests for durable method ownership and variant behavior.
- [ ] Implement task/source/variant/event/N CRUD using SQL transactions and JSONB.
- [ ] Implement runtime artifact BYTEA encode/decode with SHA-256 validation.
- [ ] Implement component rows plus internal component artifacts.
- [ ] Implement frontier JSONB persistence and variant frontier versioning.
- [ ] Implement canonical solution persistence and compatibility-result derivation.
- [ ] Run store tests.

### Task 4: Redis queue-only store and facade

**Files:**
- Create: `rebar_service/redis_queue.py`
- Replace: `rebar_service/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: `Store(settings)` facade and compatibility alias `RedisStore = Store`; queue methods preserve worker API.

- [ ] Add tests proving workload stores `job_id`, not a second job JSON copy, and durable key patterns are absent.
- [ ] Extract enqueue/claim/heartbeat/ack/requeue/reaper/slots/pending into `RedisQueue`.
- [ ] Implement facade delegation and ping both backends.
- [ ] Run queue/store tests.

### Task 5: Pipeline and API cutover

**Files:**
- Modify: `rebar_service/api.py`
- Modify: `rebar_service/pipeline.py`
- Modify: `rebar_service/worker.py`
- Modify: relevant API/pipeline tests

**Interfaces:**
- Consumes: `Store`, persisted variant polygons, integer PostgreSQL event IDs.
- Produces: same public routes plus `smooth` access to persisted source polygons.

- [ ] Change constructors/imports from Redis-specific store name to `Store`.
- [ ] Make task creation persist variants through the store before bootstrap.
- [ ] Make field preparation load persisted raw/smooth polygons instead of reparsing/re-smoothing input.
- [ ] Make N status/cancellation calls variant-aware.
- [ ] Remove `save_best_result` write from layout; derive legacy result from solution.
- [ ] Replace WebSocket Redis Stream read with PostgreSQL event polling.
- [ ] Run API/pipeline tests.

### Task 6: Kubernetes configuration, migration and clean-cutover scripts

**Files:**
- Modify: `deploy/k8s/base/configmap.yaml`
- Modify: `deploy/k8s/base/api.yaml`
- Modify: `deploy/k8s/base/worker-deployment.yaml`
- Create: `deploy/k8s/base/db-migrate-job.yaml`
- Create: `scripts/migrate-db.sh`
- Create: `scripts/clear-redis.sh`
- Delete: `deploy/k8s/secrets/rebar-secrets.dev.yaml`
- Create: `deploy/k8s/secrets/rebar-secrets.dev.example.yaml`
- Modify: `tests/test_manifests.py`

**Interfaces:**
- Consumes: Secret `a101-postgres-auth` keys `POSTGRES_USER`, `POSTGRES_PASSWORD`.
- Produces: API/worker `REBAR_POSTGRES_*` environment and an explicit one-shot Alembic migration workflow.

- [ ] Add manifest tests for PostgreSQL secret mapping and absence of committed Redis credentials.
- [ ] Update ConfigMap and deployments.
- [ ] Add a migration Job template and scripts for migration/Redis FLUSHDB.
- [ ] Replace committed dev secret with a redacted example.
- [ ] Run manifest tests.

### Task 7: Full verification and operator documentation

**Files:**
- Modify: `README.md`
- Modify: `scripts/verify.sh`

**Interfaces:**
- Produces: exact clean-cutover commands and verification queries.

- [ ] Document build/migrate/flush/deploy ordering and rollback boundary.
- [ ] Run `pytest -q` and static compile checks.
- [ ] Render Kustomize overlays when `kubectl` is available.
- [ ] Package the updated repository for handoff.
