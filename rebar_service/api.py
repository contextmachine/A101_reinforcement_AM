from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from urllib.parse import quote

from .config import get_settings
from .jsonutil import loads, to_jsonable
from .models import CancelMutation, NMutation, TaskCreate, TaskCreated, TaskParameters, WsCommand
from .pipeline import normalize_input_payload
from .planner import normalize_n_request, validate_n_request_limits, validate_solver_limits
from .store import RedisStore
from .dxf_export import DxfExportError, build_solution_dxf

settings = get_settings()
store = RedisStore(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await run_in_threadpool(store.ping)
    yield


app = FastAPI(title="Rebar Optimizer API", version="1.0.0", lifespan=lifespan)
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
            f"max_concurrent_jobs={requested_limit} превышает серверный лимит "
            f"{settings.max_jobs_per_task}"
        )
    mode, order, n_source = normalize_n_request(parameters.n)
    params = parameters.model_dump(mode="python")
    params.pop("n", None)
    initial_max = max(order)
    solver = dict(params["solver"])
    solver["prepared_max_n"] = max(
        int(solver.get("prepared_max_n") or 0),
        initial_max,
        settings.default_prepared_max_n,
    )
    if solver["prepared_max_n"] > settings.max_n_value:
        raise ValueError(
            f"prepared_max_n={solver['prepared_max_n']} превышает серверный лимит "
            f"{settings.max_n_value}"
        )
    params["solver"] = solver
    params["initial_max_n"] = initial_max
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
        "parameters": params,
        "n_mode": mode,
        "n_source": n_source,
        "max_concurrent_jobs": limit,
    }
    plan = {
        "mode": mode,
        "order": order,
        "cursor": 0,
        "paused": False,
        "exhausted": False,
        "window": max(1, limit * settings.schedule_window_factor),
    }
    store.create_task(task_id, meta, plan, input_obj)
    store.enqueue_job(task_id, "prepare")
    store.publish_event(task_id, "task_created", {"state": meta["state"], "n_mode": mode, "planned": len(order)})
    return TaskCreated(
        task_id=task_id,
        state=meta["state"],
        websocket_url=f"/v1/tasks/{task_id}/ws",
        status_url=f"/v1/tasks/{task_id}",
    )


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
        return await run_in_threadpool(
            _build_task,
            TaskParameters.model_validate(request.model_dump(exclude={"input"})),
            request.input.model_dump(mode="python"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/v1/tasks/upload", response_model=TaskCreated)
async def create_task_upload(
    config: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
):
    try:
        parameters = TaskParameters.model_validate_json(config)
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
        raise HTTPException(
            status_code=404,
            detail="Task not found",
        )

    result = await run_in_threadpool(
        store.get_result,
        task_id,
        n,
    )

    if result is None:
        raise HTTPException(
            status_code=404,
            detail="Result not found",
        )

    try:
        source_input = await run_in_threadpool(
            store.get_object,
            task_id,
            "input",
        )

        exported = await run_in_threadpool(
            lambda: build_solution_dxf(
                source_input,
                result,
                n=n,
            )
        )

    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="Source input not found",
        ) from exc

    except DxfExportError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc

    return Response(
        content=exported.content,
        media_type="application/dxf",
        headers={
            "Content-Disposition":
                "attachment; filename*=UTF-8''"
                + quote(exported.filename)
        },
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
        plan = await run_in_threadpool(store.add_ns, task_id, ns)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await run_in_threadpool(store.publish_event, task_id, "n_added", {"n": ns})
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


def _set_paused(task_id: str, paused: bool) -> dict:
    with store.lock(f"task:{task_id}:plan"):
        plan = store.get_plan(task_id)
        plan["paused"] = bool(paused)
        store.set_plan(task_id, plan)
    if not paused:
        store.refill_task(task_id)
    store.refresh_task_state(task_id)
    store.publish_event(task_id, "range_paused" if paused else "range_resumed", {})
    return plan


@app.post("/v1/tasks/{task_id}/pause")
async def pause_task(task_id: str):
    try:
        return await run_in_threadpool(_set_paused, task_id, True)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc


@app.post("/v1/tasks/{task_id}/resume")
async def resume_task(task_id: str):
    try:
        return await run_in_threadpool(_set_paused, task_id, False)
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
        await run_in_threadpool(store.add_ns, task_id, ns)
        await run_in_threadpool(store.publish_event, task_id, "n_added", {"n": ns})
    elif command.action == "cancel":
        ns = command.n if isinstance(command.n, list) else [command.n]
        if ns == [None]:
            raise ValueError("Для cancel требуется n")
        await run_in_threadpool(store.cancel_ns, task_id, ns)
    elif command.action in {"pause_range", "resume_range"}:
        await run_in_threadpool(_set_paused, task_id, command.action == "pause_range")
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
    client = aioredis.Redis.from_url(settings.redis_url, decode_responses=False)
    stream_key = store.task_key(task_id, "events")

    async def sender():
        nonlocal after
        while True:
            rows = await client.xread({stream_key: after}, count=100, block=1000)
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
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            task.result()
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await client.aclose()
