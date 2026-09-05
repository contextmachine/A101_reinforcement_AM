# Overlay-aware analysis design

## Goal

Add append-only polygon overlays to tasks and make `(task_id, variant, overlay_id)` the identity of a prepared analysis. Remove the explicit prepare endpoint; preparation happens lazily on the first task-level solve request and is persisted once.

## Overlay semantics

Overlay events belong to the task/source polygons, not to components. Each event has `id`, `type`, `idxs`, and `real`. Events are applied in append order through the requested event id inclusive. `overlay_id=0` means no overlay.

- `clean, real=false`: polygon is physically removed from the field and from reinforcement demand.
- `clean, real=true`: polygon remains physical field geometry but is removed from additional reinforcement demand (`background_only`).
- `unclean`: polygon returns to active demand; `real` is accepted/stored but does not affect the resulting active state.
- Repeated `unclean` for an already active polygon is a no-op.

Source indices are stable and never renumbered. Overlay application is identical for raw/smooth geometry indices; only loads differ by variant.

## API

- `GET /v1/tasks/{task_id}/source-polygons?smooth=&overlay=` returns the complete stable source polygon list with resolved overlay state metadata.
- `GET /v1/tasks/{task_id}/overlays` returns the append-only event list.
- `POST /v1/tasks/{task_id}/overlays` appends a list of events atomically.
- Remove `POST /components/prepare`.
- Add optional `overlay` query argument to task-level solve, component reads/solves/results, solutions, component-events, compatibility results, and DXF export.
- Component-specific solve requires an already prepared analysis. Task-level `/n` lazily creates/prepares the analysis and schedules requested Ns after preparation.

## Storage

Create `task_overlay_events` and `task_analyses`. Add `overlay_id` to derived tables: `task_n_requests`, `components`, `component_results`, `runtime_artifacts`, `solutions`, and `task_events` (nullable/0-compatible where appropriate for migration). Existing rows become overlay 0.

Prepared fields/problems/artifacts are namespaced by overlay. Raw/smooth source polygon JSON remains in `task_variants` and is never mutated by overlay operations.

## Pipeline

Job payloads and dedupe identities carry `overlay_id`. Preparation resolves overlay state against persisted source polygons, builds demand geometry from active polygons, builds physical field geometry from active + background-only polygons, and excludes removed polygons completely. Rebar configuration is resolved from the unmodified persisted variant for comparability.

Analysis preparation is idempotent. The first task-level solve request persists Ns for `(task, variant, overlay)` and enqueues one prepare job if needed. At the end of component/whole preparation, the persisted requested Ns are scheduled. Later solve requests reuse prepared artifacts.
