"""The /v2 router: minimalist scene API of ``payload-v2-am-aa.md``.

The module imports without Redis or PostgreSQL: the store is resolved lazily from
``rebar_service.api`` inside the handlers, which keeps the import graph acyclic
(``rebar_service.api`` mounts this router) and lets tests monkeypatch
``rebar_service.api.store``.
"""
from __future__ import annotations

import asyncio
import time
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..jsonutil import to_jsonable
from . import scenes
from .models import (
    BarsCreated,
    BarsRequest,
    BarsView,
    CancelMutation,
    FEPolygon,
    FEPolygonOut,
    NMutation,
    OverlayCreated,
    OverlayOut,
    OverlaysPost,
    SceneCreated,
    SolutionView,
    TaskCreate,
    TaskCreated,
    TaskView,
    VerificationCreated,
    VerificationRequest,
    VerificationView,
)


router = APIRouter(prefix="/v2")


def _api_module(request: Request | None = None):
    """Resolve the module that owns the serving app (``app.state.api_module``).

    Falling back to an import keeps the router usable outside FastAPI; going through the
    app makes the lookup robust when tests re-import ``rebar_service.api``.
    """

    if request is not None:
        module = getattr(request.app.state, "api_module", None)
        if module is not None:
            return module
    from rebar_service import api as _api

    return _api


def _store(request: Request | None = None) -> Any:
    return _api_module(request).store


async def _create_scene_response(request: Request, input_obj: dict) -> SceneCreated:
    """Persist a parsed source; source-parsing failures are a synchronous 422."""

    try:
        created = await run_in_threadpool(lambda: scenes.create_scene(_store(request), input_obj))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return SceneCreated.model_validate(created)


def _scene_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, scenes.SceneNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, scenes.SceneNotReadyError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (KeyError, IndexError)):
        return HTTPException(status_code=404, detail=str(exc.args[0] if exc.args else exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    raise exc


# ------------------------------------------------------------------- uploads


@router.post("/dxf_upload", response_model=SceneCreated)
async def v2_create_scene_dxf(request: Request, file: UploadFile = File(...)):
    """Parse one DXF synchronously and create a ready reusable scene."""

    input_obj = await _api_module(request)._read_upload_input(file, dxf_only=True)
    return await _create_scene_response(request, input_obj)


@router.post("/json_upload", response_model=SceneCreated)
async def v2_create_scene_json(request: Request, polygons: list[FEPolygon]):
    """Create a ready scene from a bare ``FEPolygon[]`` JSON body."""

    try:
        input_obj = scenes.json_input_obj(
            [polygon.model_dump(mode="python") for polygon in polygons]
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await _create_scene_response(request, input_obj)


@router.post("/tables_upload", response_model=SceneCreated)
async def v2_create_scene_tables(request: Request,
    nodes_file: UploadFile = File(...),
    elements_file: UploadFile = File(...),
    loads_file: UploadFile = File(...),
    load_column: Annotated[int, Form(ge=1, le=4)] = 1,
):
    """Create a ready scene from the three XLSX source tables."""

    api = _api_module(request)
    nodes_content, elements_content, loads_content = await asyncio.gather(
        api._read_xlsx_file(nodes_file, label="nodes_file"),
        api._read_xlsx_file(elements_file, label="elements_file"),
        api._read_xlsx_file(loads_file, label="loads_file"),
    )
    from ..source_polygons import pack_xlsx_tables_bundle

    bundle = await run_in_threadpool(
        pack_xlsx_tables_bundle, nodes_content, elements_content, loads_content
    )
    input_obj = scenes.tables_input_obj(
        bundle,
        load_column=int(load_column),
        nodes_filename=nodes_file.filename,
        elements_filename=elements_file.filename,
        loads_filename=loads_file.filename,
    )
    return await _create_scene_response(request, input_obj)


# -------------------------------------------------------------------- scenes


@router.get("/scenes/{scene_id}/polygons", response_model=list[FEPolygonOut])
async def v2_scene_polygons(request: Request,
    scene_id: str,
    smooth: bool = Query(False),
    overlay_id: int = Query(0),
):
    """Return the full stable polygon list of one scene revision."""

    try:
        rows = await run_in_threadpool(
            lambda: scenes.resolved_polygons(
                _store(request), scene_id, smooth=bool(smooth), overlay_id=int(overlay_id)
            )
        )
    except Exception as exc:  # noqa: BLE001 - mapped to the documented status codes
        raise _scene_http_error(exc) from exc
    return JSONResponse(to_jsonable(rows))


@router.post("/scenes/{scene_id}/overlays", response_model=OverlayCreated)
async def v2_append_overlays(request: Request, scene_id: str, body: OverlaysPost):
    """Append eraser events to a scene and return the id of the last stored event."""

    if body.scene_id is not None and str(body.scene_id) != str(scene_id):
        raise HTTPException(status_code=422, detail="scene_id в теле не совпадает с путём")
    store = _store(request)
    if await run_in_threadpool(store.get_scene, scene_id) is None:
        raise HTTPException(status_code=404, detail=f"Сцена {scene_id} не найдена")
    payload = [overlay.model_dump(mode="python") for overlay in body.overlays]
    try:
        overlay_id = await run_in_threadpool(
            lambda: scenes.append_overlays(store, scene_id, payload)
        )
    except Exception as exc:  # noqa: BLE001 - mapped to the documented status codes
        raise _scene_http_error(exc) from exc
    return OverlayCreated(scene_id=scene_id, overlay_id=overlay_id)


@router.get("/scenes/{scene_id}/overalys/{overlay_id}", include_in_schema=False)
async def v2_get_overlays_contract_spelling(request: Request, scene_id: str, overlay_id: int):
    """Alias for the path as (mis)spelled in payload-v2-am-aa.md."""

    return await v2_get_overlays(request, scene_id, overlay_id)


@router.get("/scenes/{scene_id}/overlays/{overlay_id}", response_model=list[OverlayOut])
async def v2_get_overlays(request: Request, scene_id: str, overlay_id: int):
    """Return the overlay log of a scene up to and including one revision."""

    store = _store(request)
    if await run_in_threadpool(store.get_scene, scene_id) is None:
        raise HTTPException(status_code=404, detail=f"Сцена {scene_id} не найдена")
    try:
        rows = await run_in_threadpool(lambda: scenes.overlay_log(store, scene_id, overlay_id))
    except Exception as exc:  # noqa: BLE001 - mapped to the documented status codes
        raise _scene_http_error(exc) from exc
    return JSONResponse(to_jsonable(rows))


# --------------------------------------------------------------------- tasks


def _settings(request: Request | None = None) -> Any:
    return _api_module(request).settings


def _pipeline(request: Request):
    from .pipeline import V2Pipeline

    return V2Pipeline(_store(request), _settings(request))


async def _ready_scene(request: Request, scene_id: str) -> dict:
    try:
        return await run_in_threadpool(lambda: scenes.require_ready_scene(_store(request), scene_id))
    except Exception as exc:  # noqa: BLE001 - mapped to the documented status codes
        raise _scene_http_error(exc) from exc


async def _resolve_overlay(request: Request, scene_id: str, selector: int) -> int:
    try:
        store = _store(request)
        return int(await run_in_threadpool(lambda: store.resolve_scene_overlay_id(scene_id, selector)))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0] if exc.args else exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _check_ns(request: Request, values: list[int]) -> list[int]:
    limit = int(_settings(request).max_n)
    too_big = [n for n in values if int(n) > limit]
    if too_big:
        raise HTTPException(status_code=422, detail=f"N {too_big} превышает REBAR_MAX_N={limit}")
    return [int(n) for n in values]


def _summary(row: dict) -> dict:
    out: dict[str, Any] = {"n": int(row["n"]), "state": str(row["state"])}
    for key in ("fun", "status", "mass_metrics"):
        if row.get(key) is not None:
            out[key] = row[key]
    return out


async def _task_or_404(request: Request, task_id: str) -> dict:
    task = await run_in_threadpool(_store(request).v2.get_task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return task


async def _task_view(request: Request, task: dict) -> JSONResponse:
    rows = await run_in_threadpool(_store(request).v2.list_ns, str(task["task_id"]))
    body = {
        "task_id": task["task_id"], "scene_id": task["scene_id"], "smooth": bool(task["smooth"]),
        "overlay_id": int(task["overlay_id"]), "solutions": [_summary(row) for row in rows],
    }
    return JSONResponse(to_jsonable(body))


@router.put("/tasks", response_model=TaskCreated)
async def v2_create_task(request: Request, body: TaskCreate):
    """Create a whole-field optimisation task for the given N values."""

    await _ready_scene(request, body.scene_id)
    overlay_id = await _resolve_overlay(request, body.scene_id, body.overlay_id)
    ns = _check_ns(request, list(body.n))
    task_id = await run_in_threadpool(lambda: _pipeline(request).create_task(
        scene_id=body.scene_id, overlay_id=overlay_id, smooth=bool(body.smooth),
        config=body.config.model_dump(mode="python"), ns=ns,
    ))
    return TaskCreated(task_id=task_id)


@router.get("/tasks/{task_id}", response_model=TaskView)
async def v2_get_task(request: Request, task_id: str):
    """Task summary: one entry per N with its state, objective, status and masses."""

    return await _task_view(request, await _task_or_404(request, task_id))


@router.put("/tasks/{task_id}/n", response_model=TaskView)
async def v2_add_task_ns(request: Request, task_id: str, mutation: NMutation):
    """Add N values to an existing task."""

    task = await _task_or_404(request, task_id)
    if mutation.task_id is not None and str(mutation.task_id) != str(task_id):
        raise HTTPException(status_code=422, detail="task_id в теле не совпадает с путём")
    ns = _check_ns(request, list(mutation.n))
    await run_in_threadpool(lambda: _pipeline(request).add_ns(task_id, ns))
    return await _task_view(request, task)


@router.put("/tasks/{task_id}/cancel", response_model=TaskView)
async def v2_cancel_task(request: Request, task_id: str, mutation: CancelMutation | None = None):
    """Cancel the listed N values, or the whole task when no N is given."""

    task = await _task_or_404(request, task_id)
    if mutation is not None and mutation.task_id is not None and str(mutation.task_id) != str(task_id):
        raise HTTPException(status_code=422, detail="task_id в теле не совпадает с путём")
    ns = None if mutation is None or mutation.n is None else [int(n) for n in mutation.n]
    await run_in_threadpool(lambda: _pipeline(request).cancel(task_id, ns))
    return await _task_view(request, task)


@router.get("/tasks/{task_id}/{n}", response_model=SolutionView)
async def v2_get_task_solution(request: Request, task_id: str, n: int):
    """One N of a task, with bars and zones (without anchorage) once it succeeded."""

    await _task_or_404(request, task_id)
    row = await run_in_threadpool(lambda: _store(request).v2.get_n(task_id, int(n)))
    if row is None:
        raise HTTPException(status_code=404, detail="N не найден в задаче")
    body: dict[str, Any] = {"task_id": task_id, **_summary(row)}
    if row.get("error"):
        body["error"] = row["error"]
    result = row.get("result") or {}
    if str(row.get("state")) == "success" and result:
        body["bars"] = result.get("bars", [])
        body["zones"] = result.get("zones", [])
    return JSONResponse(to_jsonable(body))


# ---------------------------------------------------------- bars / verification


def _isolated_job(kind: str, task_id: str) -> dict:
    return {
        "kind": kind, "task_id": task_id, "payload": {}, "generation": 0,
        "dedupe_key": f"{kind}:{task_id}", "job_id": uuid4().hex, "created_at": time.time(),
        "schema_version": 1,
    }


@router.post("/bars", response_model=BarsCreated)
async def v2_create_bars(request: Request, body: BarsRequest):
    """Queue a bar layout for the given zones on the isolated bars worker."""

    await _ready_scene(request, body.scene_id)
    overlay_id = await _resolve_overlay(request, body.scene_id, body.overlay_id)
    task_id = uuid4().hex
    zones = [zone.model_dump(mode="python") for zone in body.zones]
    config = body.config.model_dump(mode="python")

    def create() -> None:
        store = _store(request)
        store.v2.create_bar_task(
            task_id, scene_id=body.scene_id, overlay_id=overlay_id, smooth=bool(body.smooth),
            config=config, zones=zones,
        )
        store.bars_queue.enqueue_pipeline_job(_isolated_job("bars", task_id))

    await run_in_threadpool(create)
    return BarsCreated(task_id=task_id, state="pending")


@router.get("/bars/{task_id}", response_model=BarsView)
async def v2_get_bars(request: Request, task_id: str):
    """State of a bar layout request and, once successful, its bars, zones and masses."""

    row = await run_in_threadpool(_store(request).v2.get_bar_task, task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Задача раскладки не найдена")
    body: dict[str, Any] = {"task_id": task_id, "state": row["state"]}
    if row.get("error"):
        body["error"] = row["error"]
    if str(row.get("state")) == "success" and row.get("result"):
        body.update({key: row["result"].get(key) for key in ("bars", "zones", "mass_metrics")})
    return JSONResponse(to_jsonable(body))


@router.post("/verification", response_model=VerificationCreated)
async def v2_create_verification(request: Request, body: VerificationRequest):
    """Queue a reinforcement verification (layout + per-polygon need/fact) on its own worker."""

    await _ready_scene(request, body.scene_id)
    overlay_id = await _resolve_overlay(request, body.scene_id, body.overlay_id)
    task_id = uuid4().hex
    zones = [zone.model_dump(mode="python") for zone in body.zones]
    config = body.config.model_dump(mode="python")

    def create() -> None:
        store = _store(request)
        store.v2.create_verification_task(
            task_id, scene_id=body.scene_id, overlay_id=overlay_id, smooth=bool(body.smooth),
            config=config, zones=zones,
        )
        store.verification_queue.enqueue_pipeline_job(_isolated_job("verification", task_id))

    await run_in_threadpool(create)
    return VerificationCreated(task_id=task_id, state="pending")


@router.get("/verification/{verification_task_id}", response_model=VerificationView)
async def v2_get_verification(request: Request, verification_task_id: str):
    """State of a verification request and, once successful, one row per source polygon."""

    row = await run_in_threadpool(_store(request).v2.get_verification_task, verification_task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Задача проверки не найдена")
    body: dict[str, Any] = {"verification_id": verification_task_id, "state": row["state"]}
    if row.get("error"):
        body["error"] = row["error"]
    if str(row.get("state")) == "success" and row.get("result") is not None:
        body["result"] = row["result"]
    return JSONResponse(to_jsonable(body))
