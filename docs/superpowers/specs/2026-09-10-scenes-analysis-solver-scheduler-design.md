# Scene / analysis / solver / scheduler redesign

Date: 2026-09-10

## Scope and hard constraint

This package changes persistence, API contracts, solver candidate weighting/filtering,
solution serialization, validation, maximum-N calculation, and Redis scheduling.

**Kubernetes YAML is out of scope. No Kubernetes YAML file may be modified.** If a
runtime/Kubernetes setting is discovered to be necessary, it must be reported in text
only.

The current archive is the migration source. Existing task-upload requests and the
current frontend result-reading flow must remain compatible.

## 1. Scene is persistent source data

A `scene_id` represents only source information derived from an uploaded input:

- original source polygons with stable `source_index`;
- raw geometry;
- smooth geometry;
- stable component membership computed once during scene materialization;
- scene overlay event log.

Component membership is computed once at upload/materialization time and is identical
for raw and smooth variants. Overlay changes never renumber or repartition components,
even when a `clean(real=false)` event physically disconnects part of a component.

Scene preparation is asynchronous. Scene upload stores the input, creates `scene_id`,
enqueues materialization, and returns a preparing state. A worker materializes source
polygons, raw/smooth variants, and components. Starting analysis against a non-ready
scene returns HTTP 409.

### Scene upload endpoints

New endpoints:

- `POST /v1/scenes/dxf_upload`
- `POST /v1/scenes/json_upload`
- `POST /v1/scenes/tables_upload`
- `POST /v1/scenes/pkl_upload`

They only create/materialize a scene. They do not create an analysis task.

Typical response:

```json
{
  "scene_id": "...",
  "state": "preparing"
}
```

Add `GET /v1/scenes/{scene_id}` for scene state and stable component metadata.

### Existing upload compatibility

The following current task-upload endpoints retain their existing request format,
including the exact multipart contract used by the current frontend:

- `POST /v1/tasks/upload`
- `POST /v1/tasks/tables_upload`
- `POST /v1/tasks/json_upload`
- `POST /v1/tasks/pickle_upload`
- existing JSON polygon task creation flow

Internally they create a scene and then create/start a task. Their response gains
`scene_id` while preserving all existing response keys.

`POST /v1/tasks/upload` changes only one default: `whole` defaults to true. An explicit
`whole=false` continues to override it.

## 2. Overlays belong to scenes

Overlay events are stored by `scene_id`, not by task. Existing positive overlay event
IDs remain unchanged and are not ordinal numbers. Event order is append order.

Public overlay selectors support:

- `0`: source state with no overlay events;
- positive value: exact existing overlay event ID;
- `-1`: last event in append order;
- `-2`: previous event;
- etc.

A negative selector is resolved immediately to the real positive overlay event ID.
Responses always expose the resolved real ID. Example: if the last event has
`id=1730005000`, a request with `overlay=-1` returns `overlay_id=1730005000`.

A task created at `overlay=-1` is immutable: it stores the resolved real overlay ID.
Later scene overlay events do not change the task context or its results.

Scene overlay endpoints become the primary API:

- `GET /v1/scenes/{scene_id}/overlays`
- `POST /v1/scenes/{scene_id}/overlays`

Task-level overlay routes may remain as deprecated aliases to `task.scene_id`, but are
not required by the current frontend.

## 3. Scene polygon API

Add a scene polygon endpoint (operation name `scene_polygons`), canonically:

`GET /v1/scenes/{scene_id}/polygons?smooth=<bool>&overlay=<selector>`

It returns all stable source polygons in source order, annotated with overlay state, and
returns the resolved `overlay_id`. It does not filter rows out because indexes must stay
stable.

States:

- `active`: physically present and contributes demand;
- `background_only`: physically present, no supplemental demand;
- `removed`: physical void.

## 4. New immutable analysis task

Add `PUT /v1/tasks` as the primary analysis-start endpoint. It accepts:

- `scene_id`;
- `overlay_id` selector;
- `smooth`;
- calculation config in the same effective form used by the current upload flow;
- explicit list of requested positive `N` values;
- component selection.

Component selection semantics:

- `[-2]`: all stable components plus whole-field solver;
- `[-3]`: all stable components, without whole-field solver;
- `[-1]`: whole-field solver only;
- `[0, 2, 5]`: explicit stable component IDs.

Special values `-1/-2/-3` are valid only as the sole element of the list. Combinations
such as `[-3, 0]` return 422.

The task stores an immutable analysis context:

- `scene_id`;
- `smooth` / variant;
- resolved positive `overlay_id` (or `0`);
- effective configuration;
- requested Ns and selected components.

Response includes at least:

```json
{
  "task_id": "...",
  "scene_id": "...",
  "smooth": true,
  "overlay_id": 1730005000,
  "state": "..."
}
```

## 5. Result identity and compatibility

For new tasks, `task_id` already identifies scene variant and overlay. Primary new
result routes therefore do not require `smooth` or `overlay` to identify results.

Semantics:

- `task_id + N`: best feasible task-wide result for total N, regardless of whether it
  came from the whole-field solver or a combination of component results;
- `task_id + component_id + N`: result for one specific component.

Every analysis/result/status response includes the task's resolved `overlay_id`.

### Legacy frontend compatibility

Existing result-reading endpoints continue accepting the old query shape so the current
frontend does not need to change immediately. Defaults are `smooth=false` and
`overlay=0`.

For new immutable tasks, the task's stored context is authoritative; legacy query
parameters are compatibility/deprecated inputs and must not mutate or reinterpret the
new task.

For migrated old tasks, a canonical compatibility analysis exists at `raw + overlay 0`.
If the old task already has that analysis, use it. Otherwise select a source analysis
by this deterministic priority and copy/relabel it into a canonical `raw/0`
compatibility context without deleting the original historical data:

1. raw + overlay 0;
2. smooth + overlay 0;
3. old initial variant + latest overlay in append order;
4. latest available analysis.

Existing solutions/component results remain readable after migration.

## 6. Database migration

Introduce scene-level persistence rather than deleting old task history. The expected
logical entities are:

- `scenes`;
- `scene_sources` (uploaded source bytes/metadata where required for deferred parsing);
- `scene_variants` (`raw`, `smooth`);
- `scene_components` (stable component -> source indices);
- `scene_overlay_events`;
- task columns linking to `scene_id`, immutable variant, and resolved overlay;
- existing derived task/result tables retained/migrated as needed for compatibility.

For every legacy task, migration creates a scene from its existing source/variants,
moves or copies its overlay history to the scene, links the task to that scene, and
establishes the canonical raw/0 compatibility context described above.

Migration must be idempotent enough for Alembic execution and must not require
recalculation of existing solutions.

## 7. Solver candidate constraints and anchorage

Default `anchor_factor` changes from 32 to 40.

A selectable solver candidate is valid only when its full calculation rectangle lies in
physically existing scene material for the task's selected overlay. `active` and
`background_only` are physical material; `removed`, holes, and outside-scene area are
void. Candidates intersecting void are excluded before MILP selection.

Candidate objective weight must treat anchorage symmetrically for all candidates:
anchorage is assumed on both ends for weight/cost calculation, including candidates at
perpendicular field boundaries where real post-layout geometry may extend beyond the
field. This removes the current artificial edge-candidate mass advantage.

The physical-validity rule and objective anchorage rule are distinct: the candidate's
coverage rectangle must be inside physical material; the cost model still assumes two
anchorage lengths.

## 8. Compact solution representation

Every concrete solution gains an additional compact representation while preserving the
existing full result for compatibility/debugging.

```json
[
  {
    "origin": [1000.0, 2000.0],
    "direction": [0.6, 0.8],
    "length": 2500.0,
    "step": 200.0,
    "right": 2,
    "left": 2,
    "d": 20
  }
]
```

Definitions:

- `origin`: start point of the base bar;
- `direction`: unit vector in the direction in which parallel bars are laid out;
- bar direction = `direction` rotated counter-clockwise by 90 degrees;
- `length`: full **unclipped** base-bar length including anchorage on both ends;
- `step`: bar spacing;
- `right`: number of neighbours in `+direction`, excluding the base bar;
- `left`: number of neighbours in `-direction`, excluding the base bar;
- `d`: diameter.

`count` is intentionally absent; it is `left + right + 1`.

Compact groups must reproduce actual regular tracks exactly. If one existing layout zone
contains irregularly shifted tracks that cannot be represented by one arithmetic
progression, split it into several compact dictionaries rather than losing geometry.

## 9. Solution verification endpoints

Add two verification flows using one shared geometry engine.

### Verify a stored task result

Input identifies `scene_id`, `task_id`, and `N` (plus component ID when explicitly
verifying one component). Task context supplies stored smooth/overlay/config and the
resolved overlay ID is returned.

### Verify arbitrary compact JSON

Input contains:

- `scene_id`;
- `smooth`;
- overlay selector (including negative relative selectors);
- compact `zones` list;
- optional `back_grid`.

If `back_grid` is supplied, it is global background reinforcement and zones are added on
top. If omitted/null, no separate global background is added; the caller is assumed to
have included background reinforcement among the supplied zones.

For a compact zone, build its oriented coverage rectangle as follows:

1. `p_right = origin + direction * right * step`;
2. `p_left = origin - direction * left * step`;
3. shift that segment by `rot90ccw(direction) * length` to obtain the opposite edge;
4. expand the two sides perpendicular to the bar direction by `step/2` along
   `+direction` and `-direction`.

Use the existing reinforcement unit convention/formula:

`As(d, step) = 10 * pi * (d/2)^2 / step`

For every active source polygon `P` with required reinforcement `load`:

`coverage_percent = 100 * (A_bg * area(P) + sum(area(P ∩ Z_i) * As_i)) / (area(P) * load)`

where `A_bg = As(back_grid)` when supplied, otherwise zero.

Overlapping reinforcement zones intentionally add their reinforcement contributions.

Return one value per source polygon in stable source order:

- active -> computed non-negative percentage;
- `background_only` -> exactly `100.0`;
- `removed` -> `null`.

The output also returns `scene_id`, `smooth`, and the resolved real `overlay_id`.

For speed, use Shapely `STRtree`/prepared geometry (already within the geometry stack)
rather than adding an external `rtree` dependency unless profiling proves necessary.

## 10. MILP-derived maximum useful N

Replace the current max-N heuristic/bound for normal component/whole solver scheduling
with the agreed layer-mask MILP calculation.

For each selected component (and whole when selected):

1. resolve load -> reinforcement class -> primitive recipe layers;
2. expand demand into primitive layer masks, preserving shared primitive layers across
   adjacent loads/classes where possible;
3. for every distinct `(primitive class, layer occurrence)` binary mask, generate
   physically valid rectangle cover candidates;
4. solve an exact minimum-cardinality rectangle-cover MILP for that mask;
5. sum those minimum rectangle counts to produce `max_useful_n`;
6. apply the server hard cap: `solver_max_n = min(max_useful_n, 100)`.

This max-N computation is a worker job, not synchronous API work.

No ordinary solver job is scheduled for N above that component/whole bound. The hard
cap 100 applies to one component solver or whole-field solver only. A task-wide result
assembled by combining component results may have total N > 100 and is not itself sent
to a separate rectangle solver.

If a requested component has no physically feasible cover, record an explicit
infeasible preparation/max-N state rather than crashing the worker.

## 11. Queue planning

Task scheduling has two phases.

### Phase A: max-N jobs

Immediately enqueue one max-N computation for every selected stable component and whole
(if selected). Downstream N solve jobs for that unit are not expanded until its max-N is
known.

### Phase B: N solve jobs

For each unit, intersect the user's explicit requested-N set with `1..max_useful_n` and
the hard per-solver limit 100. Do not synthesize values the user did not request.

Order each unit's remaining values from both ends toward the middle. Example:

`[1,2,3,4,5,6,7] -> [1,7,2,6,3,5,4]`

For arbitrary values:

`[2,4,8,12] -> [2,12,4,8]`

Global enqueue is round-robin by position across units. With units comp0, comp1, whole:

- round 1: each unit's lowest requested N;
- round 2: each unit's highest requested N;
- round 3: next-lowest;
- round 4: next-highest;
- continue toward each unit's middle.

There is no execution barrier between rounds: Redis/KEDA workers may complete and claim
jobs concurrently. The requirement concerns enqueue/priority order, not serial
completion.

Queue state/ack operations must preserve the already-agreed atomic Redis transaction
behavior so stale workload entries cannot keep KEDA replicas alive after terminal jobs.

## 12. Error and state behavior

- scene not ready -> 409 when starting analysis;
- invalid special component selection -> 422;
- invalid/missing positive overlay ID -> 404/422 according to existing API error style;
- negative overlay outside available history -> 404/422, never silently clamp;
- impossible reinforcement/candidate cover -> domain infeasible state, not worker crash;
- temporary PostgreSQL recovery continues to use connection-level retry only; SQL
  operations are not blindly replayed.

## 13. Testing requirements

Implementation is test-first and must add/update tests for:

- all four scene uploads and asynchronous materialization;
- exact backwards compatibility of current `/v1/tasks/upload` multipart input;
- `whole=true` default on current upload;
- stable scene components across raw/smooth and overlays;
- positive and negative overlay resolution and snapshot immutability in task;
- new PUT task component selectors (`-1/-2/-3` and explicit IDs);
- new result identity and legacy result query compatibility;
- legacy DB migration/canonical raw-overlay0 fallback priority;
- physical-void candidate exclusion;
- symmetric two-ended anchorage objective and default factor 40;
- compact solution conversion, including irregular-track splitting;
- both verification endpoints and exact null/100 semantics;
- MILP max-N calculation and hard cap 100;
- two-phase max-N-first queue scheduling and edge-to-middle round-robin ordering;
- Redis workload cleanup/atomic ack regression;
- OpenAPI/Swagger Russian descriptions for all new/changed endpoints.

No Kubernetes manifest test should expect any YAML change from this package.
