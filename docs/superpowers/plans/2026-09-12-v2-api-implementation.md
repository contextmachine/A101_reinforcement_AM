# /v2 implementation plan (phase A: foundations, phase B: pipeline + workers, phase C: review)

Status 2026-09-12: phases A, B and C are complete on branch `v2` (full test suite green). The adversarial review
(8 dimensions, 28 findings, 12 confirmed) is folded in: stage idempotency and error persistence, cancel lock order,
shared-edge halving in verification, same-coordinate track merging in bars, contract-tolerant mutation bodies,
worker-loop failure exit, dev NetworkPolicy and prod-private pull-secret patches for the ScaledJobs. Deviations from the plan are recorded in the per-agent reports and in the code docstrings
(notably: ScaledJob manifests live in `deploy/k8s/overlays/{dev,prod}/`, `V2Store.set_n/set_task` use an
`_UNSET` sentinel so omitted keywords keep their values, and the v2 router resolves the store through
`app.state.api_module`).

Design: `docs/superpowers/specs/2026-09-12-v2-api-design.md`. Contract: `payload-v2-am-aa.md`.
Requirements: `changes 2026-09-12.md`. All decisions in §10 of the design are final.

`rebar_service/config.py` and `deploy/k8s/base/configmap.yaml` are ALREADY rewritten and are the
source of truth for settings names (read them first). Removed settings: `default_solver_threads`,
`max_solver_threads`, `max_solver_timeout_seconds`, `max_n_value`, `schedule_window_factor`,
`effective_threads()`, `schedule_window`. Added: `max_n`, `solver_threads`, `fit_threads`,
`solver_log_dir`/`solver_log_path`, `worker_exit_when_idle`, `bars_*_queue`, `verification_*_queue`.

## Rules for every agent

* Work in the shared working tree on branch `v2`. Never `git commit`, never `git stash`,
  never run `git checkout`/`reset`. Do not touch `.DS_Store`, `.idea/`.
* Edit only the files you own (listed per agent). Use `Edit` for existing files; never
  rewrite an existing shared file with `Write`. New files may be created with `Write`.
* Python 3.12+, ruff line length 110, no new dependencies. Russian error strings are fine.
* Run `python -m compileall -q <your files>` and your own tests with
  `.venv/bin/python -m pytest -q <your test files>`. Do not "fix" other agents' files; report
  cross-cutting problems in your final report instead.
* `pipeline.py` and `postgres_store.py` define many methods twice; the LAST definition is live.
  Edit the live copy (line numbers: see the design's understanding notes below).
* Existing tests that pin behaviour you were told to change may be updated/deleted only when
  they are listed in your ownership.

## Shared contracts

### Wire models (`rebar_service/v2/models.py`, owner A6)

```python
RcVariant{d: float>0, step: float>0}
Anchorage{start: float>=0, end: float>=0}
ZoneBase{id:int, kind:"bg", arm:RcVariant, anchorage:Anchorage|None=None}
ZoneAdditional{id:int, kind:"additional", arm:RcVariant, left:int>=0=0, right:int>=0=0,
               length: float>0, anchorage:Anchorage|None=None,
               origin:(float,float), direction:(float,float) unit vector}
Zone = discriminated union on "kind"
Bar{zone_id:int, start:(x,y), end:(x,y), d:float, anchorage:Anchorage}
MassGroup{with_anchorage_kg, without_anchorage_kg, with_anchorage_unclipped_kg, without_anchorage_unclipped_kg}
MassMetrics{additional:MassGroup, bg:MassGroup}
FEPolygon{load:float, color:int|None=None, points:list[(x,y)] (>=3)}
FEPolygonOut(FEPolygon){overlay_state:"active"|"real"|"empty", source_index:int}
OverlayIn{type:"clean"|"unclean", idxs:list[int], real:bool=False, time:int|float|None=None}
OverlayOut(OverlayIn){id:int}
OverlaysPost{scene_id:str|None=None, overlays:list[OverlayIn]}
OverlayCreated{scene_id:str, overlay_id:int}
SceneCreated{scene_id:str, state:str}
SolverConfig{solver_time_limit: float|None=None}
TaskConfig{max_layers:int|None=None, axis:"x"|"y"="y", anchor_factor:float>=0=40,
           min_width_mm:float>0=1000, max_snap_mm:float>=0=600, min_bar_gap_mm:float>0|None=None,
           steel_density_kg_m3:float>0=7850, back_grid:RcVariant|None=None,
           stock:list[RcVariant]|None=None, solver:SolverConfig=SolverConfig()}
TaskCreate{scene_id:str, overlay_id:int=0, smooth:bool=False, n:list[int] (>=1 each, non-empty, deduped),
           config:TaskConfig=TaskConfig()}
TaskCreated{task_id:str}
NMutation{n:list[int]}      CancelMutation{n:list[int]|None=None}
SolutionSummary{n:int, state:str, fun:float|None=None, status:str|None=None, mass_metrics:MassMetrics|None=None}
TaskView{task_id, scene_id, smooth, overlay_id, solutions:list[SolutionSummary]}
SolutionView(SolutionSummary){task_id:str, bars:list[Bar]|None=None, zones:list[Zone]|None=None, error:str|None=None}
BarsConfig{axis:"x"|"y"="y", anchor_factor:float>=0=40, min_bar_gap_mm:float>0|None=None}
BarsRequest{scene_id, smooth:bool=False, overlay_id:int=0, config:BarsConfig=BarsConfig(), zones:list[Zone]}
BarsCreated{task_id, state}
BarsView{task_id, state, bars|None, zones|None, mass_metrics|None, error|None}
VerificationConfig(BarsConfig){steel_density_kg_m3:float>0=7850, t:float>0}
VerificationRequest{scene_id, smooth, overlay_id, config:VerificationConfig, zones:list[Zone]}
VerificationCreated{task_id, state}
VerificationRow{source_index:int, overlay_state:str,
                "need_load_sm2/m":float|None, "fact_load_sm2/m":float|None,
                "need_load_kg/m3":float|None, "fact_load_kg/m3":float|None}   # pydantic aliases
VerificationView{verification_id, state, result:list[VerificationRow]|None, error|None}
```
Per-N `state` values: `pending, preparing, solving, fitting, bars, success, error, cancelled`.
Per-N `status` wire values: `optimal`, `feasable`, `infeasable` (contract spelling).
Bar/verification task `state`: `pending, running, success, error`.

Zone validation: `ZoneAdditional.direction` must be a unit vector (|len-1| <= 1e-5); zone ids
unique within a request; exactly one `kind:"bg"` zone is allowed per request (0 allowed for
/v2/bars? No: require exactly one bg zone for bars/verification; PUT /v2/tasks has no zones).

### Overlay state vocabulary
Stored/internal: `active | background_only | removed` (unchanged). Wire (v2 only):
`active | real | empty`. Translate ONLY at the v2 serialisation boundary
(`rebar_service/v2/scenes.py`: `to_wire_overlay_state()` / `from_wire_overlay_state()`).

### Overlay events store contract (owner A5, consumer A6)
`PostgresStore.append_scene_overlay_events(scene_id, events)`:
* each event: `{type, idxs, real, id?: int, time?: number}`. When `id` is absent the store
  allocates `COALESCE(MAX(overlay_id),0)+1` per event inside the existing `FOR UPDATE`
  transaction (ids strictly increasing in append order). Client ids (v1) keep working.
* Before insert, `idxs` are filtered against the state materialised through the current head
  (`resolve_overlay(raw_polygons, existing_events, head_id)`): `clean` skips idxs whose state is
  already `background_only`/`removed`; `unclean` skips idxs whose state is already `active`.
  An event whose filtered `idxs` is empty is still stored (so the client always gets an id).
* `time` is persisted in the new nullable `scene_overlay_events.client_time DOUBLE PRECISION`
  column (migration 0005) and returned as `time` (None when absent).
* Returns the full event log (existing behaviour) as rows
  `{seq, id, type, idxs, real, created_at, time}`.
`PostgresStore.scene_overlay_events(scene_id)` returns the same row shape (adds `time`).

### v2 store contract (`rebar_service/v2/store.py`, owner A5)
```python
class V2Store:
    def __init__(self, database: Database): ...
    # tasks
    def create_task(self, task_id, *, scene_id, overlay_id, smooth, config: dict, ns: list[int]) -> None
    def get_task(self, task_id) -> dict|None   # {task_id, scene_id, overlay_id, smooth, config, state, error,
                                              #  max_useful_n, min_useful_n, cancelled, created_at, updated_at}
    def set_task(self, task_id, **fields) -> None   # state, error, max_useful_n, min_useful_n, cancelled, prepare_info(jsonb)
    def list_ns(self, task_id) -> list[dict]    # ordered by n: {n, state, status, fun, mass_metrics, error, updated_at} (no result)
    def get_n(self, task_id, n) -> dict|None    # same + result (dict|None)
    def set_n(self, task_id, n, *, state=None, status=None, fun=None, mass_metrics=None, result=None, error=None) -> None
    def add_ns(self, task_id, ns) -> list[int]  # inserts missing rows as 'pending', returns the NEW ns
    def schedule_lock(self, task_id): contextmanager yielding a Connection with `SELECT ... FOR UPDATE` on v2_tasks
    def ns_in_states(self, task_id, states, conn=None) -> list[int]
    def cancel(self, task_id, ns: list[int]|None) -> list[int]     # None => all; sets state 'cancelled', task.cancelled when all
    def is_cancelled(self, task_id, n) -> bool                     # task.cancelled or row.state == 'cancelled'
    # artifacts (codec.encode_object / decode_object, sha256-verified)
    def save_artifact(self, task_id, key, value) -> None
    def load_artifact(self, task_id, key) -> Any|None
    def delete_artifact(self, task_id, key) -> None
    # bar tasks / verification tasks
    def create_bar_task(self, task_id, *, scene_id, overlay_id, smooth, config, zones) -> None
    def get_bar_task(self, task_id) -> dict|None   # {task_id, scene_id, overlay_id, smooth, config, zones, state, result, error}
    def set_bar_task(self, task_id, *, state, result=None, error=None) -> None
    def create_verification_task / get_verification_task / set_verification_task   # same shapes
```
Tables (migration `0005_v2_tasks.py`, additive, schema via search_path like 0003):
```
v2_tasks(id varchar(32) pk, scene_id varchar(32) fk scenes ON DELETE RESTRICT, overlay_id bigint not null default 0,
         smooth bool not null default false, config jsonb not null, state varchar(32) not null default 'created',
         error text null, max_useful_n int null, min_useful_n int null, prepare_info jsonb null,
         cancelled bool not null default false, created_at/updated_at timestamptz default now())
v2_task_ns(task_id fk v2_tasks cascade, n int, state varchar(32) not null default 'pending', status varchar(16) null,
           fun double precision null, mass_metrics jsonb null, result jsonb null, error text null,
           created_at/updated_at, PK(task_id, n), CHECK n > 0)
v2_artifacts(task_id fk cascade, key text, codec varchar(32), payload bytea, sha256 varchar(64), created_at/updated_at, PK(task_id, key))
v2_bar_tasks(id pk, scene_id fk, overlay_id, smooth, config jsonb, zones jsonb, state varchar(16) default 'pending',
             result jsonb null, error text null, created_at/updated_at)
v2_verification_tasks(same columns)
ALTER scene_overlay_events ADD COLUMN client_time double precision NULL
```

### Queue contract (owner A4)
`RedisQueue(settings, names: QueueNames | None = None)`; `QueueNames(ready, processing, workload)`;
default = the main queue. `Store` (facade) gets `self.bars_queue`, `self.verification_queue`
(wired by the coordinator after phase A). Job dicts for the isolated queues:
`{"kind": "bars"|"verification", "task_id": <id>, "payload": {}, "generation": 0,
  "dedupe_key": "bars:<id>", "job_id": uuid4 hex, "created_at": time.time()}`.
Main-queue v2 jobs use `PipelineJob(kind="v2_prepare"|"v2_solve"|"v2_fit"|"v2_bars", task_id, payload={"n": n}?,
dedupe_key="v2:<kind>:<task_id>:<n or '-'>")`.

`rebar_service/worker_loop.py`:
```python
def run_queue_worker(*, settings, queue: RedisQueue, handle: Callable[[dict, str], None], worker_name: str,
                     exit_when_idle: bool, stopping: threading.Event | None = None) -> None
```
SIGTERM/SIGINT handling, `requeue_stale_jobs` reaper cadence as in `worker.py`, `LeaseHeartbeat`
(moved here, re-exported from `worker.py`), `claim_job` → `handle(job_dict, worker_id)` → `ack_job('done'|'failed')`;
exceptions are logged with traceback and the job is acked `failed` (the handler is responsible for
persisting the error state). With `exit_when_idle=True` the loop returns when a claim times out.

`rebar_service/worker.py` (main): keep its loop; add a branch right after the `materialize_scene`
branch: `if str(job_data.get("kind","")).startswith("v2_"): v2_workflow.dispatch(job_data, worker_id)` with
heartbeat, ack done/failed, no meta/generation/slot checks. `v2_workflow` is
`rebar_service.v2.pipeline.V2Pipeline(store, settings)` (module created in phase B; import lazily
inside the branch so the worker still imports before phase B lands; guard with try/except ImportError
that acks `failed`). `task_limit` line must read `settings.max_jobs_per_task` only.

### HiGHS log contract (owner A2, consumed in phase B)
`rebar_service/solver_logs.py`:
```python
def solver_log_file(settings, task_id: str, n: int, worker_id: str) -> str   # creates dirs, returns path
def highs_log_options(log_file: str) -> dict   # {"log_file": ..., "output_flag": True, "log_to_console": False}
```
`A101.reinforcement_components.solve_component_frontier(..., highs_options=None)` forwards to
`solve_rectangle_job(highs_options=...)`. `_solve_prepared_with_highs` applies `log_file`,
`output_flag`, `log_to_console` from `highs_options` AFTER its own defaults (explicit options win).
`fit_component_frontier(..., highs_options=None)` → `fit_box_layout(highs_options=...)` applies the
same three options to the fit `Highs` instance.

### prepare_component_problem contract (owner A2)
`prepare_component_problem(..., max_n=None, max_n_resolver=None, ...)`: when `max_n_resolver` is
given it is called as `max_n_resolver(work_matrix, context)` right after `filter_candidates_by_matrix_barriers`
and before `prepare_rectangle_problem`; `context = {"recipes": recipes, "work_x_edges":..., "work_y_edges":...,
"holds": holds, "component_id": ...}`. Its return value (int|None) replaces `max_n`. The returned
problem dict gains `"max_n": <effective max_n>`.

### bars module contract (`rebar_service/v2/bars.py`, owner A7)
Pure functions on plain dicts (wire shapes above), no pydantic imports:
```python
def physical_polygons(resolved_rows) -> list[shapely.Polygon]     # overlay_state in {active, background_only}
def resolve_anchorage(zone: dict, anchor_factor: float) -> tuple[float, float]
def zone_to_box(zone: dict, *, axis: str) -> dict   # {"id", "bounds": (x0,y0,x1,y1) fitted/no anchorage, "diameter", "step"}
def layout_zones(polygons: Sequence[shapely.Polygon], zones: Sequence[dict], *, axis: str, anchor_factor: float,
                 steel_density_kg_m3: float, min_step: float) -> dict
    # returns {"is_feasible": bool, "status": str, "bars": list[Bar dict], "zones": list[Zone dict],
    #          "mass_metrics": MassMetrics dict, "layout": <raw layout_rebars output>, "warnings": [...]}
def fitted_boxes_to_zones(boxes: Sequence[dict], *, axis: str, anchor_factor: float, bg: RcVariant dict,
                          bg_anchorage: dict|None=None) -> list[Zone dict]   # phase B input: add_box_anchorage rows
def bars_from_layout(layout, zones, *, axis, anchor_factor) -> list[Bar dict]   # helper used by layout_zones
```
Semantics: design §5 and Q2/Q3/Q9. Exactly one bg zone in `zones`. Input zone ids are preserved;
extra arithmetic runs get ids `max(existing)+1, ...`. Bars: `start/end` are the visible clipped
segment endpoints in world mm (axis `y`: x0==x1, y0<y1; axis `x`: y0==y1, x0<x1), `d` = zone d,
`anchorage` = `resolve_anchorage`. Masses use `π(d/1000)²/4 · ρ · L/1000` (kg).

### verification module contract (`rebar_service/v2/verification.py`, owner A8)
```python
def reinforcement_rows(resolved_rows: Sequence[dict], bars: Sequence[dict], *, steel_density_kg_m3: float, t_mm: float) -> list[dict]
```
One row per source polygon in order: `{"source_index", "overlay_state" (wire vocabulary),
"need_load_sm2/m", "fact_load_sm2/m", "need_load_kg/m3", "fact_load_kg/m3"}` with the formulas of
design §6 (`active` → need=load; `background_only`→ need 0; `removed` → all four None; fact always
computed from the bar axes ∩ polygon, cylinder volume / polygon area; kg/m³ = cm²/m·ρ/(10·t)).
Use an STRtree over bar LineStrings; polygons repaired with `buffer(0)`.

## Phase A agents and file ownership

### A1 `knobs-cleanup` (model: opus)
Owns: `rebar_service/models.py`, `rebar_service/api.py` (only `_build_task`, the validation calls
and the `max_concurrent_jobs`/`prepared_max_n`/250 caps — do NOT add routes), `rebar_service/planner.py`,
`rebar_service/pipeline.py` (knob reads only), `rebar_service/postgres_store.py` ONLY the two
`settings.max_n_value` reads (→ `settings.max_n`), `rebar_service/api_docs_ru.py` ONLY the MODEL_FIELDS
entries of removed fields, `run3.py`
(if it references removed settings), tests: `tests/test_planner.py`, `tests/test_task_metadata_normalization.py`,
`tests/test_deferred_dxf_variants.py`, `tests/test_postgres_scene_roundtrip.py`, `tests/test_max_n_milp.py`,
`tests/test_task_start_api.py`, `tests/test_manual_component_api.py`, `tests/test_models.py`,
`tests/test_scene_audit_regressions.py` (only assertions about removed knobs).
Work: requirement 6. SolverOptions keeps ONLY `solver_time_limit` (extra ignored); TaskParameters
drops `max_concurrent_jobs` and `quantizer`; `AnalysisTaskStart` N cap uses `settings.max_n`
(pass settings into the validator via api, not a literal); `_build_task` drops
`validate_solver_limits`, the `max_concurrent_jobs` and `prepared_max_n` checks and `schedule_window_factor`;
meta `max_concurrent_jobs` = `settings.max_jobs_per_task` (column is NOT NULL); `_solver_options` returns
threads=`settings.solver_threads`, timeout=`settings.solver_timeout`, time_limit = task `solver.solver_time_limit`
else `settings.solver_time_limit`, backend=`settings.solver_backend`, require_optimal=`settings.require_optimal`;
every `SOLVER_HARD_MAX_N`/`max_n_value` use → `settings.max_n`; `prepared_max_n` → `settings.max_n`;
`fit_threads` → `settings.fit_threads`; delete `planner.validate_solver_limits`. Keep v1 routes working.
Report every v1 test you changed and why.

### A2 `a101-solver` (model: default)
Owns: `A101/select_min_density_rectangles_recipes.py` (only `_solve_prepared_with_highs` option handling),
`A101/rectangle_solver_job.py` (nothing unless needed to forward `highs_options` — already forwarded),
`A101/reinforcement_components.py` (`solve_component_frontier`, `fit_component_frontier`,
`prepare_component_problem` + `max_n_resolver`), `A101/fit_box_layout.py` (`highs_options` param on
the HiGHS path), `A101/axis_orientation.py` (add optional asymmetric anchorage support to
`add_box_anchorage`: `hold_start`/`hold_end` keys honoured when present on a box; default symmetric),
new `rebar_service/solver_logs.py`, new tests `tests/test_solver_logs.py`, `tests/test_prepare_max_n_resolver.py`.
Also owns every other A101 change of requirement 6: `A101/max_n_milp.py` (`hard_cap` default → None =
no cap; `tests/test_max_n_milp.py` keeps passing when it passes `hard_cap=250` explicitly), and the
thread validations (`rectangle_solver_job.py:1032-1034,1375-1377`, `rectangle_solver_stream.py:596-598`,
`select_min_density_rectangles_recipes.py:4581-4584`, `reinforcement_components.py:1774`,
`fit_box_layout.py:416`): allow `threads == 0` (= HiGHS auto), keep rejecting negatives.
Verify with a real tiny HiGHS run that the log file is created and non-empty.

### A4 `infra-workers` (model: opus)
Owns: `rebar_service/redis_queue.py`, new `rebar_service/worker_loop.py`, `rebar_service/worker.py`,
new `rebar_service/bars_worker.py`, new `rebar_service/verification_worker.py` (each: build Store,
pick its queue, `run_queue_worker(handle=...)` where `handle` lazily imports
`rebar_service.v2.workers.handle_bars_job` / `handle_verification_job` (created in phase B) and
acks failed if the import fails), `Dockerfile` (`mkdir -p /app/logs` before chown), deploy manifests:
new `deploy/k8s/base/logs-pvc.yaml` (PVC `rebar-solver-logs`, `storageClassName: csi-s3`,
`ReadWriteMany`, 50Gi), `worker-deployment.yaml` (mount PVC at `/app/logs`, `subPath: rebar-optimizer/logs`),
new `deploy/k8s/base/bars-scaledjob.yaml` and `verification-scaledjob.yaml` (KEDA `ScaledJob`,
same image/env/secret wiring/security context/resources as the worker deployment,
`REBAR_WORKER_EXIT_WHEN_IDLE="true"`, `REBAR_DB_POOL_SIZE "1"`/`REBAR_DB_MAX_OVERFLOW "0"`,
`command: python -m rebar_service.bars_worker`, trigger `type: redis` on `rebar:bars:workload` /
`rebar:verification:workload` with `authenticationRef rebar-redis-auth`, `activationListLength "0"`,
`listLength "1"`, `pollingInterval 5`, `maxReplicaCount 8`, `ttlSecondsAfterFinished 300`,
`activeDeadlineSeconds 21600`, `backoffLimit 0`, `restartPolicy Never`, `imagePullSecrets ghcr-secret`).
Put the ScaledJobs + keda auth reference in `base/kustomization.yaml` only if the base already holds
KEDA objects — it does not (KEDA objects live in overlays), so add them to BOTH `overlays/dev` and
`overlays/prod` kustomizations (copy the files into `deploy/k8s/base` and reference from overlays, or
place them under overlays like `worker-scaledobject.yaml`; follow the existing pattern). `scripts/deploy-k8s.sh`
(print the new scaledjobs), `README.md` (worker section), tests: `tests/test_manifests.py`,
`tests/test_single_pipeline_architecture.py` (allow the two new ScaledJob files; keep its other
assertions), `tests/test_store.py` (queue names), new `tests/test_worker_loop.py` (fakeredis-free:
use a stub queue object). Do not touch `store.py` (coordinator wires queues afterwards).

### A5 `store-migration` (model: opus)
Owns: new `migrations/versions/0005_v2_tasks.py`, new `rebar_service/v2/__init__.py` (empty),
new `rebar_service/v2/store.py`, `rebar_service/postgres_store.py` (ONLY `append_scene_overlay_events`
and `scene_overlay_events`), tests: `tests/test_postgres_schema.py` (extend for 0005),
`tests/test_overlay_store_contract.py`, `tests/test_scene_store_contract.py`, new `tests/test_v2_store.py`
(capture-database doubles like `tests/test_postgres_store_unit.py`), `tests/test_scene_migration_contract.py`
if it enumerates revisions, `scripts/verify.sh` (add `grep -q 'CREATE TABLE v2_tasks'`).
Also write `tests/support/memory_v2_store.py`: an in-memory `MemoryV2Store` implementing the V2Store
contract (dict-backed, `schedule_lock` yields None) for phase B tests.

### A6 `v2-models-scenes` (model: opus)
Owns: new `rebar_service/v2/models.py`, new `rebar_service/v2/scenes.py` (upload → scene, resolved
polygons → wire rows, overlay POST/GET helpers, all pure/testable), new `rebar_service/v2/api.py`
(`router = APIRouter(prefix="/v2")` with the scene endpoints implemented: `POST /v2/dxf_upload`,
`POST /v2/json_upload` (JSON body `list[FEPolygon]` → `source_polygons_from_json_bytes`-equivalent, keep
`color`), `POST /v2/tables_upload`, `GET /v2/scenes/{scene_id}/polygons?smooth&overlay_id`,
`POST /v2/scenes/{scene_id}/overlays`, `GET /v2/scenes/{scene_id}/overlays/{overlay_id}`; task/bars/verification
endpoints are added in phase B — leave clearly marked TODO sections), `rebar_service/api.py` ONLY the
two lines `from .v2.api import router as v2_router` / `app.include_router(v2_router)` placed BEFORE
`install_russian_docs(app)`, `rebar_service/api_docs_ru.py` (append a v2 TAGS entry at the END of
TAGS, OPERATIONS entries for every v2 route function name, PARAMETERS entries for `overlay_id`,
`scene_id`, `n`, `verification_task_id`, `task_id` if missing; BODY/MODEL field descriptions for the
v2 models — Russian), tests: `tests/test_openapi_ru.py` (must stay green with v2 routes), new
`tests/test_v2_models.py`, new `tests/test_v2_scene_api.py` (TestClient with monkeypatched
`api.store` like `tests/test_scene_api.py`). The api module must import without Redis/Postgres.
Depends on the overlay store contract above (use a fake store in tests).

### A7 `v2-bars` (model: default)
Owns: new `rebar_service/v2/bars.py`, new `tests/test_v2_bars.py`. Read `A101/rebar_field_layout.py`
(`_zone` at 184-256 for accepted box keys; output shapes at 1219-1309), `A101/axis_orientation.py`
(`restore_bar_layout` for axis x key renames), `rebar_service/layout_metrics.py` (v1 mass semantics)
and `rebar_service/compact_zones.py` (direction/origin conventions). Tests must cover: axis x and y;
a hole splitting a track into two segments (unclipped counts one rod); bars clipped at the field
boundary keep full anchorage in `with_anchorage_kg`; round trip `layout_zones` → zones → `layout_zones`
is stable for zones produced by the layout; bg mass group; explicit zone anchorage overrides
anchor_factor; `fitted_boxes_to_zones` on `add_box_anchorage` output (use `fitted_bounds`).

### A8 `v2-verification` (model: default)
Owns: new `rebar_service/v2/verification.py`, new `tests/test_v2_verification.py`. Tests: a single
bar crossing a square polygon; bars partially outside; `real`/`empty` rows; unit sanity
(uniform grid d=18 step=300 over a big polygon → fact ≈ 10·π·81/300 = 8.48 cm²/m); kg/m³ relation.

## Phase B (after A; coordinator + agents)
* `rebar_service/store.py`: `self.v2 = V2Store(self.database)`, `self.bars_queue`, `self.verification_queue`.
* `rebar_service/v2/pipeline.py` (V2Pipeline: prepare/solve/fit/bars, scheduling under lock,
  cancellation, error handling, HiGHS log path), `rebar_service/v2/workers.py` (bars/verification handlers),
  task/bars/verification endpoints in `rebar_service/v2/api.py`, docs entries, e2e tests with
  `MemoryV2Store` + `MemoryStore`-style scene rows (real SciPy/HiGHS solve like
  `tests/test_scene_pipeline_end_to_end.py`).
* README: v2 section; `scripts/verify.sh` kustomize check must pass for dev/prod/prod-private.

## Phase C
Adversarial review workflow over the diff, full test suite, `kubectl kustomize` for all overlays,
`alembic upgrade head --sql` sanity.
