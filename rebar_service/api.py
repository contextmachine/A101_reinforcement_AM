from __future__ import annotations

import asyncio
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
    TaskCreate,
    TaskCreated,
    TaskParameters,
    WsCommand,
)
from .pipeline import PipelineWorkflow, normalize_input_payload, public_value
from .planner import normalize_n_request, validate_n_request_limits, validate_solver_limits
from .source_polygons import SourcePolygonsError, source_polygons_from_input
from .store import RedisStore


settings = get_settings()
store = RedisStore(settings)
workflow = PipelineWorkflow(store, settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await run_in_threadpool(store.ping)
    yield


app = FastAPI(title="Rebar Optimizer API", version="2.0.0", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=settings.cors_origin_list != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _build_task(parameters: TaskParameters, input_obj: dict) -> TaskCreated:
    validate_n_request_limits(
        parameters.n,
        max_values=settings.max_planned_n_values,
        max_n=settings.max_n_value,
    )
    validate_solver_limits(
        parameters.solver.model_dump(mode="python"),
        max_threads=settings.max_solver_threads,
        max_timeout=settings.max_solver_timeout_seconds,
    )
    requested_limit = parameters.max_concurrent_jobs
    if requested_limit is not None and requested_limit > settings.max_jobs_per_task:
        raise ValueError(
            f"max_concurrent_jobs={requested_limit} превышает серверный лимит {settings.max_jobs_per_task}"
        )

    mode, order, n_source = normalize_n_request(parameters.n)
    params = parameters.model_dump(mode="python")
    params.pop("n", None)
    prepared_max_n = params.get("solver", {}).get("prepared_max_n")
    if prepared_max_n is not None and int(prepared_max_n) > settings.max_n_value:
        raise ValueError(
            f"prepared_max_n={prepared_max_n} превышает серверный лимит {settings.max_n_value}"
        )

    task_id = uuid.uuid4().hex
    now = time.time()
    limit = int(requested_limit or settings.max_jobs_per_task)
    meta = {
        "task_id": task_id,
        "state": "queued_preparation",
        "created_at": now,
        "updated_at": now,
        "expires_at": now + settings.task_ttl_seconds,
        "cancelled": False,
        "paused": False,
        "parameters": params,
        "requested_n": list(map(int, order)),
        "n_mode": mode,
        "n_source": n_source,
        "scan_mode": parameters.scan_mode,
        "whole": bool(parameters.whole),
        "component_result_top_k": int(parameters.component_result_top_k),
        "validate_results": bool(parameters.validate_results),
        "max_concurrent_jobs": limit,
    }
    plan = {
        "mode": mode,
        "order": list(map(int, order)),
        "cursor": 0,
        "paused": False,
        "exhausted": False,
        "window": max(1, limit * settings.schedule_window_factor),
    }
    store.create_task(task_id, meta, plan, input_obj)
    store.publish_event(task_id, "task_created", {"state": meta["state"], "n_mode": mode, "planned": len(order)})
    workflow.bootstrap_task(task_id)
    return TaskCreated(
        task_id=task_id,
        state=meta["state"],
        websocket_url=f"/v1/tasks/{task_id}/ws",
        status_url=f"/v1/tasks/{task_id}",
    )


def _apply_upload_overrides(
    parameters: TaskParameters,
    *,
    scan_mode: str | None,
    whole: bool | None,
    component_result_top_k: int | None,
    validate_results: bool | None,
) -> TaskParameters:
    updates = {}
    if scan_mode is not None:
        updates["scan_mode"] = scan_mode
    if whole is not None:
        updates["whole"] = whole
    if component_result_top_k is not None:
        updates["component_result_top_k"] = component_result_top_k
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


@app.post("/v1/tasks", response_model=TaskCreated)
async def create_task(request: TaskCreate):
    try:
        parameters = TaskParameters.model_validate(request.model_dump(exclude={"input"}))
        return await run_in_threadpool(_build_task, parameters, request.input.model_dump(mode="python"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/v1/tasks/upload", response_model=TaskCreated)
async def create_task_upload(
    config: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    scan_mode: str | None = Query(None),
    whole: bool | None = Query(None),
    component_result_top_k: int | None = Query(None, ge=1, le=100),
    validate_results: bool | None = Query(None),
):
    try:
        parameters = TaskParameters.model_validate_json(config)
        parameters = _apply_upload_overrides(
            parameters,
            scan_mode=scan_mode,
            whole=whole,
            component_result_top_k=component_result_top_k,
            validate_results=validate_results,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    content = await file.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="Input file is too large")
    suffix = (file.filename or "").lower()
    if suffix.endswith(".dxf"):
        input_obj = {"kind": "dxf", "filename": file.filename or "input.dxf", "content": content}
    elif suffix.endswith(".json"):
        payload = loads(content, None)
        try:
            input_obj = normalize_input_payload(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    else:
        raise HTTPException(status_code=415, detail="Supported files: .dxf or .json")

    try:
        return await run_in_threadpool(_build_task, parameters, input_obj)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/v1/tasks/{task_id}")
async def get_task(task_id: str):
    snapshot = await run_in_threadpool(store.snapshot, task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return JSONResponse(to_jsonable(snapshot))


@app.get("/v1/tasks/{task_id}/source-polygons")
async def get_source_polygons(task_id: str):
    if await run_in_threadpool(store.get_meta, task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        source_input = await run_in_threadpool(store.get_object, task_id, "input")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Source input not found") from exc
    try:
        polygons = await run_in_threadpool(source_polygons_from_input, source_input)
    except SourcePolygonsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(polygons)


# ---------- component / solution API ----------
@app.get("/v1/tasks/{task_id}/components")
async def list_components(task_id: str):
    meta = await run_in_threadpool(store.get_meta, task_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Task not found")
    components = await run_in_threadpool(store.components, task_id)
    field = await run_in_threadpool(store.load_field, task_id)
    split = field.get("decomposition", {}) if field else {}
    return JSONResponse(
        to_jsonable(
            {
                "task_id": task_id,
                "state": meta.get("state", "unknown"),
                "active_indices": split.get("active_indices", []),
                "background_only_indices": split.get("background_only_indices", []),
                "degenerate_indices": split.get("degenerate_indices", []),
                "components": [
                    row.get("info", row)
                    for row in components
                    if row.get("component", {}).get("id") != -1
                ],
            }
        )
    )


@app.post("/v1/tasks/{task_id}/component-pipeline/prepare", status_code=202)
@app.post("/v1/tasks/{task_id}/components/prepare", status_code=202)
async def prepare_existing_task(task_id: str):
    if await run_in_threadpool(store.get_meta, task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    queued = await run_in_threadpool(workflow.enqueue, "prepare_field", task_id)
    return {"task_id": task_id, "queued": bool(queued)}


@app.get("/v1/tasks/{task_id}/components/{component_id}")
async def get_component(task_id: str, component_id: int):
    row = await run_in_threadpool(store.load_component, task_id, component_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Component not found")
    return JSONResponse(to_jsonable({"task_id": task_id, **row.get("info", row)}))


@app.post("/v1/tasks/{task_id}/components/{component_id}/n", status_code=202)
async def schedule_component_n(task_id: str, component_id: int, body: ComponentNRequest):
    try:
        queued = await run_in_threadpool(workflow.schedule_component_n, task_id, component_id, body.n)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"task_id": task_id, "component_id": component_id, "queued_n": queued, "status": "queued"}


@app.get("/v1/tasks/{task_id}/components/{component_id}/results")
async def list_component_results(task_id: str, component_id: int):
    if await run_in_threadpool(store.get_meta, task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    frontier = await run_in_threadpool(store.load_frontier, task_id, component_id)
    return {
        "task_id": task_id,
        "component_id": component_id,
        "results": [
            {
                "n": int(n),
                "is_feasible": bool(row.get("is_feasible")),
                "is_optimal": bool(row.get("is_optimal", False)),
                "proxy_mass": row.get("proxy_mass"),
                "solve_state": row.get("solve_state"),
            }
            for n, row in frontier.items()
        ],
    }


@app.get("/v1/tasks/{task_id}/components/{component_id}/results/{n}")
async def get_component_result(task_id: str, component_id: int, n: int):
    row = (await run_in_threadpool(store.load_frontier, task_id, component_id)).get(int(n))
    if row is None:
        raise HTTPException(status_code=404, detail="Component result not found")
    return JSONResponse(public_value(row))


@app.get("/v1/tasks/{task_id}/solutions")
async def list_solutions(
    task_id: str,
    total_n: int | None = Query(None),
    source: str | None = Query(None),
    status: str | None = Query(None),
):
    if await run_in_threadpool(store.get_meta, task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    rows = await run_in_threadpool(store.solutions, task_id, total_n, source)
    if status is not None:
        rows = [row for row in rows if str(row.get("status")) == status]
    return {
        "task_id": task_id,
        "solutions": [
            {
                "solution_id": row["solution_id"],
                "source": row.get("source", "components"),
                "total_N": int(row["total_N"]),
                "component_ns": row.get("component_ns", {}),
                "proxy_mass": row.get("proxy_mass"),
                "actual_mass_kg": row.get("actual_mass_kg"),
                "is_feasible": bool(row.get("is_feasible")),
                "status": row.get("status"),
                "result_url": f"/v1/tasks/{task_id}/solutions/{row['solution_id']}",
            }
            for row in rows
        ],
    }


@app.get("/v1/tasks/{task_id}/solutions/{solution_id}")
async def get_solution(task_id: str, solution_id: str):
    row = await run_in_threadpool(store.load_solution, task_id, solution_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Solution not found")
    return JSONResponse(public_value(row))


@app.get("/v1/tasks/{task_id}/component-events")
async def component_events(task_id: str, start: int = 0):
    if await run_in_threadpool(store.get_meta, task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task_id": task_id, "events": await run_in_threadpool(store.all_events, task_id, start)}


# ---------- historical frontend-compatible result API ----------
@app.get("/v1/tasks/{task_id}/results")
async def list_results(task_id: str):
    if await run_in_threadpool(store.get_meta, task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return JSONResponse(to_jsonable(await run_in_threadpool(store.get_result_metas, task_id)))


@app.get("/v1/tasks/{task_id}/results/{n}")
async def get_result(task_id: str, n: int):
    result = await run_in_threadpool(store.get_result, task_id, n)
    if result is None:
        raise HTTPException(status_code=404, detail="Result not found")
    return JSONResponse(to_jsonable(result))


@app.get("/v1/tasks/{task_id}/results/{n}/dxf")
async def get_result_dxf(task_id: str, n: int):
    if await run_in_threadpool(store.get_meta, task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    result = await run_in_threadpool(store.get_result, task_id, n)
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
        content=exported.content,
        media_type="application/dxf",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(exported.filename)},
    )


@app.get("/v1/tasks/{task_id}/events")
async def get_events(task_id: str, after: str = "0-0", count: int = 200):
    if await run_in_threadpool(store.get_meta, task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return await run_in_threadpool(store.read_events, task_id, after, min(max(count, 1), 1000))


@app.post("/v1/tasks/{task_id}/n")
async def add_n(task_id: str, mutation: NMutation):
    ns = mutation.n if isinstance(mutation.n, list) else [mutation.n]
    try:
        plan = await run_in_threadpool(store.add_requested_ns, task_id, ns)
        queued = await run_in_threadpool(workflow.schedule_requested_for_all, task_id, ns)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await run_in_threadpool(store.publish_event, task_id, "n_added", {"n": ns, "components": queued})
    return plan


@app.post("/v1/tasks/{task_id}/cancel")
async def cancel(task_id: str, mutation: CancelMutation):
    meta = await run_in_threadpool(store.get_meta, task_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if mutation.n:
        await run_in_threadpool(store.cancel_ns, task_id, mutation.n)
    else:
        await run_in_threadpool(store.cancel_task, task_id)
    return await run_in_threadpool(store.snapshot, task_id)


@app.post("/v1/tasks/{task_id}/pause")
async def pause_task(task_id: str):
    try:
        await run_in_threadpool(store.set_paused, task_id, True)
        return await run_in_threadpool(store.get_plan, task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc


@app.post("/v1/tasks/{task_id}/resume")
async def resume_task(task_id: str):
    try:
        await run_in_threadpool(store.set_paused, task_id, False)
        return await run_in_threadpool(store.get_plan, task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc


async def _ws_command(task_id: str, raw: dict) -> dict:
    command = WsCommand.model_validate(raw)
    if command.action == "snapshot":
        return {"type": "snapshot", "data": await run_in_threadpool(store.snapshot, task_id)}
    if command.action == "add":
        ns = command.n if isinstance(command.n, list) else [command.n]
        if ns == [None]:
            raise ValueError("Для add требуется n")
        await run_in_threadpool(store.add_requested_ns, task_id, ns)
        queued = await run_in_threadpool(workflow.schedule_requested_for_all, task_id, ns)
        await run_in_threadpool(store.publish_event, task_id, "n_added", {"n": ns, "components": queued})
    elif command.action == "cancel":
        ns = command.n if isinstance(command.n, list) else [command.n]
        if ns == [None]:
            raise ValueError("Для cancel требуется n")
        await run_in_threadpool(store.cancel_ns, task_id, ns)
    elif command.action in {"pause_range", "resume_range"}:
        await run_in_threadpool(store.set_paused, task_id, command.action == "pause_range")
    elif command.action == "cancel_task":
        await run_in_threadpool(store.cancel_task, task_id)
    return {"type": "ack", "action": command.action}


@app.websocket("/v1/tasks/{task_id}/ws")
async def task_websocket(websocket: WebSocket, task_id: str, after: str = "0-0"):
    snapshot = await run_in_threadpool(store.snapshot, task_id)
    if snapshot is None:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    send_lock = asyncio.Lock()
    await websocket.send_json({"type": "snapshot", "data": to_jsonable(snapshot)})
    stream_key = store.task_key(task_id, "events")

    async def sender():
        nonlocal after
        while True:
            rows = await run_in_threadpool(
                lambda: store.redis.xread({stream_key: after}, count=100, block=1000)
            )
            for _, events in rows:
                for event_id, fields in events:
                    after = event_id.decode() if isinstance(event_id, bytes) else str(event_id)
                    body = loads(fields.get(b"json"), {})
                    async with send_lock:
                        await websocket.send_json({"id": after, **to_jsonable(body)})

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
