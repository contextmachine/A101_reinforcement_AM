# /v2 API, whole-field pipeline, isolated workers

Date: 2026-09-12. Branch: `v2`. Contract: `payload-v2-am-aa.md`. Requirements: `changes 2026-09-12.md`.

This document records the design decisions taken for the `/v2` rework and the points that
were confirmed with the product owner before implementation. Sections marked **(Q)** were
open questions; the chosen answer is recorded next to them.

## 1. Scope

* New minimalist `/v2` API exactly as in `payload-v2-am-aa.md`.
* Whole-field optimisation only: no components, no frontier combination, no aggregates.
* Per-N states `pending → preparing → solving → fitting → bars → success | error | cancelled`
  and a per-N `status` `optimal | feasible | infeasible` (the contract spells `feasable` /
  `infeasable`; the wire format uses the contract spelling).
* Zone/bar geometry is stored and transferred **without anchorage**; anchorage is a pair of
  numbers `{start, end}` on every zone and bar and is only used by the solver objective, by
  the mass metrics and by the frontend for colouring.
* Two new isolated worker types with their own Redis queues, entrypoints and KEDA
  `ScaledJob`s: `/v2/bars` (layout only) and `/v2/verification` (layout + per-polygon
  reinforcement).
* Every deployment-control knob comes from the `rebar-config` ConfigMap only; caps that
  could reject a ConfigMap value are removed.
* HiGHS writes `<app>/logs/{task_id}/{n}/{worker-id}.log` on a shared CSI-S3 PVC.

## 2. Package layout

```
rebar_service/
  config.py                 env-only Settings (pruned)
  redis_queue.py            RedisQueue(settings, names=QueueNames)  – three queue name sets
  worker_loop.py            generic claim/heartbeat/ack loop + reaper, exit-when-idle mode
  worker.py                 entrypoint: main solver worker (queue rebar:jobs:*)
  bars_worker.py            entrypoint: /v2/bars worker (queue rebar:bars:*)
  verification_worker.py    entrypoint: /v2/verification worker (queue rebar:verification:*)
  v2/
    models.py               pydantic contracts (FEPolygon, Overlay, RcVariant, Zone*, Bar, …)
    api.py                  APIRouter mounted on the FastAPI app
    store.py                PostgreSQL access for v2 tables
    scenes.py               upload parsing → scene, overlay POST/GET, polygons GET
    pipeline.py             task stages: prepare / solve / fit / bars
    bars.py                 zones ↔ layout boxes, layout_rebars call, Bar/Zone serialisation,
                            v2 mass metrics (shared by task `bars` stage and both workers)
    verification.py         per-polygon need/fact in cm²/m and kg/m³
    solver_logs.py          HiGHS log path helper
migrations/versions/0005_v2_tasks.py
deploy/k8s/base/{logs-pvc.yaml, bars-scaledjob.yaml, verification-scaledjob.yaml, ...}
```

The A101 solver/layout modules stay where they are; they get small additive changes
(HiGHS `log_file`, split of `prepare_component_problem`, unclipped anchorage cost).

## 3. Storage (migration 0005, additive)

```
v2_tasks(id, scene_id → scenes, overlay_id, smooth, config jsonb, state, error,
         max_useful_n, min_useful_n, generation, cancelled, created_at, updated_at)
v2_task_ns(task_id, n, state, status, fun, mass_metrics jsonb, result jsonb, error,
           created_at, updated_at)             PK(task_id, n)
v2_artifacts(task_id, key, codec, payload bytea, created_at)   PK(task_id, key)
v2_bar_tasks(id, scene_id, overlay_id, smooth, config jsonb, zones jsonb, state, error,
             result jsonb, created_at, updated_at)
v2_verification_tasks(id, …same columns…)
```

`v2_artifacts` keys: `field` (physical/demand polygons, cfg, field geometry), `problem`
(prepared solver model, `pickle+zstd` via `safe_pickle`), `solver:{n}` (transient),
`fit:{n}` (unanchored fitted rectangles; the "zones without anchorage").
`v2_task_ns.result` holds `{bars, zones}` of the finished N.

Scenes and overlay events reuse the existing `scenes`, `scene_sources`, `scene_variants`,
`scene_overlay_events` tables. `scene_overlay_events` gains a nullable `client_time` column.

## 4. Task pipeline (main worker)

Job kinds on `rebar:jobs:*`: `v2_prepare(task)`, `v2_solve(task, n)`, `v2_fit(task, n)`,
`v2_bars(task, n)`.

* **PUT /v2/tasks** validates the request, resolves the overlay selector to a real id, stores
  the task and its N rows (`pending`), enqueues `v2_prepare`.
* **preparing** (`v2_prepare`): all N rows → `preparing`; resolve scene polygons for
  `(smooth, overlay_id)`; `resolve_rebar_config` + `class_holds`; whole-field demand
  component; grid + work matrix (`prepare_component_grid`, the first half of the old
  `prepare_component_problem`); `estimate_max_useful_n(work_matrix, recipes, cap=env)`
  → `max_useful_n`; `prepare_rectangle_problem(max_n=max_useful_n)`; `component_n_bounds`
  → `min_useful_n`; save `field`/`problem`; task state `ready`. Then, under a task row lock,
  every `pending/preparing` N is scheduled: `n < min_useful_n` or `n > max_useful_n` →
  finished immediately as `success`/`infeasable` (see Q4); otherwise → `solving` and
  `v2_solve` enqueued. Capacity errors (`ReinforcementCapacityError`, `CandidateCoverInfeasible`)
  finish every N as infeasible with a reason; unexpected exceptions → every N `error`.
* **solving** (`v2_solve`): `solve_component_frontier(problem, [n], timeout=env,
  solver_time_limit=config.solver.solver_time_limit or env, threads=env, backend=env,
  highs_options={log_file, output_flag, log_to_console})`; stores `solver:{n}`, records
  `fun = total_cost` and `status`; infeasible → `success`/`infeasable`; else → `fitting`.
* **fitting** (`v2_fit`): `fit_component_frontier` (env time limit/backend/threads); stores
  the **unanchored** fitted rectangles (`fitted_bounds`, class, d, step) as `fit:{n}`; → `bars`.
* **bars** (`v2_bars`): `v2.bars.layout_zones(...)` on the fitted rectangles converted to
  zones; stores `{bars, zones}` and `mass_metrics`; → `success`.
* **PUT /v2/tasks/{id}/n** inserts new N rows; if the task is `ready` they are scheduled
  immediately (same lock as above), otherwise they stay `pending` until preparing finishes.
* **PUT /v2/tasks/{id}/cancel** marks the given N (or all N) `cancelled`; every stage checks
  the flag before starting and discards its result if the flag was set meanwhile.

## 5. Zones, bars, anchorage, mass

Geometry conventions (from `compact_zones.py`, kept): `direction` is the transverse unit
vector, bar direction is `direction` rotated 90° CCW; vertical bars use `direction=(1,0)`,
horizontal bars use `direction=(0,-1)` and the origin is the start of the base bar.

* A `ZoneAdditional` is converted to a layout box whose cross extent spans the bar positions
  `origin + i·step·direction, i∈[-left, right]` and whose longitudinal extent is `length`
  (no anchorage). The bg zone becomes `background=(d, step)`.
* `layout_rebars` runs exactly as in the task `bars` stage (see Q3), on the physical
  polygons of the scene (removed/`real:false` polygons are holes, so bars are clipped around
  openings and at the field boundary).
* Every laid-out bar segment becomes a `Bar{zone_id, start, end, d, anchorage{start,end}}`;
  anchorage = the zone's explicit `anchorage` or `anchor_factor·d` on both ends, measured from
  the (possibly clipped) visible end.
* Output `Zone`s are re-derived from the laid-out tracks (one arithmetic run per input zone;
  extra runs get fresh ids), `length` = unclipped bar length without anchorage.
* Mass metrics, per group (`bg`, `additional`):
  * `without_anchorage_kg` = Σ segments visible length
  * `with_anchorage_kg` = Σ segments (visible length + anchorage start + end)
  * `without_anchorage_unclipped_kg` = Σ tracks full unclipped length (holes ignored)
  * `with_anchorage_unclipped_kg` = Σ tracks (full length + start + end)
  Background tracks: unclipped length = the field component's extent along the bar axis at
  that track (see Q2).

## 5. Bar layout notes (2026-09-12)

* An additional zone whose bars land on background guides is shifted as a whole by the clearance
  (`_shift_zones_off_background`), so its step stays uniform; only residual collisions are
  repaired bar by bar.
* Zones are the source of the bars and are never re-derived from them: the `zones` of a bars /
  task result are the input zones, normalised (gap-filling rods carry their zone's id). Overlaying
  a zone on the background grid legitimately shifts individual bars (a 150 mm step on a 300 mm
  background reads 100/200/100/200; a bar sharing a guide with a background bar moves by the
  clearance) and those shifts do not propagate back, so zones → bars → zones is stable.
* The verification band of a rod counts only the width inside the field (material polygons), so
  edge rods are not credited to concrete beyond the slab edge.

## 5a. Gap filling after the layout (`fill_gaps`, 2026-09-12)

A laid-out zone can leave a strip wider than its step (bars pushed off background guides by the
clearance rule, a zone box ending inside a high-need element, an element straddling a weak part of
the period). With `fill_gaps=true` (default, `BarsConfig`/`TaskConfig`) the bars stage, `/v2/bars`
and `/v2/verification` check every polygon with the verification model of §6 and, for each polygon
short by more than 0.5 cm²/m, insert one rod (diameter of the additional zone under the polygon)
into the widest gap of parallel rods crossing it, merged along the bars for neighbouring short
elements, clipped to the field, with `anchor_factor·d` anchorage; the pass repeats until nothing
is short. An inserted rod is a spacing shift inside the zone it was inserted into (like the
100/200 alternation of a 150 mm zone on the 300 mm background): it carries that zone's `zone_id`,
the zone list is returned unchanged, and the rod is counted in `mass_metrics.additional`; the
`repair` field of the result reports `passes`, `rods_added`, `added_kg`, `short_before`,
`short_after`.

## 5b. Alternative solver worker (2026-09-12)

`rebar_service.alt_worker` runs `rebar_service.altsolver` (the vendored A101_reinforcement
experiments: canonical lattice pool on a 100 mm raster + CP-SAT set cover) behind the v2 task
protocol: `handle_prepare` builds the engine from the resolved scene rows (band bound = load + 1,
background/ladder resolved like the production pipeline, user `stock` honoured), sizes N by the
exact useful range (`min_useful_n` = smallest covering count, `max_useful_n` = box count of the
unconstrained minimum-mass cover; N above it is `infeasable` with that reason), and `handle_solve` does selection → wire zones → production bar layout →
gap filling → the N row (`result.solver` carries pool/lattice/status/timings). It consumes the
*main* queue of its configuration, so routing is a ConfigMap choice for the API:
`rebar-test-solver-queues` (→ `rebar:test:solver:*`, KEDA ScaledJob `rebar-solver-worker-test`) or
`rebar-test-queues` (→ the production worker). No artifacts are written.

## 6. Verification

For every source polygon (stable order) with its overlay state mapped to
`active | real | empty`:

* `need_load_sm2/m` = polygon `load` for `active`, `0` for `real`, `null` for `empty`.
* `fact_load_sm2/m` = mean **smeared reinforcement density** over the polygon. The rods of all
  zones (background included, anchorage excluded, clipped to the field) form one set; each rod
  has only an axis and a diameter `d`. At every point along a rod its tributary band reaches
  half-way to the nearest parallel rod on each side, but never farther than the crack-control
  reach `r = 5·(cover_mm + d/2)` (EN 1992-1-1 §7.3.4; `cover_mm` = concrete cover to the bar
  face, request field, default 30). Density of a point = `10·π(d/2)² / (w_left + w_right)`
  of the nearest rod whose band covers it, `0` where no band reaches. The field is evaluated on
  a 20 mm raster and averaged over the raster cells inside the polygon, so a uniform mesh at
  spacing `s ≤ 2r` reads exactly `10·π(d/2)²/s` regardless of how the elements are cut.
* `need_load_kg/m3` = `need · ρ / (10 · t)`, `fact_load_kg/m3` = `fact · ρ / (10 · t)`
  (`t`, lengths in mm, `ρ = steel_density_kg_m3`). See Q6.

Rationale (2026-09-12): the first version credited the steel of rod cylinders intersected
with each polygon; with 300 mm spacing and 500 mm elements the result oscillated with the
element cut and narrow elements between two rods read zero, so refining the mesh raised the
"deficit" without any change in reinforcement.

## 7. Configuration (env / ConfigMap only)

Kept (all read from `Settings`, no request field, no cap): `REBAR_SOLVER_THREADS`,
`REBAR_SOLVER_TIMEOUT`, `REBAR_SOLVER_TIME_LIMIT` (default when the request omits it),
`REBAR_SOLVER_BACKEND`, `REBAR_FIT_TIME_LIMIT`, `REBAR_FIT_MILP_BACKEND`, `REBAR_FIT_THREADS`,
`REBAR_MAX_N` (cap for `max_useful_n` and for requested N, see Q7), `REBAR_GRID_SIZE`,
`REBAR_FILL_NOTCHES`, `REBAR_SHORT_EDGE`, `REBAR_SIMPLIFY_STEP`, `REBAR_USE_MOSAIC`,
`REBAR_MIN_INTERNAL_STEP`, `REBAR_MAX_JOBS_PER_TASK`, queue names, lease/claim timeouts,
`REBAR_SOLVER_LOG_DIR`, `REBAR_WORKER_EXIT_WHEN_IDLE`, `REBAR_HIGHS_OPTIONS` (JSON, extra HiGHS options
for the solving stage), `REBAR_CANDIDATE_LATTICE_CAP` (0 = exhaustive candidates; >0 = lattice-tightened
candidate pool of at most that many rectangles, see `A101/lattice_candidates.py`).

Removed: `solver.threads/timeout_seconds/backend/require_optimal/prepared_max_n/highs_options`
request fields, `max_concurrent_jobs`, `component_result_top_k`, `validate_results`,
`scan_mode`, `whole`, `effective_threads` clamp, `max_solver_threads`,
`max_solver_timeout_seconds`, `validate_solver_limits`, `SOLVER_HARD_MAX_N`, the literal
`250`/`100` caps, `scheduler_batch_size`, `combine_batch_size`, `frontier_top_k`,
`max_planned_n_values`, `schedule_window_factor`.

## 8. HiGHS logs

* `REBAR_SOLVER_LOG_DIR` defaults to `<repo>/logs` (`/app/logs` in the image).
* The solving stage creates `{log_dir}/{task_id}/{n}/` and passes
  `highs_options={"log_file": ".../{worker-id}.log", "output_flag": True,
  "log_to_console": False}` down to `_solve_prepared_with_highs`, which now applies explicit
  `output_flag`/`log_to_console` options after its defaults.
* Kubernetes: `PersistentVolumeClaim rebar-solver-logs` (`csi-s3` storage class, RWX),
  mounted in the main worker at `/app/logs` with `subPath: rebar-optimizer/logs`.

## 9. Workers and KEDA

* `worker_loop.run(queue_names, dispatch, exit_when_idle)`; the main worker keeps the
  `Deployment` + `ScaledObject` on `rebar:jobs:workload`.
* `bars_worker` / `verification_worker` run as KEDA `ScaledJob`s triggered by
  `rebar:bars:workload` / `rebar:verification:workload`; each Job processes until its queue
  is empty and exits.

## 10. Open questions and answers (confirmed 2026-09-12)

* **Q1 v1 API.** *Answer:* keep v1 alive beside v2. A v1 route is removed only when a change
  made for v2 breaks it; everything else stays. Old tables are untouched.
* **Q2 Background anchorage.** *Answer:* yes. bg anchorage = `anchor_factor·d_bg` (or the bg
  zone's explicit `anchorage`) on both ends, mass only; unclipped bg length = full component
  extent ignoring holes.
* **Q3 /v2/bars semantics.** *Answer:* same `layout_rebars` heuristics as the task `bars`
  stage; returned zones are re-derived from the laid-out tracks.
* **Q4 Infeasible N.** *Answer:* `state=success`, `status=infeasable`, no bars/zones/mass;
  `error` only for exceptions.
* **Q5 Overlays.** *Answer:* one server-assigned id per event; `POST` returns the id of the
  last appended event; `GET /overlays/{id}` returns the log up to and including that revision
  (selectors `0/-1/-2` apply); already-masked / already-unmasked idxs are skipped at append
  time; client `time` is stored and echoed.
* **Task view (2026-09-12).** `GET /v2/tasks/{id}` also carries `state`, `error`, `min_useful_n`, `max_useful_n`.

**Q6 Verification units.** *Answer:* formulas of §6, bars without anchorage, background
  bars included; the example numbers in the contract are illustrative only.
* **Q7 N caps.** *Answer:* `REBAR_MAX_N` (default 1000) caps `max_useful_n` and validates
  requested N; `prepare_rectangle_problem` receives `max_useful_n`;
  `config.solver.solver_time_limit` stays in the request and falls back to
  `REBAR_SOLVER_TIME_LIMIT`. All other solver knobs are env-only.
* **Q8 CSI-S3.** *Answer:* cluster `yc-kube-cxm`, namespace `rebar-optimizer`, StorageClass
  `csi-s3` (provisioner `ru.yandex.s3.csi`; RWX proven by the existing PVC `a101-thumbs`).
  No bucket or secret appears in the manifests.
* **Q9 min_bar_gap_mm / max_snap_mm.** *Decision:* `min_bar_gap_mm` → `layout_rebars(min_step=…)`
  (the guide quantum lower bound; `REBAR_MIN_INTERNAL_STEP` is the default when omitted);
  `max_snap_mm` → `fit_box_layout(max_distance=…)`.
