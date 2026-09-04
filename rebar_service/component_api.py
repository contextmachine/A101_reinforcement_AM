from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from .component_models import ComponentNRequest


router = APIRouter(prefix="/v1/tasks", tags=["components"])


def _public(value):
    if hasattr(value, "geom_type"):
        from shapely.geometry import mapping
        return mapping(value)
    if isinstance(value, dict):
        return {str(k): _public(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_public(v) for v in value]
    try:
        import numpy as np
        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, np.ndarray):
            return value.tolist()
    except Exception:
        pass
    return value


def service(request: Request):
    value = getattr(request.app.state, "component_service", None)
    if value is None:
        raise HTTPException(status_code=503, detail="Component workflow is not configured")
    return value


@router.get("/{task_id}/components")
def list_components(task_id: str, request: Request):
    svc = service(request)
    components = svc.store.components(task_id)
    meta = svc.store.task_meta(task_id)
    field = svc.store.load_field(task_id)
    split = field.get("decomposition", {}) if field else {}
    return {
        "task_id": task_id,
        "state": meta.get("state", "unknown"),
        "active_indices": split.get("active_indices", []),
        "background_only_indices": split.get("background_only_indices", []),
        "degenerate_indices": split.get("degenerate_indices", []),
        "components": [row.get("info", row) for row in components if row.get("component", {}).get("id") != -1],
    }


@router.get("/{task_id}/components/{component_id}")
def get_component(task_id: str, component_id: int, request: Request):
    row = service(request).store.load_component(task_id, component_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Component not found")
    return {"task_id": task_id, **row.get("info", row)}


@router.post("/{task_id}/components/{component_id}/n", status_code=202)
def schedule_component_n(task_id: str, component_id: int, body: ComponentNRequest, request: Request):
    svc = service(request)
    try:
        queued = svc.schedule_component_n(task_id, component_id, body.n)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"task_id": task_id, "component_id": component_id, "queued_n": queued, "status": "queued"}


@router.get("/{task_id}/components/{component_id}/results")
def list_component_results(task_id: str, component_id: int, request: Request):
    frontier = service(request).store.load_frontier(task_id, component_id)
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


@router.get("/{task_id}/components/{component_id}/results/{n}")
def get_component_result(task_id: str, component_id: int, n: int, request: Request):
    row = service(request).store.load_frontier(task_id, component_id).get(int(n))
    if row is None:
        raise HTTPException(status_code=404, detail="Component result not found")
    return _public(row)


@router.get("/{task_id}/solutions")
def list_solutions(
    task_id: str,
    request: Request,
    total_n: Optional[int] = Query(None),
    source: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
):
    rows = service(request).store.solutions(task_id, total_n=total_n, source=source)
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
                "result_url": "/v1/tasks/%s/solutions/%s" % (task_id, row["solution_id"]),
            }
            for row in rows
        ],
    }


@router.get("/{task_id}/solutions/{solution_id}")
def get_solution(task_id: str, solution_id: str, request: Request):
    row = service(request).store.load_solution(task_id, solution_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Solution not found")
    return _public(row)


@router.post("/{task_id}/component-pipeline/prepare", status_code=202)
@router.post("/{task_id}/components/prepare", status_code=202)
def prepare_existing_task(task_id: str, request: Request):
    svc = service(request)
    if not svc.store.load_request(task_id):
        raise HTTPException(status_code=409, detail="Original task request is unavailable; recreate the task through run3 API")
    queued = svc.enqueue("prepare_field", task_id)
    return {"task_id": task_id, "queued": queued}


@router.get("/{task_id}/component-events")
def component_events(task_id: str, request: Request, start: int = 0):
    return {"task_id": task_id, "events": service(request).store.events(task_id, start)}
