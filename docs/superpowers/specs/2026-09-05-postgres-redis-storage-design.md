# PostgreSQL / Redis Storage Split Design

## Goal

Make PostgreSQL the durable source of truth for rebar tasks while keeping Redis only for KEDA-visible job queues and short-lived worker coordination.

## Retention

Tasks, raw/smooth source polygons, component metadata, component frontier results, solutions, source DXF bytes, and events are retained indefinitely in PostgreSQL. Solver-result and candidate runtime artifacts are removed after they are consumed successfully. Field, component, and prepared-problem runtime artifacts remain available so an old task can accept additional N values later.

## PostgreSQL tables

- `tasks`: task lifecycle and immutable/request configuration; `generation` and pause/cancel state.
- `task_sources`: original source kind/name and binary DXF content when required for later DXF export.
- `task_variants`: one `raw` and one `smooth` row per task containing canonical polygon JSONB and variant-level preparation/frontier state.
- `task_n_requests`: requested N values and status per task + variant.
- `components`: public/preparation metadata per task + variant + component. Whole-field component uses `component_id=-1`.
- `component_results`: one JSON-safe frontier result per component + N.
- `runtime_artifacts`: opaque `pickle+zstd`/`pickle+zlib` BYTEA for field, component, problem, solver-result and candidate internals.
- `solutions`: canonical JSON-safe final solutions. No separate legacy result storage.
- `task_events`: durable ordered event log.

## Polygon variants

Task creation parses input once to canonical raw polygons in millimetres. Smooth polygons are computed once from raw using the existing `smooth_load` algorithm. Both canonical lists are persisted before the first worker job. Workers rebuild Shapely geometry from the persisted variant, so repeated preparation does not reparse DXF or recompute smoothing.

## Runtime artifact deduplication

The field artifact omits `start_polygons` (persisted in `task_variants`) and omits the component list from decomposition (persisted as component rows/artifacts). Component artifacts keep only the internal component object; public state/plan/bounds live in `components`. Solver-result artifacts are deleted when the frontier result is saved. Candidate artifacts are deleted when a final solution is saved.

## Redis

Redis keys are restricted to:

- `rebar:jobs:ready`: full job JSON waiting to be claimed.
- `rebar:jobs:processing`: full job JSON currently claimed.
- `rebar:jobs:workload`: job IDs only; KEDA uses only list length.
- `rebar:job:<id>` and `rebar:job:<id>:lease`: transient worker state and lease.
- `rebar:job-dedupe:<hash>`: enqueue dedupe.
- `rebar:task:<id>:pending`: outstanding job IDs.
- `rebar:task:<id>:slots`: concurrency leases.
- `rebar:lock:queue-reaper`: queue recovery lock.

No task metadata, polygons, blobs, results, events, generation, cancellation or frontier indices live in Redis.

## API compatibility

Existing task/component/solution endpoints remain. `/results/{n}` is derived from the canonical solution via `to_compat_result`; no compatibility result copy is stored. `/source-polygons` reads persisted raw polygons and accepts the existing/new `smooth` query to return the persisted smooth variant. WebSocket events poll PostgreSQL by monotonically increasing event ID rather than Redis Streams.

## Connection and deployment

PostgreSQL 16 is reached at `a101-postgres:5432`, database `a101`, namespace `rebar-optimizer`. `POSTGRES_USER` and `POSTGRES_PASSWORD` come from existing Secret `a101-postgres-auth` and are mapped into `REBAR_POSTGRES_USER` / `REBAR_POSTGRES_PASSWORD` in API, worker and migration Job manifests. SQLAlchemy 2 + psycopg 3 provide synchronous access; Alembic owns schema migrations.

The existing Redis database is intentionally discarded at cutover. PostgreSQL data is not migrated from Redis.
