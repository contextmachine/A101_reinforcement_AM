"""Handlers of the isolated ``/v2/bars`` and ``/v2/verification`` workers.

Both workers run as KEDA ``ScaledJob``s on their own Redis queues (see
``rebar_service.bars_worker`` / ``rebar_service.verification_worker``). A handler owns the
persisted state of its task row; the queue loop only acks the job.
"""
from __future__ import annotations

import traceback
from typing import Any, Mapping

from ..pipeline import analysis_variant
from .bars import layout_zones, physical_polygons
from .verification import reinforcement_rows


def _layout_for(store: Any, row: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    config = dict(row.get("config") or {})
    resolved = list(store.resolved_scene_polygons(
        str(row["scene_id"]),
        variant=analysis_variant(bool(row.get("smooth"))),
        overlay_id=int(row.get("overlay_id") or 0),
    ))
    polygons = physical_polygons(resolved)
    min_step = config.get("min_bar_gap_mm")
    out = layout_zones(
        polygons, list(row.get("zones") or []),
        axis=str(config.get("axis", "y")),
        anchor_factor=float(config.get("anchor_factor", 40.0)),
        steel_density_kg_m3=float(config.get("steel_density_kg_m3", 7850.0)),
        min_step=float(min_step) if min_step else float(store.settings.min_internal_step),
    )
    if not out.get("is_feasible"):
        raise RuntimeError(
            f"раскладка стержней не выполнена ({out.get('status', 'infeasible')}): "
            f"warnings={out.get('warnings')} errors={out.get('errors')}"
        )
    return resolved, out


def handle_bars_job(store: Any, job: Mapping[str, Any], worker_id: str) -> None:
    task_id = str(job["task_id"])
    row = store.v2.get_bar_task(task_id)
    if row is None:
        return
    store.v2.set_bar_task(task_id, state="running")
    try:
        _resolved, out = _layout_for(store, row)
        store.v2.set_bar_task(task_id, state="success", result={
            "bars": out["bars"], "zones": out["zones"], "mass_metrics": out["mass_metrics"],
        })
    except Exception as exc:  # noqa: BLE001 - the task row must record every failure
        traceback.print_exc()
        store.v2.set_bar_task(task_id, state="error", error=f"{type(exc).__name__}: {exc}")
        raise


def handle_verification_job(store: Any, job: Mapping[str, Any], worker_id: str) -> None:
    task_id = str(job["task_id"])
    row = store.v2.get_verification_task(task_id)
    if row is None:
        return
    store.v2.set_verification_task(task_id, state="running")
    try:
        config = dict(row.get("config") or {})
        resolved, out = _layout_for(store, row)
        rows = reinforcement_rows(
            resolved, out["bars"],
            steel_density_kg_m3=float(config.get("steel_density_kg_m3", 7850.0)),
            t_mm=float(config["t"]),
            cover_mm=float(config.get("cover_mm", 30.0)),
        )
        store.v2.set_verification_task(task_id, state="success", result=rows)
    except Exception as exc:  # noqa: BLE001 - the task row must record every failure
        traceback.print_exc()
        store.v2.set_verification_task(task_id, state="error", error=f"{type(exc).__name__}: {exc}")
        raise
