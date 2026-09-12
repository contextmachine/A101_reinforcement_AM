from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .config import get_settings
from .dxf_export import DxfExportError, build_solution_dxf
from .jsonutil import loads, to_jsonable
from .models import (
    CancelMutation,
    ComponentNRequest,
    NMutation,
    OverlayEventMutation,
    SceneCreated,
    SceneInfo,
    AnalysisTaskStart,
    CompactVerificationRequest,
    StoredSolutionVerificationRequest,
    VerificationResponse,
    TaskCreate,
    TaskCreated,
    TaskParameters,
    WsCommand,
)
from .pipeline import (
    AnalysisNotPreparedError,
    PipelineWorkflow,
    analysis_variant,
    component_storage_id,
    normalize_input_payload,
    public_value,
    to_compat_result,
)
from .planner import normalize_n_request, validate_n_request_limits
from .source_polygons import (
    SourcePolygonsError,
    pack_xlsx_tables_bundle,
    source_polygons_from_input,
)
from .store import Store


settings = get_settings()
store = Store(settings)
workflow = PipelineWorkflow(store, settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await run_in_threadpool(store.ping)
    yield


app = FastAPI(title="Rebar Optimizer API", version="2.0.0", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)
# The /v2 router resolves the store/settings through the app that mounts it.
app.state.api_module = sys.modules[__name__]
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=settings.cors_origin_list != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _build_task(
    parameters: TaskParameters,
    input_obj: dict,
    *,
    start_pipeline: bool = True,
    manual_mode: bool = False,
    smooth: bool = False,
    scene_id: str | None = None,
    overlay_id: int = 0,
    component_selection: list[int] | None = None,
) -> TaskCreated:
    validate_n_request_limits(
        parameters.n,
        max_values=settings.max_planned_n_values,
        max_n=settings.max_n,
    )

    mode, order, n_source = normalize_n_request(parameters.n)
    params = parameters.model_dump(mode="python")
    for control_key in (
        "n",
        "scan_mode",
        "whole",
        "component_result_top_k",
        "validate_results",
    ):
        params.pop(control_key, None)

    task_id = uuid.uuid4().hex
    now = time.time()
    # ``tasks.max_concurrent_jobs`` is NOT NULL; the ConfigMap is its only source.
    limit = int(settings.max_jobs_per_task)
    initial_state = "queued_preparation" if start_pipeline else "uploaded"

    # Upload routes materialize source polygons in the API process.  Tasks then
    # reference the immutable ready scene, while workers only handle the heavier
    # analysis pipeline (prepare_field / solve / result assembly).
    selected_scene_id = str(scene_id or uuid.uuid4().hex)
    task_input_obj = input_obj
    if scene_id is None:
        create_scene = getattr(store, "create_scene", None)
        if callable(create_scene):
            create_scene(
                selected_scene_id,
                {"state": "ready", "created_at": now},
                input_obj,
            )
        task_input_obj = {"kind": "scene_ref", "scene_id": selected_scene_id}

    selected_variant = analysis_variant(smooth)
    selected_components = list(component_selection or ([-2] if bool(parameters.whole) else [-3]))
    meta = {
        "task_id": task_id,
        "state": initial_state,
        "created_at": now,
        "updated_at": now,
        "cancelled": False,
        "paused": False,
        "parameters": params,
        "requested_n": list(map(int, order)),
        "n_mode": mode,
        "n_source": n_source,
        "scan_mode": parameters.scan_mode,
        "whole": bool(parameters.whole),
        "component_result_top_k": int(settings.frontier_top_k),
        "validate_results": bool(parameters.validate_results),
        "max_concurrent_jobs": limit,
        "manual_mode": bool(manual_mode),
        "initial_variant": selected_variant,
        "initial_smooth": bool(smooth),
        "scene_id": selected_scene_id,
        "analysis_variant": selected_variant,
        "analysis_overlay_id": int(overlay_id),
        "component_selection": selected_components,
    }
    plan = {
        "mode": mode,
        "order": list(map(int, order)),
        "cursor": 0,
        "paused": False,
        "exhausted": False,
        "window": max(1, limit),
    }
    store.create_task(task_id, meta, plan, task_input_obj)
    store.publish_event(
        task_id,
        "task_created",
        {
            "state": meta["state"],
            "n_mode": mode,
            "planned": len(order),
            "manual_mode": bool(manual_mode),
            "variant": selected_variant,
            "smooth": bool(smooth),
            "scene_id": selected_scene_id,
            "overlay_id": int(overlay_id),
        },
    )
    if start_pipeline:
        workflow.prepare_task_components(task_id, auto_solve=True, smooth=smooth, overlay_id=int(overlay_id))
    return TaskCreated(
        task_id=task_id,
        state=meta["state"],
        websocket_url=f"/v1/tasks/{task_id}/ws",
        status_url=f"/v1/tasks/{task_id}",
        scene_id=selected_scene_id,
        smooth=bool(smooth),
        overlay_id=int(overlay_id),
    )


def _create_scene(input_obj: dict) -> SceneCreated:
    """Parse and persist a reusable ready scene inside the API process."""
    scene_id = uuid.uuid4().hex
    store.create_scene(scene_id, {"state": "ready", "created_at": time.time()}, input_obj)
    return SceneCreated(scene_id=scene_id, state="ready")


async def _create_scene_response(input_obj: dict) -> SceneCreated:
    """Map user-source parsing failures to a synchronous client error."""
    try:
        return await run_in_threadpool(lambda: _create_scene(input_obj))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _read_upload_bytes(file: UploadFile, *, label: str = "Input file") -> bytes:
    content = await file.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail=f"{label} is too large")
    return content


async def _read_xlsx_file(file: UploadFile, *, label: str) -> bytes:
    """Read and size-check an XLSX source before API-side polygon preparation."""
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(status_code=415, detail=f"{label} must be a .xlsx file")
    return await _read_upload_bytes(file, label=label)


async def _read_upload_input(file: UploadFile, *, dxf_only: bool = False) -> dict:
    content = await _read_upload_bytes(file)
    filename = file.filename or "input.dxf"
    suffix = filename.lower()
    if suffix.endswith(".dxf"):
        return {"kind": "dxf", "filename": filename, "content": content}
    if dxf_only:
        raise HTTPException(status_code=415, detail="Supported file: .dxf")
    if suffix.endswith(".json"):
        payload = loads(content, None)
        try:
            return normalize_input_payload(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise HTTPException(status_code=415, detail="Supported files: .dxf or .json")


def _parameters_from_upload_config(config: str | None, *, start: bool) -> TaskParameters:
    if config is None or not config.strip():
        if start:
            raise ValueError("config is required when start=true")
        payload: dict = {"n": [1]}
    else:
        try:
            raw = json.loads(config)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON config: {exc.msg}") from exc
        if not isinstance(raw, dict):
            raise ValueError("config должен быть JSON-объектом")
        payload = dict(raw)
        if not start:
            payload.setdefault("n", [1])
    return TaskParameters.model_validate(payload)



def _task_context(
    task_id: str,
    meta: dict,
    *,
    smooth: bool | None = None,
    overlay: int | None = None,
) -> tuple[str, bool, int]:
    """Resolve result context while preserving migrated legacy query behavior.

    Revision-0003 migrated tasks reuse their historical task id as scene id.
    Those rows continue to honor the old query shape.  Newly-created tasks use
    a distinct scene id and their immutable stored context is authoritative.
    """
    scene_id = str(meta.get("scene_id") or task_id)
    immutable = scene_id != str(task_id)
    if immutable:
        variant = str(meta.get("analysis_variant") or meta.get("initial_variant") or "raw")
        selected_overlay = int(meta.get("analysis_overlay_id", 0) or 0)
        return variant, variant == "smooth", selected_overlay

    use_smooth = bool(smooth) if smooth is not None else False
    variant = analysis_variant(use_smooth)
    selector = 0 if overlay is None else int(overlay)
    selected_overlay = store.resolve_scene_overlay_id(scene_id, selector)
    return variant, use_smooth, int(selected_overlay)


async def _read_task_context(
    task_id: str,
    *,
    smooth: bool | None = None,
    overlay: int | None = None,
) -> tuple[dict, str, bool, int]:
    meta = await run_in_threadpool(store.get_meta, task_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        variant, selected_smooth, selected_overlay = await run_in_threadpool(
            lambda: _task_context(task_id, meta, smooth=smooth, overlay=overlay)
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return meta, variant, selected_smooth, selected_overlay


def _apply_upload_overrides(
    parameters: TaskParameters,
    *,
    scan_mode: str | None,
    whole: bool | None,
    validate_results: bool | None,
) -> TaskParameters:
    updates = {}
    if scan_mode is not None:
        updates["scan_mode"] = scan_mode
    if whole is not None:
        updates["whole"] = whole
    if validate_results is not None:
        updates["validate_results"] = validate_results
    return TaskParameters.model_validate({**parameters.model_dump(mode="python"), **updates})


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready():
    try:
        store.ping()
        return {"status": "ready"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/v1/scenes/dxf_upload", response_model=SceneCreated, deprecated=True)
async def create_scene_dxf_upload(file: UploadFile = File(...)):
    """Parse one DXF in the API and create a ready reusable scene without analysis."""
    input_obj = await _read_upload_input(file, dxf_only=True)
    return await _create_scene_response(input_obj)


@app.post("/v1/scenes/json_upload", response_model=SceneCreated, deprecated=True)
async def create_scene_json_upload(file: UploadFile = File(...)):
    """Parse source-polygons JSON in the API and create a ready reusable scene."""
    if not (file.filename or "").lower().endswith(".json"):
        raise HTTPException(status_code=415, detail="file must be a .json file")
    content = await _read_upload_bytes(file)
    input_obj = {"kind": "json", "filename": file.filename or "polygons.json", "content": content}
    return await _create_scene_response(input_obj)


@app.post("/v1/scenes/tables_upload", response_model=SceneCreated, deprecated=True)
async def create_scene_tables_upload(
    nodes_file: UploadFile = File(...),
    elements_file: UploadFile = File(...),
    loads_file: UploadFile = File(...),
    load_column: Annotated[int, Form(ge=1, le=4)] = 1,
):
    """Parse three XLSX source tables in the API and create a ready reusable scene."""
    nodes_content, elements_content, loads_content = await asyncio.gather(
        _read_xlsx_file(nodes_file, label="nodes_file"),
        _read_xlsx_file(elements_file, label="elements_file"),
        _read_xlsx_file(loads_file, label="loads_file"),
    )
    bundle = await run_in_threadpool(
        pack_xlsx_tables_bundle, nodes_content, elements_content, loads_content
    )
    input_obj = {
        "kind": "xlsx_tables",
        "filename": "tables.zip",
        "content": bundle,
        "load_column": int(load_column),
        "nodes_filename": nodes_file.filename or "nodes.xlsx",
        "elements_filename": elements_file.filename or "elements.xlsx",
        "loads_filename": loads_file.filename or "loads.xlsx",
    }
    return await _create_scene_response(input_obj)


@app.post("/v1/scenes/pkl_upload", response_model=SceneCreated)
async def create_scene_pkl_upload(file: UploadFile = File(...)):
    """Parse a restricted pickle source in the API and create a ready reusable scene."""
    suffix = (file.filename or "").lower()
    if not suffix.endswith((".pickle", ".pkl")):
        raise HTTPException(status_code=415, detail="file must be a .pickle or .pkl file")
    content = await _read_upload_bytes(file)
    input_obj = {"kind": "pickle", "filename": file.filename or "polygons.pickle", "content": content}
    return await _create_scene_response(input_obj)


@app.get("/v1/scenes/{scene_id}", response_model=SceneInfo)
async def get_scene(scene_id: str):
    row = await run_in_threadpool(store.get_scene, scene_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Scene not found")
    if row.get("state") == "preparing" and row.get("metadata", {}).get("needs_component_backfill"):
        await run_in_threadpool(workflow.enqueue_scene_materialization, scene_id)
    return SceneInfo.model_validate(row)


@app.get("/v1/scenes/{scene_id}/polygons", deprecated=True)
async def scene_polygons(
    scene_id: str,
    smooth: bool = Query(False),
    overlay: int = Query(0),
):
    scene = await run_in_threadpool(store.get_scene, scene_id)
    if scene is None:
        raise HTTPException(status_code=404, detail="Scene not found")
    if str(scene.get("state")) != "ready":
        raise HTTPException(status_code=409, detail="Scene is not ready")
    try:
        resolved_overlay = await run_in_threadpool(
            lambda: store.resolve_scene_overlay_id(scene_id, overlay)
        )
        polygons = await run_in_threadpool(
            lambda: store.resolved_scene_polygons(
                scene_id, variant=analysis_variant(smooth), overlay_id=resolved_overlay
            )
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(
        to_jsonable(
            {
                "scene_id": scene_id,
                "variant": analysis_variant(smooth),
                "smooth": bool(smooth),
                "overlay_id": int(resolved_overlay),
                "polygons": polygons,
            }
        )
    )


@app.get("/v1/scenes/{scene_id}/overlays", deprecated=True)
async def list_scene_overlays(scene_id: str):
    if await run_in_threadpool(store.get_scene, scene_id) is None:
        raise HTTPException(status_code=404, detail="Scene not found")
    rows = await run_in_threadpool(store.scene_overlay_events, scene_id)
    return JSONResponse(to_jsonable({"scene_id": scene_id, "overlays": rows}))


@app.post("/v1/scenes/{scene_id}/overlays", deprecated=True)
async def append_scene_overlays(scene_id: str, mutations: list[OverlayEventMutation]):
    if await run_in_threadpool(store.get_scene, scene_id) is None:
        raise HTTPException(status_code=404, detail="Scene not found")
    try:
        rows = await run_in_threadpool(
            lambda: store.append_scene_overlay_events(
                scene_id, [row.model_dump(mode="python") for row in mutations]
            )
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(to_jsonable({"scene_id": scene_id, "overlays": rows}))


@app.put("/v1/tasks", response_model=TaskCreated, deprecated=True)
async def start_analysis(request: AnalysisTaskStart):
    """Start an immutable analysis against a prepared scene snapshot."""
    scene = await run_in_threadpool(store.get_scene, request.scene_id)
    if scene is None:
        raise HTTPException(status_code=404, detail="Scene not found")
    if str(scene.get("state")) != "ready":
        raise HTTPException(status_code=409, detail="Scene is not ready")

    try:
        resolved_overlay = await run_in_threadpool(
            lambda: store.resolve_scene_overlay_id(request.scene_id, request.overlay_id)
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    selection = [int(x) for x in request.components]

    include_whole = selection in ([-1], [-2])
    parameter_payload = request.model_dump(
        mode="python", exclude={"scene_id", "overlay_id", "smooth", "components", "config"}
    )
    parameter_payload["whole"] = include_whole
    try:
        parameters = TaskParameters.model_validate(parameter_payload)
        input_obj = {"kind": "scene_ref", "scene_id": request.scene_id}
        return await run_in_threadpool(
            lambda: _build_task(
                parameters,
                input_obj,
                start_pipeline=True,
                manual_mode=False,
                smooth=bool(request.smooth),
                scene_id=request.scene_id,
                overlay_id=int(resolved_overlay),
                component_selection=selection,
            )
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/v1/tasks", response_model=TaskCreated, deprecated=True)
async def create_task(request: TaskCreate, smooth: bool = Query(False)):
    try:
        parameters = TaskParameters.model_validate(request.model_dump(exclude={"input"}))
        return await run_in_threadpool(
            lambda: _build_task(parameters, request.input.model_dump(mode="python"), smooth=bool(smooth))
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _upload_parameters(
    config: str | None, *, start: bool, scan_mode: str | None, whole: bool | None,
    validate_results: bool | None,
) -> TaskParameters:
    parameters = _parameters_from_upload_config(config, start=bool(start))
    return _apply_upload_overrides(
        parameters, scan_mode=scan_mode, whole=whole, validate_results=validate_results,
    )


async def _finish_source_upload(
    *, config: str | None, input_obj: dict, start: bool, smooth: bool, scan_mode: str | None,
    whole: bool | None, validate_results: bool | None,
) -> TaskCreated:
    try:
        parameters = _upload_parameters(
            config, start=start, scan_mode=scan_mode, whole=whole, validate_results=validate_results,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        return await run_in_threadpool(
            lambda: _build_task(
                parameters, input_obj, start_pipeline=bool(start), manual_mode=not bool(start), smooth=bool(smooth)
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/v1/tasks/upload", response_model=TaskCreated, deprecated=True)
async def create_task_upload(
    config: Annotated[str | None, Form()] = None,
    file: UploadFile = File(...),
    start: bool = Query(True),
    smooth: bool = Query(False),
    scan_mode: str | None = Query(None),
    whole: bool | None = Query(None),
    validate_results: bool | None = Query(None),
):
    """Create a task from one DXF file. Source polygons are prepared in the API."""
    input_obj = await _read_upload_input(file, dxf_only=True)
    if whole is None:
        try:
            parsed = _parameters_from_upload_config(config, start=bool(start))
        except (ValueError, ValidationError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        whole = parsed.whole if "whole" in parsed.model_fields_set else True
    return await _finish_source_upload(
        config=config, input_obj=input_obj, start=start, smooth=smooth, scan_mode=scan_mode,
        whole=whole,
        validate_results=validate_results,
    )


@app.post("/v1/tasks/tables_upload", response_model=TaskCreated, deprecated=True)
async def create_task_tables_upload(
    config: Annotated[str | None, Form()] = None,
    nodes_file: UploadFile = File(...),
    elements_file: UploadFile = File(...),
    loads_file: UploadFile = File(...),
    load_column: Annotated[int, Form(ge=1, le=4)] = 1,
    start: bool = Query(True),
    smooth: bool = Query(False),
    scan_mode: str | None = Query(None),
    whole: bool | None = Query(None),
    validate_results: bool | None = Query(None),
):
    """Create a task from three XLSX exports; polygons are prepared in the API."""
    nodes_content, elements_content, loads_content = await asyncio.gather(
        _read_xlsx_file(nodes_file, label="nodes_file"),
        _read_xlsx_file(elements_file, label="elements_file"),
        _read_xlsx_file(loads_file, label="loads_file"),
    )
    bundle = await run_in_threadpool(pack_xlsx_tables_bundle, nodes_content, elements_content, loads_content)
    input_obj = {
        "kind": "xlsx_tables", "filename": "tables.zip", "content": bundle, "load_column": int(load_column),
        "nodes_filename": nodes_file.filename or "nodes.xlsx",
        "elements_filename": elements_file.filename or "elements.xlsx",
        "loads_filename": loads_file.filename or "loads.xlsx",
    }
    return await _finish_source_upload(
        config=config, input_obj=input_obj, start=start, smooth=smooth, scan_mode=scan_mode, whole=whole,
        validate_results=validate_results,
    )


@app.post("/v1/tasks/json_upload", response_model=TaskCreated, deprecated=True)
async def create_task_json_upload(
    config: Annotated[str | None, Form()] = None,
    file: UploadFile = File(...),
    start: bool = Query(True), smooth: bool = Query(False), scan_mode: str | None = Query(None),
    whole: bool | None = Query(None),
    validate_results: bool | None = Query(None),
):
    if not (file.filename or "").lower().endswith(".json"):
        raise HTTPException(status_code=415, detail="file must be a .json file")
    content = await _read_upload_bytes(file)
    input_obj = {"kind": "json", "filename": file.filename or "polygons.json", "content": content}
    return await _finish_source_upload(
        config=config, input_obj=input_obj, start=start, smooth=smooth, scan_mode=scan_mode, whole=whole,
        validate_results=validate_results,
    )


@app.post("/v1/tasks/pickle_upload", response_model=TaskCreated)
async def create_task_pickle_upload(
    config: Annotated[str | None, Form()] = None,
    file: UploadFile = File(...),
    start: bool = Query(True), smooth: bool = Query(False), scan_mode: str | None = Query(None),
    whole: bool | None = Query(None),
    validate_results: bool | None = Query(None),
):
    suffix = (file.filename or "").lower()
    if not suffix.endswith((".pickle", ".pkl")):
        raise HTTPException(status_code=415, detail="file must be a .pickle or .pkl file")
    content = await _read_upload_bytes(file)
    input_obj = {"kind": "pickle", "filename": file.filename or "polygons.pickle", "content": content}
    return await _finish_source_upload(
        config=config, input_obj=input_obj, start=start, smooth=smooth, scan_mode=scan_mode, whole=whole,
        validate_results=validate_results,
    )


@app.post("/v1/source-polygons/upload")
async def source_polygons_upload(file: Annotated[UploadFile, File()]):
    """Parse a DXF and return source polygons without creating a task or writing Redis state."""
    input_obj = await _read_upload_input(file, dxf_only=True)
    try:
        polygons = await run_in_threadpool(source_polygons_from_input, input_obj)
    except SourcePolygonsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(polygons)


@app.post("/v1/verification/zones", response_model=VerificationResponse, deprecated=True)
async def verify_zones(request: CompactVerificationRequest):
    from .solution_verification import verify_compact_zones

    scene = await run_in_threadpool(store.get_scene, request.scene_id)
    if scene is None:
        raise HTTPException(status_code=404, detail="Scene not found")
    if str(scene.get("state")) != "ready":
        raise HTTPException(status_code=409, detail="Scene is not ready")
    try:
        overlay_id = await run_in_threadpool(
            lambda: store.resolve_scene_overlay_id(request.scene_id, request.overlay_id)
        )
        polygons = await run_in_threadpool(
            lambda: store.resolved_scene_polygons(
                request.scene_id, variant=analysis_variant(request.smooth), overlay_id=overlay_id
            )
        )
        zones = [zone.model_dump(mode="python") for zone in request.zones]
        coverage = await run_in_threadpool(
            lambda: verify_compact_zones(polygons, zones, back_grid=request.back_grid)
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return VerificationResponse(
        scene_id=request.scene_id, smooth=bool(request.smooth),
        overlay_id=int(overlay_id), coverage=coverage,
    )


@app.post("/v1/verification/task", response_model=VerificationResponse, deprecated=True)
async def verify_task_solution(request: StoredSolutionVerificationRequest):
    from .compact_zones import compact_zones_from_layout
    from .solution_verification import verify_compact_zones

    meta = await run_in_threadpool(store.get_meta, request.task_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Task not found")
    task_scene_id = str(meta.get("scene_id") or request.task_id)
    if task_scene_id != request.scene_id:
        raise HTTPException(status_code=422, detail="scene_id does not belong to task_id")
    variant = str(meta.get("analysis_variant") or meta.get("initial_variant") or "raw")
    overlay_id = int(meta.get("analysis_overlay_id", 0) or 0)
    try:
        polygons = await run_in_threadpool(
            lambda: store.resolved_source_polygons(request.task_id, variant=variant, overlay_id=overlay_id)
        )
        field = await run_in_threadpool(
            lambda: store.load_field(request.task_id, variant=variant, overlay_id=overlay_id)
        )
        cfg = dict((field or {}).get("cfg", {}) or {})
        if not cfg.get("back_grid"):
            raise HTTPException(status_code=409, detail="Prepared background configuration is missing")
        back_grid = cfg.get("back_grid")
        if request.component_id is None:
            solution = await run_in_threadpool(
                lambda: store.best_solution(request.task_id, request.n, variant=variant, overlay_id=overlay_id)
            )
            if solution is None:
                raise HTTPException(status_code=404, detail="Result not found")
            zones = list(solution.get("compact_zones", []) or [])
            if not zones and solution.get("bar_layout"):
                zones = compact_zones_from_layout(solution["bar_layout"])
        else:
            component_key = component_storage_id(request.component_id)
            frontier = await run_in_threadpool(
                lambda: store.load_frontier(
                    request.task_id, component_key, variant=variant, overlay_id=overlay_id
                )
            )
            row = frontier.get(int(request.n))
            if row is None:
                raise HTTPException(status_code=404, detail="Component result not found")
            zones = list(row.get("compact_zones", []) or [])
            if not zones:
                if not row.get("is_feasible"):
                    raise HTTPException(status_code=409, detail="Component has no feasible layout")
                view = await run_in_threadpool(lambda: workflow.component_layout_view(
                    request.task_id, component_key, request.n, row, variant=variant, overlay_id=overlay_id))
                if not view.get("is_feasible"):
                    raise HTTPException(status_code=409, detail="Component has no feasible bar layout")
                zones = view.get("compact_zones", [])
        coverage = await run_in_threadpool(
            lambda: verify_compact_zones(polygons, zones, back_grid=back_grid)
        )
    except HTTPException:
        raise
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return VerificationResponse(
        scene_id=task_scene_id, task_id=request.task_id, n=request.n,
        component_id=request.component_id, smooth=variant == "smooth",
        overlay_id=overlay_id, coverage=coverage,
    )


@app.get("/v1/tasks/{task_id}")
async def get_task(task_id: str):
    snapshot = await run_in_threadpool(store.snapshot, task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return JSONResponse(to_jsonable(snapshot))


@app.get("/v1/tasks/{task_id}/source-polygons")
async def get_source_polygons(task_id: str, smooth: bool | None = Query(None), overlay: int | None = Query(None)):
    _meta, variant, _selected_smooth, selected_overlay = await _read_task_context(
        task_id, smooth=smooth, overlay=overlay
    )
    try:
        polygons = await run_in_threadpool(
            lambda: store.resolved_source_polygons(task_id, variant=variant, overlay_id=selected_overlay)
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Historical endpoint intentionally remains a bare polygon list for the
    # current frontend. The new scene_polygons endpoint returns the context envelope.
    return JSONResponse(to_jsonable(polygons))


@app.get("/v1/tasks/{task_id}/overlays", deprecated=True)
async def list_overlays(task_id: str):
    meta = await run_in_threadpool(store.get_meta, task_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Task not found")
    scene_id = str(meta.get("scene_id") or task_id)
    rows = await run_in_threadpool(store.scene_overlay_events, scene_id)
    return JSONResponse(to_jsonable({"task_id": task_id, "scene_id": scene_id, "overlays": rows}))


@app.post("/v1/tasks/{task_id}/overlays", deprecated=True)
async def append_overlays(task_id: str, mutations: list[OverlayEventMutation]):
    meta = await run_in_threadpool(store.get_meta, task_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Task not found")
    scene_id = str(meta.get("scene_id") or task_id)
    try:
        rows = await run_in_threadpool(
            lambda: store.append_scene_overlay_events(scene_id, [row.model_dump(mode="python") for row in mutations])
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(to_jsonable({"task_id": task_id, "scene_id": scene_id, "overlays": rows}))


# ---------- component / solution API ----------
@app.get("/v1/tasks/{task_id}/components",deprecated=True)
async def list_components(task_id: str, smooth: bool | None = Query(None), overlay: int | None = Query(None)):
    meta, variant, selected_smooth, selected_overlay = await _read_task_context(
        task_id, smooth=smooth, overlay=overlay
    )
    components = await run_in_threadpool(
        lambda: store.components(task_id, variant=variant, overlay_id=selected_overlay)
    )
    field = await run_in_threadpool(
        lambda: store.load_field(task_id, variant=variant, overlay_id=selected_overlay)
    )
    split = field.get("decomposition", {}) if field else {}
    return JSONResponse(to_jsonable({
        "task_id": task_id, "scene_id": meta.get("scene_id"),
        "state": meta.get("state", "unknown"), "variant": variant,
        "smooth": selected_smooth, "overlay_id": selected_overlay,
        "active_indices": split.get("active_indices", []),
        "background_only_indices": split.get("background_only_indices", []),
        "degenerate_indices": split.get("degenerate_indices", []),
        "components": [row.get("info", row) for row in components],
    }))


@app.get("/v1/tasks/{task_id}/components/{component_id}",deprecated=True)
async def get_component(task_id: str, component_id: int, smooth: bool | None = Query(None), overlay: int | None = Query(None)):
    meta, variant, selected_smooth, selected_overlay = await _read_task_context(
        task_id, smooth=smooth, overlay=overlay
    )
    immutable = str(meta.get("scene_id") or task_id) != str(task_id)
    if component_id == -1 and immutable:
        info = await run_in_threadpool(
            lambda: workflow.aggregate_component_info(task_id, variant=variant, overlay_id=selected_overlay)
        )
        if info.get("state") == "empty":
            return JSONResponse(to_jsonable({
                "task_id": task_id, "scene_id": meta.get("scene_id"), "variant": variant,
                "smooth": selected_smooth, "overlay_id": selected_overlay, **info,
            }))
        return JSONResponse(to_jsonable({
            "task_id": task_id, "scene_id": meta.get("scene_id"), "variant": variant,
            "smooth": selected_smooth, "overlay_id": selected_overlay, **info,
        }))

    storage_id = component_storage_id(component_id)
    row = await run_in_threadpool(
        lambda: store.load_component(task_id, storage_id, variant=variant, overlay_id=selected_overlay)
    )
    if row is None and storage_id == "whole":
        try:
            info = await run_in_threadpool(
                lambda: workflow.whole_component_info(task_id, variant=variant, overlay_id=selected_overlay)
            )
        except (KeyError, ValueError):
            info = None
        if info is not None:
            return JSONResponse(to_jsonable({
                "task_id": task_id, "scene_id": meta.get("scene_id"), "variant": variant,
                "smooth": selected_smooth, "overlay_id": selected_overlay, **info,
            }))
    if row is None:
        raise HTTPException(status_code=404, detail="Component not found")
    return JSONResponse(to_jsonable({
        "task_id": task_id, "scene_id": meta.get("scene_id"), "variant": variant,
        "smooth": selected_smooth, "overlay_id": selected_overlay, **row.get("info", row),
    }))


@app.post("/v1/tasks/{task_id}/components/{component_id}/n", status_code=202,deprecated=True)
async def schedule_component_n(task_id: str, component_id: int, body: ComponentNRequest, smooth: bool | None = Query(None), overlay: int | None = Query(None)):
    meta, variant, selected_smooth, selected_overlay = await _read_task_context(
        task_id, smooth=smooth, overlay=overlay
    )
    try:
        queued = await run_in_threadpool(
            lambda: workflow.schedule_component_n(
                task_id, component_id, body.n, smooth=selected_smooth, overlay_id=selected_overlay
            )
        )
    except AnalysisNotPreparedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "task_id": task_id, "scene_id": meta.get("scene_id"), "component_id": component_id,
        "queued_n": queued, "status": "queued", "variant": variant,
        "smooth": selected_smooth, "overlay_id": selected_overlay,
    }


@app.get("/v1/tasks/{task_id}/components/{component_id}/results",deprecated=True)
async def list_component_results(task_id: str, component_id: int, smooth: bool | None = Query(None), overlay: int | None = Query(None)):
    meta, variant, selected_smooth, selected_overlay = await _read_task_context(
        task_id, smooth=smooth, overlay=overlay
    )
    immutable = str(meta.get("scene_id") or task_id) != str(task_id)
    if component_id == -1 and immutable:
        frontier = await run_in_threadpool(
            lambda: workflow.aggregate_frontier(task_id, variant=variant, overlay_id=selected_overlay)
        )
    else:
        frontier = await run_in_threadpool(
            lambda: store.load_frontier(
                task_id, component_storage_id(component_id), variant=variant, overlay_id=selected_overlay
            )
        )
    return {
        "task_id": task_id, "scene_id": meta.get("scene_id"), "component_id": component_id,
        "variant": variant, "smooth": selected_smooth, "overlay_id": selected_overlay,
        "results": [
            {
                "n": int(n), "is_feasible": bool(row.get("is_feasible")),
                "is_optimal": bool(row.get("is_optimal", False)), "proxy_mass": row.get("proxy_mass"),
                "status": row.get("status"), "solve_state": row.get("solve_state"),
                "overlay_id": selected_overlay,
            }
            for n, row in frontier.items()
        ],
    }


@app.get("/v1/tasks/{task_id}/components/{component_id}/results/{n}",deprecated=True)
async def get_component_result(task_id: str, component_id: int, n: int, smooth: bool | None = Query(None), overlay: int | None = Query(None)):
    meta, variant, selected_smooth, selected_overlay = await _read_task_context(
        task_id, smooth=smooth, overlay=overlay
    )
    immutable = str(meta.get("scene_id") or task_id) != str(task_id)
    if component_id == -1 and immutable:
        frontier = await run_in_threadpool(
            lambda: workflow.aggregate_frontier(task_id, variant=variant, overlay_id=selected_overlay)
        )
    else:
        frontier = await run_in_threadpool(
            lambda: store.load_frontier(
                task_id, component_storage_id(component_id), variant=variant, overlay_id=selected_overlay
            )
        )
    row = frontier.get(int(n))
    if row is None:
        raise HTTPException(status_code=404, detail="Component result not found")
    body = dict(row)
    body.update(
        task_id=task_id, scene_id=meta.get("scene_id"), component_id=component_id,
        variant=variant, smooth=selected_smooth, overlay_id=selected_overlay,
    )
    return JSONResponse(public_value(body))


@app.get("/v1/tasks/{task_id}/solutions",deprecated=True)
async def list_solutions(
    task_id: str,
    total_n: int | None = Query(None),
    source: str | None = Query(None),
    status: str | None = Query(None),
    smooth: bool | None = Query(None),
    overlay: int | None = Query(None),
):
    meta, variant, selected_smooth, selected_overlay = await _read_task_context(
        task_id, smooth=smooth, overlay=overlay
    )
    rows = await run_in_threadpool(
        lambda: store.solution_summaries(
            task_id, total_n=total_n, source=source, variant=variant, overlay_id=selected_overlay
        )
    )
    if status is not None:
        rows = [row for row in rows if str(row.get("status")) == status]
    return {
        "task_id": task_id, "scene_id": meta.get("scene_id"), "variant": variant,
        "smooth": selected_smooth, "overlay_id": selected_overlay,
        "solutions": [
            {
                "solution_id": row["solution_id"], "source": row.get("source", "components"),
                "variant": variant, "smooth": selected_smooth, "total_N": int(row["total_N"]),
                "component_ns": row.get("component_ns", {}), "proxy_mass": row.get("proxy_mass"),
                "actual_mass_kg": row.get("actual_mass_kg"),
                "is_feasible": bool(row.get("is_feasible")),
                "is_optimal": bool(row.get("is_optimal", False)), "status": row.get("status"),
                "overlay_id": selected_overlay,
                "result_url": f"/v1/tasks/{task_id}/solutions/{row['solution_id']}?overlay={selected_overlay}",
            }
            for row in rows
        ],
    }


@app.get("/v1/tasks/{task_id}/solutions/{solution_id}",deprecated=True)
async def get_solution(task_id: str, solution_id: str, overlay: int | None = Query(None)):
    meta, variant, selected_smooth, selected_overlay = await _read_task_context(
        task_id, overlay=overlay
    )
    immutable = str(meta.get("scene_id") or task_id) != task_id
    # A historical bookmarked solution_id already identifies its context. Do not
    # hide smooth/overlay history behind the new raw/0 compatibility default.
    filter_overlay = selected_overlay if immutable or overlay is not None else None
    row = await run_in_threadpool(
        lambda: store.load_solution(task_id, solution_id, overlay_id=filter_overlay)
    )
    if row is None or (immutable and str(row.get("variant", "raw")) != variant):
        raise HTTPException(status_code=404, detail="Solution not found")
    if not immutable:
        variant = str(row.get("variant", "raw"))
        selected_smooth = variant == "smooth"
        selected_overlay = int(row.get("overlay_id", 0))
    body = dict(row)
    body.update(
        task_id=task_id, scene_id=meta.get("scene_id"), variant=variant,
        smooth=selected_smooth, overlay_id=selected_overlay,
    )
    return JSONResponse(public_value(body))


@app.get("/v1/tasks/{task_id}/component-events")
async def component_events(task_id: str, start: int = 0, overlay: int | None = Query(None)):
    meta, _variant, _selected_smooth, selected_overlay = await _read_task_context(task_id, overlay=overlay)
    return {
        "task_id": task_id, "scene_id": meta.get("scene_id"), "overlay_id": selected_overlay,
        "events": await run_in_threadpool(
            lambda: store.all_events(task_id, start, overlay_id=selected_overlay)
        ),
    }


# ---------- historical frontend-compatible result API ----------
@app.get("/v1/tasks/{task_id}/results",deprecated=True)
async def list_results(task_id: str, smooth: bool | None = Query(None), overlay: int | None = Query(None)):
    meta = await run_in_threadpool(store.get_meta, task_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        variant, selected_smooth, selected_overlay = await run_in_threadpool(
            lambda: _task_context(task_id, meta, smooth=smooth, overlay=overlay)
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404 if isinstance(exc, KeyError) else 422, detail=str(exc)) from exc
    rows = await run_in_threadpool(
        lambda: store.get_result_metas(task_id, variant=variant, overlay_id=selected_overlay)
    )
    if isinstance(rows, dict):
        enriched = {
            str(key): {
                **dict(row),
                "scene_id": meta.get("scene_id"),
                "smooth": selected_smooth,
                "overlay_id": selected_overlay,
            }
            for key, row in rows.items()
        }
    else:
        # Compatibility for lightweight stores/test doubles that return a list.
        enriched = [
            {
                **dict(row),
                "scene_id": meta.get("scene_id"),
                "smooth": selected_smooth,
                "overlay_id": selected_overlay,
            }
            for row in rows
        ]
    return JSONResponse(to_jsonable(enriched))


@app.get("/v1/tasks/{task_id}/results/{n}",deprecated=True)
async def get_result(task_id: str, n: int, smooth: bool | None = Query(None), overlay: int | None = Query(None)):
    meta = await run_in_threadpool(store.get_meta, task_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        variant, selected_smooth, selected_overlay = await run_in_threadpool(
            lambda: _task_context(task_id, meta, smooth=smooth, overlay=overlay)
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404 if isinstance(exc, KeyError) else 422, detail=str(exc)) from exc
    solution = await run_in_threadpool(
        lambda: store.best_solution(task_id, n, variant=variant, overlay_id=selected_overlay)
    )
    result = (
        to_compat_result(solution)
        if solution is not None
        else await run_in_threadpool(lambda: store.get_result(task_id, n, variant=variant, overlay_id=selected_overlay))
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Result not found")
    body = dict(result)
    body.update(scene_id=meta.get("scene_id"), smooth=selected_smooth, overlay_id=selected_overlay)
    return JSONResponse(to_jsonable(body))


@app.get("/v1/tasks/{task_id}/results/{n}/dxf",deprecated=True)
async def get_result_dxf(task_id: str, n: int, smooth: bool | None = Query(None), overlay: int | None = Query(None)):
    meta = await run_in_threadpool(store.get_meta, task_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        variant, _selected_smooth, selected_overlay = await run_in_threadpool(
            lambda: _task_context(task_id, meta, smooth=smooth, overlay=overlay)
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404 if isinstance(exc, KeyError) else 422, detail=str(exc)) from exc
    solution = await run_in_threadpool(
        lambda: store.best_solution(task_id, n, variant=variant, overlay_id=selected_overlay)
    )
    result = (
        to_compat_result(solution)
        if solution is not None
        else await run_in_threadpool(lambda: store.get_result(task_id, n, variant=variant, overlay_id=selected_overlay))
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Result not found")
    try:
        source_input = await run_in_threadpool(store.get_object, task_id, "input")
        exported = await run_in_threadpool(lambda: build_solution_dxf(source_input, result, n=n))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Source input not found") from exc
    except DxfExportError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(
        content=exported.content, media_type="application/dxf",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(exported.filename)},
    )


@app.get("/v1/tasks/{task_id}/events")
async def get_events(task_id: str, after: str = "0-0", count: int = 200, overlay: int | None = Query(None)):
    _meta, _variant, _selected_smooth, selected_overlay = await _read_task_context(task_id, overlay=overlay)
    return await run_in_threadpool(
        lambda: store.read_events(
            task_id, after, min(max(count, 1), 1000), overlay_id=selected_overlay
        )
    )


@app.post("/v1/tasks/{task_id}/n",deprecated=True)
async def add_n(task_id: str, mutation: NMutation, smooth: bool | None = Query(None), overlay: int | None = Query(None)):
    meta, variant, selected_smooth, selected_overlay = await _read_task_context(
        task_id, smooth=smooth, overlay=overlay
    )
    ns = mutation.n if isinstance(mutation.n, list) else [mutation.n]
    try:
        queued = await run_in_threadpool(
            lambda: workflow.schedule_requested_for_all(
                task_id, ns, smooth=selected_smooth, overlay_id=selected_overlay
            )
        )
        plan = await run_in_threadpool(
            lambda: store.get_plan(task_id, variant=variant, overlay_id=selected_overlay)
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await run_in_threadpool(
        lambda: store.publish_event(
            task_id, "n_added",
            {
                "n": ns, "components": queued, "variant": variant, "smooth": selected_smooth,
                "overlay_id": selected_overlay,
            },
            overlay_id=selected_overlay,
        )
    )
    if isinstance(plan, dict):
        plan = {**plan, "scene_id": meta.get("scene_id"), "smooth": selected_smooth, "overlay_id": selected_overlay}
    return plan


@app.post("/v1/tasks/{task_id}/cancel")
async def cancel(task_id: str, mutation: CancelMutation, smooth: bool | None = Query(None), overlay: int | None = Query(None)):
    _meta, variant, _selected_smooth, selected_overlay = await _read_task_context(
        task_id, smooth=smooth, overlay=overlay
    )
    if mutation.n:
        await run_in_threadpool(
            lambda: store.cancel_ns(task_id, mutation.n, variant=variant, overlay_id=selected_overlay)
        )
    else:
        await run_in_threadpool(store.cancel_task, task_id)
    return await run_in_threadpool(store.snapshot, task_id)


@app.post("/v1/tasks/{task_id}/pause",deprecated=True)
async def pause_task(task_id: str):
    try:
        meta, variant, selected_smooth, selected_overlay = await _read_task_context(task_id)
        await run_in_threadpool(store.set_paused, task_id, True)
        plan = await run_in_threadpool(
            lambda: store.get_plan(task_id, variant=variant, overlay_id=selected_overlay)
        )
        return {
            **dict(plan),
            "scene_id": meta.get("scene_id"),
            "smooth": selected_smooth,
            "overlay_id": selected_overlay,
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc


@app.post("/v1/tasks/{task_id}/resume",deprecated=True)
async def resume_task(task_id: str):
    try:
        meta, variant, selected_smooth, selected_overlay = await _read_task_context(task_id)
        await run_in_threadpool(store.set_paused, task_id, False)
        plan = await run_in_threadpool(
            lambda: store.get_plan(task_id, variant=variant, overlay_id=selected_overlay)
        )
        return {
            **dict(plan),
            "scene_id": meta.get("scene_id"),
            "smooth": selected_smooth,
            "overlay_id": selected_overlay,
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc


async def _ws_command(task_id: str, raw: dict) -> dict:
    command = WsCommand.model_validate(raw)
    if command.action == "snapshot":
        return {"type": "snapshot", "data": await run_in_threadpool(store.snapshot, task_id)}

    _meta, variant, selected_smooth, selected_overlay = await _read_task_context(
        task_id, smooth=command.smooth, overlay=command.overlay
    )
    if command.action == "add":
        ns = command.n if isinstance(command.n, list) else [command.n]
        if ns == [None]:
            raise ValueError("Для add требуется n")
        queued = await run_in_threadpool(
            lambda: workflow.schedule_requested_for_all(
                task_id, ns, smooth=selected_smooth, overlay_id=selected_overlay
            )
        )
        await run_in_threadpool(
            lambda: store.publish_event(
                task_id, "n_added",
                {
                    "n": ns, "components": queued, "variant": variant,
                    "smooth": selected_smooth, "overlay_id": selected_overlay,
                },
                overlay_id=selected_overlay,
            )
        )
    elif command.action == "cancel":
        ns = command.n if isinstance(command.n, list) else [command.n]
        if ns == [None]:
            raise ValueError("Для cancel требуется n")
        await run_in_threadpool(
            lambda: store.cancel_ns(
                task_id, ns, variant=variant, overlay_id=selected_overlay
            )
        )
    elif command.action in {"pause_range", "resume_range"}:
        await run_in_threadpool(store.set_paused, task_id, command.action == "pause_range")
    elif command.action == "cancel_task":
        await run_in_threadpool(store.cancel_task, task_id)
    return {"type": "ack", "action": command.action}


@app.websocket("/v1/tasks/{task_id}/ws")
async def task_websocket(websocket: WebSocket, task_id: str, after: str = "0-0", overlay: int = 0):
    try:
        _meta, _variant, _selected_smooth, selected_overlay = await _read_task_context(
            task_id, overlay=overlay
        )
    except HTTPException as exc:
        await websocket.close(code=4404 if exc.status_code == 404 else 4400)
        return
    snapshot = await run_in_threadpool(store.snapshot, task_id)
    if snapshot is None:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    send_lock = asyncio.Lock()
    await websocket.send_json({"type": "snapshot", "data": to_jsonable(snapshot)})
    async def sender():
        nonlocal after
        while True:
            rows = await run_in_threadpool(
                lambda: store.read_events(task_id, after, 100, overlay_id=selected_overlay)
            )
            if not rows:
                await asyncio.sleep(settings.event_poll_interval_seconds)
                continue
            for body in rows:
                after = str(body["id"])
                async with send_lock:
                    await websocket.send_json(to_jsonable(body))

    async def receiver():
        while True:
            raw = await websocket.receive_json()
            try:
                reply = await _ws_command(task_id, raw)
            except Exception as exc:
                reply = {"type": "command_error", "error": str(exc)}
            async with send_lock:
                await websocket.send_json(to_jsonable(reply))

    tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            task.result()
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()


from .v2.api import router as v2_router

app.include_router(v2_router)

# Documentation only: request/response contracts and runtime validation stay unchanged.
from .api_docs_ru import install_russian_docs

install_russian_docs(app)
