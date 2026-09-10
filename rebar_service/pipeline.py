from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5

import numpy as np
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from .config import Settings
from .store import Store
from .overlays import normalize_overlay_id
from .layout_metrics import MASS_METRICS_VERSION, augment_layout_mass_metrics
from .planner import edge_to_middle_order, round_robin_unit_plans


WHOLE_COMPONENT_ID = -1
WHOLE_COMPONENT_KEY = "whole"
SOLVER_HARD_MAX_N = 100


def component_storage_id(component_id: int | str) -> int | str:
    """Map the public pseudo-component ``-1`` to the internal whole-field key."""
    if component_id == WHOLE_COMPONENT_KEY:
        return WHOLE_COMPONENT_KEY
    try:
        value = int(component_id)
    except (TypeError, ValueError):
        return component_id
    return WHOLE_COMPONENT_KEY if value == WHOLE_COMPONENT_ID else value


def analysis_variant(smooth: bool = False) -> str:
    return "smooth" if bool(smooth) else "raw"


def variant_is_smooth(variant: str | None) -> bool:
    return str(variant or "raw") == "smooth"


def payload_variant(payload: Mapping[str, Any]) -> str:
    value = str(payload.get("variant") or analysis_variant(bool(payload.get("smooth", False))))
    if value not in {"raw", "smooth"}:
        raise ValueError(f"Unknown analysis variant: {value}")
    return value

class AnalysisNotPreparedError(RuntimeError):
    """Raised when a component-level operation needs an analysis prepared first."""


def payload_overlay_id(payload: Mapping[str, Any]) -> int:
    return normalize_overlay_id(payload.get("overlay_id", 0))


def overlay_polygon_sets(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Split resolved source polygons into demand, physical and removed sets."""
    active: list[dict[str, Any]] = []
    physical: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    background: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        state = str(row.get("overlay_state", "active"))
        if state == "removed":
            removed.append(row)
            continue
        physical.append(row)
        if state == "background_only":
            background.append(row)
        else:
            active.append(row)
    return {
        "active": active,
        "physical": physical,
        "removed": removed,
        "background_only": background,
        "active_indices": [int(row["source_index"]) for row in active],
        "background_only_indices": [int(row["source_index"]) for row in background],
        "removed_indices": [int(row["source_index"]) for row in removed],
    }


def map_components_to_source_indices(
    components: Sequence[dict[str, Any]], source_polygons: Sequence[Mapping[str, Any]], *, area_eps: float = 1e-6
) -> None:
    """Replace derived rectangle indices with stable source-polygon indices for API/overlay use."""
    sources = [
        (int(row["source_index"]), row.get("geometry"))
        for row in source_polygons
        if row.get("geometry") is not None and not row["geometry"].is_empty
    ]
    for component in components:
        demand = component.get("demand_geometry")
        if demand is None:
            demand = component.get("geometry")
        matched: list[int] = []
        if demand is not None and not demand.is_empty:
            for source_index, geometry in sources:
                try:
                    if demand.intersection(geometry).area > area_eps:
                        matched.append(source_index)
                except Exception:
                    continue
        if matched:
            stable = sorted(set(matched))
            component["polygon_indices"] = stable
            component["source_polygon_indices"] = stable



def build_scene_analysis_components(
    demand_polygons: Sequence[Mapping[str, Any]],
    stable_components: Sequence[Mapping[str, Any]],
    *,
    load2cls: Mapping[Any, int],
    axis: str,
    holds: Mapping[Any, float] | None = None,
) -> list[dict[str, Any]]:
    """Build analysis components using immutable scene component membership.

    Overlay edits may remove demand rows, but they never change a scene
    component id.  Empty components simply have no solver job for that task.
    """
    from A101.reinforcement_components import _load_class

    rows_by_index = {int(row["source_index"]): dict(row) for row in demand_polygons}
    class_holds = {int(k): float(v) for k, v in dict(holds or {}).items()}
    result: list[dict[str, Any]] = []
    for raw_component in stable_components:
        cid = int(raw_component.get("id", raw_component.get("component_id", 0)))
        wanted = [int(x) for x in raw_component.get("polygon_indices", [])]
        selected = [rows_by_index[i] for i in wanted if i in rows_by_index]
        if not selected:
            continue
        polygons: list[dict[str, Any]] = []
        classes: set[int] = set()
        loads: set[float] = set()
        for row in selected:
            cls = int(_load_class(load2cls, row["load"]))
            if cls <= 0:
                continue
            item = dict(row)
            item["class"] = cls
            polygons.append(item)
            classes.add(cls)
            loads.add(float(row["load"]))
        if not polygons:
            continue
        demand = unary_union([row["geometry"] for row in polygons])
        max_hold = max((class_holds.get(cls, 0.0) for cls in classes), default=0.0)
        result.append(
            {
                "id": cid,
                "axis": axis,
                "polygon_indices": [int(row["source_index"]) for row in polygons],
                "polygons": polygons,
                "geometry": demand,
                "demand_geometry": demand,
                "bounds": tuple(map(float, demand.bounds)),
                "demand_bounds": tuple(map(float, demand.bounds)),
                "classes": sorted(classes),
                "loads": sorted(loads),
                "max_hold": float(max_hold),
                "expanded_polygons": [],
            }
        )
    return result


class JobKind(str, Enum):
    materialize_scene = "materialize_scene"
    materialize_source = "materialize_source"
    prepare_field = "prepare_field"
    prepare_component = "prepare_component"
    compute_max_n_component = "compute_max_n_component"
    solve_component = "solve_component"
    fit_component = "fit_component"
    combine_frontiers = "combine_frontiers"
    layout_solution = "layout_solution"
    validate_solution = "validate_solution"
    prepare_whole = "prepare_whole"
    compute_max_n_whole = "compute_max_n_whole"
    solve_whole = "solve_whole"
    fit_whole = "fit_whole"


def stable_digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PipelineJob:
    kind: str
    task_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    generation: int = 0
    dedupe_key: str | None = None
    job_id: str | None = None
    created_at: float | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        kind = self.kind.value if isinstance(self.kind, JobKind) else str(self.kind)
        object.__setattr__(self, "kind", kind)
        dedupe = self.dedupe_key or self.default_dedupe_key()
        object.__setattr__(self, "dedupe_key", dedupe)
        object.__setattr__(self, "job_id", self.job_id or uuid5(NAMESPACE_URL, dedupe).hex)
        object.__setattr__(self, "created_at", float(self.created_at or time.time()))

    def default_dedupe_key(self) -> str:
        coordinate = {
            key: self.payload.get(key)
            for key in ("component_id", "n", "total_n", "solution_id", "source", "frontier_version", "offset", "variant", "overlay_id",
                        "candidate_id", "layout_refresh", "relayout_solution_id", "metrics_version")
            if key in self.payload
        }
        if not coordinate:
            coordinate = {"payload": stable_digest(self.payload)}
        return f"{self.kind}:{self.task_id}:{int(self.generation)}:{stable_digest(coordinate)}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_value(cls, value: Mapping[str, Any]) -> "PipelineJob":
        row = dict(value)
        # Compatibility with very early jobs that stored n at the top level.
        payload = dict(row.get("payload") or {})
        if row.get("n") is not None and "n" not in payload:
            payload["n"] = row["n"]
        row["payload"] = payload
        allowed = {"kind", "task_id", "payload", "generation", "dedupe_key", "job_id", "created_at", "schema_version"}
        return cls(**{key: row[key] for key in allowed if key in row})


def normalize_input_payload(payload: Any) -> dict[str, Any]:
    """Accept the public polygon schema and the historical ``[[points, load], ...]`` JSON."""
    if isinstance(payload, list):
        payload = {"kind": "polygons", "units": "mm", "polygons": payload}
    if not isinstance(payload, Mapping):
        raise ValueError("Вход должен быть JSON-объектом или списком полигонов")
    out = dict(payload)
    if "kind" not in out and "polygons" in out:
        out["kind"] = "polygons"
    if out.get("kind") != "polygons":
        return out
    out.setdefault("units", "mm")
    normalized = []
    for i, item in enumerate(out.get("polygons", [])):
        if isinstance(item, Mapping):
            points, load = item.get("points"), item.get("load")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            points, load = item
        else:
            raise ValueError(f"Некорректный polygon #{i}")
        normalized.append({"points": points, "load": load})
    out["polygons"] = normalized
    return out


def polygons_from_input(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    payload = normalize_input_payload(payload)
    kind = payload.get("kind")
    if kind == "dxf":
        from A101.read_dxf import extract_polygons

        suffix = Path(str(payload.get("filename", "input.dxf"))).suffix or ".dxf"
        fd, path = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload["content"])
            return extract_polygons(path)
        finally:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
    if kind != "polygons":
        raise ValueError("input.kind должен быть polygons или dxf")
    scale = 1000.0 if payload.get("units", "mm") == "m" else 1.0
    out = []
    for i, item in enumerate(payload.get("polygons", [])):
        points = np.asarray(item["points"], dtype=float) * scale
        geometry = Polygon(points)
        if not geometry.is_valid:
            geometry = geometry.buffer(0)
        if geometry.is_empty or geometry.area <= 0:
            raise ValueError(f"Некорректный полигон #{i}")
        out.append({"points": points, "geometry": geometry, "load": float(item["load"])})
    if not out:
        raise ValueError("Не переданы полигоны")
    return out


def choose_component_ns(requested: Sequence[int], max_useful_n: int, hard: bool = False) -> tuple[list[int], bool]:
    max_n = max(1, int(max_useful_n))
    if hard:
        return list(range(1, max_n + 1)), False
    values = list(dict.fromkeys(int(n) for n in requested if 0 < int(n) <= max_n))
    return (values, False) if values else ([1], True)


def component_public_info(component: Mapping[str, Any], max_useful_n: int | None = None, state: str = "created") -> dict[str, Any]:
    return {
        "id": int(component.get("id", 0)),
        "polygon_indices": list(map(int, component.get("polygon_indices", []))),
        "classes": list(map(int, component.get("classes", []))),
        "loads": list(map(float, component.get("loads", []))),
        "bounds": list(map(float, component.get("bounds", ()))) or None,
        "demand_bounds": list(map(float, component.get("demand_bounds", ()))) or None,
        "max_useful_n": None if max_useful_n is None else int(max_useful_n),
        "prepared": max_useful_n is not None,
        "state": state,
    }


def public_value(value: Any) -> Any:
    if hasattr(value, "geom_type"):
        from shapely.geometry import mapping

        return mapping(value)
    if isinstance(value, Mapping):
        return {str(k): public_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [public_value(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    return value



def _compatibility_zones(layout: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    fit_zones: list[dict[str, Any]] = []
    summary_zones: list[dict[str, Any]] = []
    for zone in layout.get("zones", []) or []:
        if zone.get("background"):
            continue
        anchored_bounds = tuple(map(float, zone.get("final_rectangle_with_anchorage") or zone.get("bounds") or zone.get("primary_bounds") or ()))
        if len(anchored_bounds) != 4:
            continue
        no_anchor_bounds = tuple(map(float, zone.get("final_rectangle_without_anchorage") or zone.get("fitted_bounds") or zone.get("primary_bounds") or anchored_bounds))
        no_anchor_unclipped = tuple(map(float, zone.get("final_rectangle_without_anchorage_unclipped") or no_anchor_bounds))
        anchored_unclipped = tuple(map(float, zone.get("final_rectangle_with_anchorage_unclipped") or zone.get("anchored_bounds_unclipped") or anchored_bounds))
        primary = tuple(map(float, zone.get("primary_bounds") or no_anchor_bounds))
        cls = int(zone["class"]) if zone.get("class") is not None else None
        bars = [tuple(map(float, row[:4])) for row in zone.get("bars_without_anchorage", zone.get("bars", [])) or []]
        anchored_bars = [tuple(map(float, row[:4])) for row in zone.get("bars_with_anchorage", zone.get("bars", [])) or []]
        bars_unclipped = [tuple(map(float, row[:4])) for row in zone.get("bars_without_anchorage_unclipped", bars) or []]
        anchored_bars_unclipped = [tuple(map(float, row[:4])) for row in zone.get("bars_with_anchorage_unclipped", anchored_bars) or []]
        fit_zones.append({
            "id": zone.get("id"), "class": cls, "bounds": no_anchor_bounds,
            "diameter": zone.get("diameter"), "step": zone.get("step"), "bars": bars,
        })
        summary_zones.append({
            "class": cls, "diameter": zone.get("diameter"), "step": zone.get("step"),
            "primary rectangle": primary,
            "final rectangle": no_anchor_bounds,
            "final rectangle with anchorage": anchored_bounds,
            "final rectangle unclipped": no_anchor_unclipped,
            "final rectangle with anchorage unclipped": anchored_unclipped,
            "bars": bars, "bars with anchorage": anchored_bars,
            "bars unclipped": bars_unclipped, "bars with anchorage unclipped": anchored_bars_unclipped,
            "zone mass without anchorage": zone.get("zone_mass_without_anchorage_kg"),
            "zone mass with anchorage": zone.get("zone_mass_with_anchorage_kg"),
            "zone mass without anchorage unclipped": zone.get("zone_mass_without_anchorage_unclipped_kg"),
            "zone mass with anchorage unclipped": zone.get("zone_mass_with_anchorage_unclipped_kg"),
            "anchorage mass": None if zone.get("zone_mass_without_anchorage_kg") is None else float(zone.get("zone_mass_with_anchorage_kg", 0.0)) - float(zone.get("zone_mass_without_anchorage_kg", 0.0)),
            "anchorage mass unclipped": None if zone.get("zone_mass_without_anchorage_unclipped_kg") is None else float(zone.get("zone_mass_with_anchorage_unclipped_kg", 0.0)) - float(zone.get("zone_mass_without_anchorage_unclipped_kg", 0.0)),
            "width": abs(anchored_bounds[2] - anchored_bounds[0]),
            "length": abs(anchored_bounds[3] - anchored_bounds[1]),
        })
    return fit_zones, summary_zones


def to_compat_result(solution: Mapping[str, Any]) -> dict[str, Any]:
    """Return the historical result shape, backed by the current component solution."""
    row = dict(solution)
    rectangles = list(row.get("rectangles", []) or [])
    anchored = list(row.get("anchored_boxes", []) or [])
    layout = dict(row.get("bar_layout", {}) or {})
    fit_zones, summary_zones = _compatibility_zones(layout)
    actual_mass = row.get("actual_mass_kg")
    proxy_mass = row.get("proxy_mass")
    mass_metrics = dict(row.get("mass_metrics") or layout.get("mass_metrics") or {})
    feasible = bool(row.get("is_feasible"))
    total_n = int(row.get("total_N", 0))
    return {
        "is_feasible": feasible,
        "is_optimal": bool(row.get("is_optimal", False)),
        "status": row.get("status", "feasible" if feasible else "infeasible"),
        "total_cost": proxy_mass,
        "solver_result": {
            "is_feasible": feasible,
            "is_optimal": bool(row.get("is_optimal", False)),
            "status": row.get("status", "feasible" if feasible else "infeasible"),
            "total_cost": proxy_mass,
            "component_ns": dict(row.get("component_ns", {}) or {}),
        },
        "primary_rectangles": rectangles or anchored,
        "fit_result": {
            "is_feasible": feasible,
            "is_optimal": bool(row.get("is_optimal", False)),
            "rectangles": rectangles,
            "zones": fit_zones,
        },
        "summary": {
            "mass": actual_mass,
            "mass_kg": actual_mass,
            "mass_metrics_version": mass_metrics.get("schema_version"),
            "mass_metrics_scope": mass_metrics.get("unclipped_scope"),
            "mass without anchorage": mass_metrics.get("without_anchorage_kg"),
            "mass with anchorage": mass_metrics.get("with_anchorage_kg", actual_mass),
            "mass without anchorage unclipped": mass_metrics.get("without_anchorage_unclipped_kg"),
            "mass with anchorage unclipped": mass_metrics.get("with_anchorage_unclipped_kg"),
            "anchorage mass": mass_metrics.get("anchorage_kg"),
            "anchorage mass unclipped": mass_metrics.get("anchorage_unclipped_kg"),
            "proxy_mass": proxy_mass,
            "N": total_n,
            "zones": summary_zones,
        },
        "solution_id": row.get("solution_id"),
        "source": row.get("source", "components"),
        "total_N": total_n,
        "component_ns": dict(row.get("component_ns", {}) or {}),
        "proxy_mass": proxy_mass,
        "actual_mass_kg": actual_mass,
        "compact_zones": list(row.get("compact_zones", []) or []),
    }


class PipelineWorkflow:
    """The only production calculation workflow used by the API and workers."""

    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        self._overlay_context: ContextVar[int] = ContextVar(f"pipeline_overlay_{id(self)}", default=0)

    def _publish(self, task_id: str, event: Mapping[str, Any]) -> str:
        row = dict(event)
        event_type = str(row.pop("type"))
        return self.store.publish_event(task_id, event_type, row)

    @staticmethod
    def _variant_call(method, *args, variant: str = "raw"):
        """Keep raw calls compatible with historical store/test doubles."""
        return method(*args) if variant == "raw" else method(*args, variant=variant)

    def _requested_ns(self, task_id: str, variant: str) -> list[int]:
        method = getattr(self.store, "requested_ns", None)
        if callable(method):
            try:
                values = method(task_id, variant=variant)
            except TypeError:
                values = method(task_id)
            if values:
                return [int(n) for n in values]
        meta = self.store.get_meta(task_id) or {}
        return [int(n) for n in meta.get("requested_n", [1])]

    def _is_n_cancelled(self, task_id: str, n: int, variant: str) -> bool:
        method = getattr(self.store, "is_n_cancelled", None)
        if not callable(method):
            return False
        try:
            return bool(method(task_id, int(n), variant=variant))
        except TypeError:
            return bool(method(task_id, int(n)))

    def _persisted_variant_polygons(
        self, task_id: str, variant: str, input_obj: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        method = getattr(self.store, "load_variant_polygons", None)
        if callable(method):
            materialize = getattr(self.store, "ensure_polygon_variants", None)
            if callable(materialize):
                materialize(task_id)
            return list(method(task_id, variant=variant))
        # Compatibility path for small historical test doubles only. Production Store always persists variants.
        if variant != "raw":
            raise AttributeError("store does not expose persisted smooth polygons")
        rows = polygons_from_input(input_obj)
        return [
            {
                "points": [[float(x), float(y)] for x, y in row["points"]],
                "load": float(row["load"]),
                **({"color": int(row["color"])} if row.get("color") is not None else {}),
            }
            for row in rows
        ]

    def enqueue(
        self,
        kind: JobKind | str,
        task_id: str,
        payload: Mapping[str, Any] | None = None,
        *,
        generation: int | None = None,
        dedupe_key: str | None = None,
    ) -> bool:
        if self.store.pending_jobs(task_id) >= int(self.settings.max_jobs_per_task):
            raise RuntimeError(f"REBAR_MAX_JOBS_PER_TASK exceeded for task {task_id}")
        generation = self.store.generation(task_id) if generation is None else int(generation)
        job = PipelineJob(
            kind.value if isinstance(kind, JobKind) else str(kind),
            task_id,
            dict(payload or {}),
            generation=generation,
            dedupe_key=dedupe_key,
        )
        return self.store.enqueue_pipeline_job(job.to_dict())

    def enqueue_scene_materialization(self, scene_id: str) -> bool:
        """Queue scene materialization without requiring a task row."""
        job = PipelineJob(
            JobKind.materialize_scene.value,
            str(scene_id),
            {"scene_id": str(scene_id)},
            generation=0,
            dedupe_key=f"materialize-scene:{scene_id}",
        )
        return self.store.enqueue_pipeline_job(job.to_dict())

    def handle_materialize_scene(self, job: PipelineJob) -> None:
        scene_id = str(job.payload.get("scene_id") or job.task_id)
        materialize = getattr(self.store, "ensure_scene_variants", None)
        if not callable(materialize):
            raise RuntimeError("store does not support scene materialization")
        materialize(scene_id)

    def materialize_task_source(self, task_id: str, *, continue_pipeline: bool, smooth: bool = False) -> bool:
        return self.enqueue(
            JobKind.materialize_source,
            task_id,
            {"continue_pipeline": bool(continue_pipeline), "smooth": bool(smooth), "variant": analysis_variant(smooth)},
        )

    def handle_materialize_source(self, job: PipelineJob) -> None:
        meta = self.store.get_meta(job.task_id) or {}
        scene_id = str(meta.get("scene_id") or job.task_id)
        scene_materialize = getattr(self.store, "ensure_scene_variants", None)
        sync = getattr(self.store, "sync_task_variants_from_scene", None)
        if callable(scene_materialize) and callable(sync):
            changed = bool(scene_materialize(scene_id))
            sync(job.task_id, scene_id)
        else:
            materialize = getattr(self.store, "ensure_polygon_variants", None)
            if not callable(materialize):
                raise RuntimeError("store does not support source materialization")
            changed = bool(materialize(job.task_id))
        smooth = bool(job.payload.get("smooth", False))
        variant = analysis_variant(smooth)
        self._publish(job.task_id, {
            "type": "source_materialized", "changed": changed, "variant": variant, "smooth": smooth,
        })
        if bool(job.payload.get("continue_pipeline", False)):
            current = self.store.get_meta(job.task_id) or meta
            context_variant = str(current.get("analysis_variant") or variant)
            context_smooth = context_variant == "smooth"
            context_overlay = int(current.get("analysis_overlay_id", 0) or 0)
            self.prepare_task(
                job.task_id, auto_solve=True, smooth=context_smooth, overlay_id=context_overlay
            )

    def prepare_task(
        self,
        task_id: str,
        *,
        auto_solve: bool | None = None,
        smooth: bool = False,
    ) -> bool:
        meta = self.store.get_meta(task_id)
        if meta is None:
            raise KeyError(task_id)
        if auto_solve is None:
            auto_solve = not bool(meta.get("manual_mode", False))
        variant = analysis_variant(smooth)
        self.store.patch_meta(task_id, state="queued_preparation", generation=self.store.generation(task_id))
        self._publish(
            task_id,
            {
                "type": "pipeline_queued",
                "requested_n": self._requested_ns(task_id, variant),
                "scan_mode": meta.get("scan_mode", "requested"),
                "whole": bool(meta.get("whole", False)),
                "auto_solve": bool(auto_solve),
                "variant": variant,
                "smooth": bool(smooth),
            },
        )
        return self.enqueue(
            JobKind.prepare_field,
            task_id,
            {"auto_solve": bool(auto_solve), "variant": variant, "smooth": bool(smooth)},
        )

    def bootstrap_task(self, task_id: str, *, smooth: bool = False) -> bool:
        return self.prepare_task(task_id, auto_solve=True, smooth=smooth)

    def dispatch(self, job: PipelineJob) -> None:
        if str(job.kind) != JobKind.materialize_scene.value:
            if int(job.generation) != self.store.generation(job.task_id):
                return
        handler = getattr(self, "handle_" + str(job.kind), None)
        if handler is None:
            raise ValueError(f"Неизвестный job kind: {job.kind}")
        handler(job)

    def _params(self, task_id: str) -> dict[str, Any]:
        meta = self.store.get_meta(task_id)
        if meta is None:
            raise KeyError(task_id)
        return dict(meta.get("parameters", {}) or {})

    def _solver(self, task_id: str) -> dict[str, Any]:
        return dict(self._params(task_id).get("solver", {}) or {})

    def _field(self, task_id: str, variant: str = "raw") -> dict[str, Any]:
        field = self._variant_call(self.store.load_field, task_id, variant=variant)
        if not field:
            raise KeyError(f"field variant={variant} не подготовлен для task {task_id}")
        return field

    def handle_prepare_field(self, job: PipelineJob) -> None:
        from A101.axis_orientation import class_holds, normalize_axis
        from A101.calculate_mass import resolve_rebar_config
        from A101.grid_work import clean_poly
        from A101.poly_bbox import rect_polygons
        from A101.reinforcement_components import split_reinforcement_components

        task_id = job.task_id
        variant = payload_variant(job.payload)
        smooth = variant_is_smooth(variant)
        auto_solve = bool(job.payload.get("auto_solve", True))
        self.store.patch_meta(task_id, state="preparing_components")
        self._publish(task_id, {"type": "component_prepare_started", "variant": variant, "smooth": smooth})
        input_obj = self.store.get_object(task_id, "input")
        params = self._params(task_id)
        persisted_polygons = self._persisted_variant_polygons(task_id, variant, input_obj)
        polygons = polygons_from_input({"kind": "polygons", "units": "mm", "polygons": persisted_polygons})

        cfg = resolve_rebar_config(
            polygons,
            back_grid=params.get("back_grid"),
            stock=params.get("stock"),
            max_layers=params.get("max_layers"),
        )
        axis = normalize_axis(str(params.get("axis", "y")))
        cfg["axis"] = axis
        anchor_factor = float(params.get("anchor_factor", 40.0))
        base_holds, holds, leaves = class_holds(cfg["diameters"], cfg.get("recipes"), anchor_factor)
        cfg.update(base_holds=base_holds, holds=holds, recipe_leaves=leaves)

        meta_before = self.store.get_meta(task_id) or {}
        effective = dict(meta_before.get("effective_rebar_config", {}) or {})
        effective[variant] = {
            "back_grid": tuple(cfg["back_grid"]),
            "stock": [tuple(row) for row in cfg["stock"]],
            "max_layers": int(cfg["max_layers"]),
            "source": cfg.get("rebar_config_source"),
        }
        self.store.patch_meta(task_id, effective_rebar_config=effective)

        ortho = clean_poly(rect_polygons(polygons))
        split = split_reinforcement_components(
            ortho,
            load2cls=cfg["load2cls"],
            recipes=cfg.get("recipes"),
            diameters=cfg["diameters"],
            anchor_factor=anchor_factor,
            axis=axis,
        )
        field_geometry = unary_union(
            [p["geometry"] for p in polygons if p.get("geometry") is not None and not p["geometry"].is_empty]
        )
        field = {
            "filename": str(input_obj.get("filename", "input.dxf")) if isinstance(input_obj, Mapping) else "input.dxf",
            "start_polygons": polygons,
            "ortho_polygons": ortho,
            "cfg": cfg,
            "decomposition": split,
            "field_geometry": field_geometry,
            "variant": variant,
            "smooth": smooth,
        }
        self._variant_call(self.store.save_field, task_id, field, variant=variant)
        for component in split.get("components", []):
            cid = int(component["id"])
            self._variant_call(
                self.store.save_component, task_id, cid,
                {"component": component, "info": component_public_info(component), "state": "queued", "variant": variant},
                variant=variant,
            )
            prepare_payload = {"component_id": cid, "auto_solve": auto_solve}
            if variant != "raw":
                prepare_payload.update(variant=variant, smooth=True)
            self.enqueue(JobKind.prepare_component, task_id, prepare_payload)
        meta = self.store.get_meta(task_id) or {}
        if (not auto_solve) or bool(meta.get("whole")):
            whole_payload = {"auto_solve": auto_solve}
            if variant != "raw":
                whole_payload.update(variant=variant, smooth=True)
            self.enqueue(JobKind.prepare_whole, task_id, whole_payload)
        self.store.patch_meta(task_id, state="components_ready")
        self._publish(
            task_id,
            {
                "type": "components_ready",
                "components": [component_public_info(c) for c in split.get("components", [])],
                "active_indices": split.get("active_indices", []),
                "background_only_indices": split.get("background_only_indices", []),
                "degenerate_indices": split.get("degenerate_indices", []),
                "variant": variant,
                "smooth": smooth,
            },
        )

    def _prepare_problem(
        self, task_id: str, component_id: Any, component: Mapping[str, Any], *, variant: str = "raw"
    ) -> dict[str, Any]:
        from A101.reinforcement_components import component_n_bounds, prepare_component_problem

        field = self._field(task_id, variant)
        cfg = field["cfg"]
        params = self._params(task_id)
        solver = self._solver(task_id)
        prepared_max_n = solver.get("prepared_max_n")
        if prepared_max_n in (None, 0, "0"):
            prepared_max_n = SOLVER_HARD_MAX_N
        else:
            prepared_max_n = min(int(prepared_max_n), SOLVER_HARD_MAX_N)
        prepared_max_n = min(int(prepared_max_n), int(self.settings.max_n_value))
        problem = prepare_component_problem(
            component,
            load2cls=cfg["load2cls"],
            recipes=cfg.get("recipes"),
            densities=cfg["densities"],
            diameters=cfg["diameters"],
            anchor_factor=float(params.get("anchor_factor", 40.0)),
            axis=cfg["axis"],
            min_width=float(params.get("min_width_mm", 1000.0)),
            grid_size=float(self.settings.grid_size),
            fill_notches_threshold=float(self.settings.fill_notches),
            short_edge_threshold=float(self.settings.short_edge),
            simplify_steps_threshold=float(self.settings.simplify_step),
            max_n=prepared_max_n,
            use_mosaic=bool(self.settings.use_mosaic),
            preserve_demand_classes=True,
            strict_grid_coverage=True,
            refine_unrepresentable_cells=False,
            refine_mixed_cells=False,
            physical_geometry=field.get("field_geometry"),
        )
        bounds = component_n_bounds(problem["prepared"], cap=prepared_max_n)
        max_useful = max(1, int(bounds["nonredundant_upper_bound"]))
        stored = {"problem": problem, "bounds": bounds, "max_useful_n": max_useful, "variant": variant}
        self._variant_call(self.store.save_problem, task_id, component_id, stored, variant=variant)
        return stored

    def _schedule_plan(
        self,
        task_id: str,
        component_id: Any,
        plan: Sequence[int],
        force_single: bool,
        start: int = 0,
        whole: bool = False,
        *,
        variant: str = "raw",
    ) -> None:
        batch = max(1, int(self.settings.scheduler_batch_size))
        end = min(len(plan), int(start) + batch)
        kind = JobKind.solve_whole if whole else JobKind.solve_component
        smooth = variant_is_smooth(variant)
        for n in plan[int(start) : end]:
            if self._is_n_cancelled(task_id, int(n), variant):
                continue
            self.enqueue(
                kind,
                task_id,
                {
                    "component_id": component_id,
                    "n": int(n),
                    "force_single_box": bool(force_single and int(n) == 1),
                    "source": "whole" if whole else "components",
                    "variant": variant,
                    "smooth": smooth,
                },
            )
        if end < len(plan):
            self.enqueue(
                JobKind.prepare_whole if whole else JobKind.prepare_component,
                task_id,
                {"component_id": component_id, "schedule_only": True, "start": end, "variant": variant, "smooth": smooth},
                dedupe_key=f"schedule:{task_id}:{variant}:{component_id}:{end}:{self.store.generation(task_id)}",
            )

    def _record_cover_infeasible(self, job: PipelineJob, cid: Any, record: Mapping[str, Any], error: Exception) -> None:
        variant = payload_variant(job.payload)
        row = dict(record)
        result = {"feasible": False, "reason": "candidate_cover", "detail": str(error), "max_useful_n": None}
        row.update(state="max_n_infeasible", max_n_state="infeasible", max_n_milp=result,
                   max_useful_n=None, plan=[], force_single_box=False)
        self._variant_call(self.store.save_component, job.task_id, cid, row, variant=variant)
        self._publish(job.task_id, {"type": "component_infeasible", "component_id": cid,
                      "variant": variant, "reason": "candidate_cover", "detail": str(error)})
        self._maybe_complete_analysis(job.task_id, variant, bool(job.payload.get("analysis_auto_solve", True)))

    def handle_prepare_component(self, job: PipelineJob) -> None:
        task_id, cid = job.task_id, int(job.payload["component_id"])
        variant = payload_variant(job.payload)
        record = self._variant_call(self.store.load_component, task_id, cid, variant=variant)
        if record is None:
            raise KeyError(f"component {cid} variant={variant} not found")

        # Jobs already queued by the pre-max-N scheduler can survive a rolling
        # upgrade. Preserve their old schedule-only behavior.
        if job.payload.get("schedule_only"):
            self._schedule_plan(
                task_id, cid, list(record.get("plan", [])), bool(record.get("force_single_box")),
                int(job.payload.get("start", 0)), variant=variant,
            )
            return

        from A101.reinforcement_components import CandidateCoverInfeasible
        try:
            prepared = (
                self._prepare_problem(task_id, cid, record["component"])
                if variant == "raw"
                else self._prepare_problem(task_id, cid, record["component"], variant=variant)
            )
        except CandidateCoverInfeasible as exc:
            self._record_cover_infeasible(job, cid, record, exc)
            return

        # Compatibility path for the explicit/manual component API and for
        # jobs queued before the two-phase max-N scheduler existed. New scene
        # analyses always carry analysis_auto_solve in their prepare payload.
        if "analysis_auto_solve" not in job.payload:
            meta = self.store.get_meta(task_id) or {}
            plan, force_single = choose_component_ns(
                self._requested_ns(task_id, variant) or [1],
                int(prepared["max_useful_n"]),
                str(meta.get("scan_mode")) == "hard",
            )
            record.update(
                state="prepared", plan=plan, force_single_box=force_single,
                max_useful_n=int(prepared["max_useful_n"]), bounds=prepared["bounds"],
                info=component_public_info(record["component"], int(prepared["max_useful_n"]), "prepared"),
                variant=variant, smooth=variant_is_smooth(variant),
            )
            self._variant_call(self.store.save_component, task_id, cid, record, variant=variant)
            self._publish(task_id, {
                "type": "component_prepared", "component_id": cid,
                "max_useful_n": int(prepared["max_useful_n"]), "planned_n": plan,
                "fallback_single_box": force_single, "variant": variant,
                "smooth": variant_is_smooth(variant),
            })
            if bool(job.payload.get("auto_solve", True)):
                self._schedule_plan(task_id, cid, plan, force_single, variant=variant)
            return

        record.update(
            state="max_n_queued", plan=[], force_single_box=False,
            max_useful_n=None, max_n_state="queued", bounds=prepared["bounds"],
            info=component_public_info(record["component"], None, "max_n_queued"),
            variant=variant, smooth=variant_is_smooth(variant),
        )
        self._variant_call(self.store.save_component, task_id, cid, record, variant=variant)
        self._publish(task_id, {
            "type": "component_problem_prepared", "component_id": cid,
            "variant": variant, "smooth": variant_is_smooth(variant),
        })
        self.enqueue(
            JobKind.compute_max_n_component, task_id,
            {
                "component_id": cid, "variant": variant, "smooth": variant_is_smooth(variant),
                "analysis_auto_solve": bool(job.payload.get("analysis_auto_solve", True)),
            },
        )

    def _compute_exact_max_n(
        self, task_id: str, component_id: Any, *, variant: str = "raw"
    ) -> dict[str, Any]:
        from A101.max_n_milp import estimate_max_useful_n

        stored = self._variant_call(self.store.load_problem, task_id, component_id, variant=variant)
        if not stored:
            raise KeyError(f"problem missing for component={component_id}")
        problem = stored["problem"]
        field = self._field(task_id, variant)
        cfg = dict(field.get("cfg", {}) or {})
        cap = min(SOLVER_HARD_MAX_N, int(self.settings.max_n_value))
        configured = self._solver(task_id).get("prepared_max_n")
        if configured is not None:
            cap = min(cap, int(configured))
        return estimate_max_useful_n(problem["work_matrix"], recipes=cfg.get("recipes"), hard_cap=cap)

    def handle_compute_max_n_component(self, job: PipelineJob) -> None:
        task_id, cid = job.task_id, int(job.payload["component_id"])
        variant = payload_variant(job.payload)
        result = self._compute_exact_max_n(task_id, cid, variant=variant)
        record = self._variant_call(self.store.load_component, task_id, cid, variant=variant) or {}
        feasible = bool(result.get("feasible")) and int(result.get("max_useful_n", 0) or 0) > 0
        max_n = int(result.get("max_useful_n")) if feasible else None
        requested = self._requested_ns(task_id, variant, self._current_overlay_id())
        plan = edge_to_middle_order(
            n for n in requested if max_n is not None and 1 <= int(n) <= min(max_n, SOLVER_HARD_MAX_N)
        )
        result = {**dict(result), "max_useful_n": max_n}
        record.update(
            state="prepared" if feasible else "max_n_infeasible",
            max_n_state="ready" if feasible else "infeasible",
            max_useful_n=max_n, max_n_milp=result, plan=plan, force_single_box=False,
            info=component_public_info(record.get("component", {}), max_n, "prepared" if feasible else "infeasible"),
            variant=variant, smooth=variant_is_smooth(variant),
        )
        self._variant_call(self.store.save_component, task_id, cid, record, variant=variant)
        self._publish(task_id, {
            "type": "component_max_n_ready", "component_id": cid, "max_useful_n": max_n,
            "feasible": feasible, "planned_n": plan, "variant": variant,
            "smooth": variant_is_smooth(variant),
        })
        analysis_auto_solve = bool(job.payload.get("analysis_auto_solve", True))
        if feasible and analysis_auto_solve and plan:
            self._schedule_plan(task_id, cid, plan, False, whole=False, variant=variant)

        # Direct whole-field max-N for scene tasks is the sum of real-component
        # maxima. If the whole problem was prepared before this component bound
        # became ready, let the last arriving component wake/finalize it.
        meta = self.store.get_meta(task_id) or {}
        scene_task = bool(meta.get("scene_id") and str(meta.get("scene_id")) != str(task_id))
        selection = [int(x) for x in (meta.get("component_selection") or ([-2] if meta.get("whole") else [-3]))]
        if scene_task and selection in ([-1], [-2]):
            all_real = self.real_component_ids(
                task_id, variant=variant, overlay_id=self._current_overlay_id()
            )
            if len(all_real) > 1:
                whole_record = self._variant_call(
                    self.store.load_component, task_id, WHOLE_COMPONENT_KEY,
                    variant=variant, overlay_id=self._current_overlay_id(),
                ) or {}
                if str(whole_record.get("max_n_state", "")) in {"queued", "waiting_components"}:
                    whole_auto_solve = bool(whole_record.get("analysis_auto_solve", not bool(meta.get("manual_mode", False))))
                    self.handle_compute_max_n_whole(PipelineJob(
                        JobKind.compute_max_n_whole.value,
                        task_id,
                        {
                            "component_id": WHOLE_COMPONENT_KEY,
                            "variant": variant,
                            "smooth": variant_is_smooth(variant),
                            "analysis_auto_solve": whole_auto_solve,
                            "overlay_id": self._current_overlay_id(),
                        },
                        generation=self.store.generation(task_id),
                    ))
        self._maybe_complete_analysis(task_id, variant, analysis_auto_solve)

    def _single_component_frontier(
        self, task_id: str, component_id: Any, source: str = "components", *, variant: str = "raw"
    ) -> dict[str, Any]:
        from A101.axis_orientation import add_box_anchorage

        field = self._field(task_id, variant)
        cfg = field["cfg"]
        params = self._params(task_id)
        stored = self._variant_call(self.store.load_problem, task_id, component_id, variant=variant)
        if not stored:
            raise KeyError("component problem missing")
        problem = stored["problem"]
        component = problem.get("component", {})
        bounds = tuple(map(float, component.get("demand_bounds") or component.get("bounds") or component["geometry"].bounds))
        classes = [int(x) for x in component.get("classes", []) if int(x) > 0]
        if not classes:
            classes = [
                int(row.get("class", 0))
                for row in component.get("polygons", [])
                if isinstance(row, Mapping) and int(row.get("class", 0)) > 0
            ]
        if not classes:
            raise ValueError("component has no positive reinforcement class")
        cls = max(classes)
        rectangle = (*bounds, cls)
        fit_result = {
            "status": "N=1 direct bounding box",
            "is_feasible": True,
            "is_optimal": True,
            "rectangles": [rectangle],
            "objective": 0.0,
            "class_changes": [],
            "stats": {"n1_fast_path": True},
            "n1_fast_path": True,
        }
        anchored = add_box_anchorage(
            [rectangle], recipes=cfg.get("recipes"), diameters=cfg["diameters"], steps=cfg["steps"],
            anchor_factor=float(params.get("anchor_factor", 40.0)), axis=cfg["axis"], field=field["field_geometry"],
        )
        for row in anchored:
            row["component_id"] = component_id
        proxy_mass = 0.0
        for row in anchored:
            geometry = row.get("geometry") if isinstance(row, Mapping) else None
            if geometry is None:
                geometry = box(*row["bounds"][:4])
            density = cfg["densities"].get(row.get("class"), cfg["densities"].get(str(row.get("class")), 1.0))
            proxy_mass += float(geometry.area) * float(density)
        smooth = variant_is_smooth(variant)
        return {
            "n": 1, "component_id": component_id, "is_feasible": True, "is_optimal": True,
            "solve_state": "optimal",
            "solver_result": {
                "is_feasible": True, "is_optimal": True, "status": "Optimal",
                "n1_fast_path": True, "rectangles": [rectangle],
            },
            "rectangles": [rectangle], "fit_result": fit_result, "class_changes": [],
            "anchored_boxes": anchored, "proxy_mass": float(proxy_mass), "source": source,
            "variant": variant, "smooth": smooth, "n1_fast_path": True,
        }

    def _solver_options(self, task_id: str) -> tuple[dict[str, Any], int, float | None, float | None, str, bool]:
        solver = self._solver(task_id)
        threads = self.settings.effective_threads(solver.get("threads"))
        timeout = solver.get("timeout_seconds")
        if timeout is None:
            timeout = self.settings.solver_timeout
        time_limit = solver.get("solver_time_limit")
        if time_limit is None:
            time_limit = self.settings.solver_time_limit
        backend = str(solver.get("backend") or self.settings.solver_backend)
        require_optimal = bool(solver.get("require_optimal", self.settings.require_optimal))
        return solver, threads, timeout, time_limit, backend, require_optimal

    def handle_solve_component(self, job: PipelineJob) -> None:
        from A101.reinforcement_components import solve_component_frontier

        task_id, cid, n = job.task_id, int(job.payload["component_id"]), int(job.payload["n"])
        variant = payload_variant(job.payload)
        if self._is_n_cancelled(task_id, n, variant):
            self._publish(task_id, {"type": "component_n_cancelled", "component_id": cid, "n": n, "variant": variant})
            return
        meta = self.store.get_meta(task_id) or {}
        scene_task = bool(meta.get("scene_id") and str(meta["scene_id"]) != str(task_id))
        if not scene_task and (n == 1 or job.payload.get("force_single_box")):
            result = self._single_component_frontier(task_id, cid, variant=variant)
            self._variant_call(self.store.save_frontier_result, task_id, cid, 1, result, variant=variant)
            self._frontier_ready(task_id, cid, 1, result, variant=variant)
            return
        stored = self._variant_call(self.store.load_problem, task_id, cid, variant=variant)
        if not stored:
            raise KeyError("problem missing")
        _, threads, timeout, time_limit, backend, require_optimal = self._solver_options(task_id)
        results, data = solve_component_frontier(
            stored["problem"], [n], data={}, timeout=timeout, solver_time_limit=time_limit, threads=threads,
            backend=backend, require_optimal=require_optimal, return_best_on_timeout=True, raise_errors=False,
        )
        self._variant_call(
            self.store.save_solver_result, task_id, cid, n,
            {"results": results, "data": data, "threads": threads, "variant": variant}, variant=variant
        )
        self.enqueue(JobKind.fit_component, task_id, {
            "component_id": cid, "n": n, "source": "components", "variant": variant,
            "smooth": variant_is_smooth(variant),
        })
        self._publish(task_id, {
            "type": "component_n_finished", "component_id": cid, "n": n, "stage": "solver",
            "variant": variant, "smooth": variant_is_smooth(variant),
        })

    def handle_fit_component(self, job: PipelineJob) -> None:
        from A101.reinforcement_components import fit_component_frontier

        task_id, cid, n = job.task_id, int(job.payload["component_id"]), int(job.payload["n"])
        variant = payload_variant(job.payload)
        existing = self._variant_call(self.store.load_frontier, task_id, cid, variant=variant).get(n)
        if existing is not None:
            self._frontier_ready(task_id, cid, n, existing, variant=variant)
            cleanup = getattr(self.store, "delete_solver_result", None)
            if callable(cleanup):
                self._variant_call(cleanup, task_id, cid, n, variant=variant)
            return
        stored = self._variant_call(self.store.load_problem, task_id, cid, variant=variant)
        if not stored:
            self._publish(task_id, {
                "type": "job_recovery", "stage": "fit_component", "reason": "problem_missing",
                "component_id": cid, "n": n, "variant": variant, "smooth": variant_is_smooth(variant),
            })
            self.enqueue(JobKind.prepare_component, task_id, {
                "component_id": cid, "auto_solve": True, "analysis_auto_solve": True,
                "variant": variant, "smooth": variant_is_smooth(variant),
            })
            return
        solved = self._variant_call(self.store.load_solver_result, task_id, cid, n, variant=variant)
        if not solved:
            self._publish(task_id, {
                "type": "job_recovery", "stage": "fit_component", "reason": "solver_result_missing",
                "component_id": cid, "n": n, "variant": variant, "smooth": variant_is_smooth(variant),
            })
            self.enqueue(JobKind.solve_component, task_id, {
                "component_id": cid, "n": n, "force_single_box": False, "source": "components",
                "variant": variant, "smooth": variant_is_smooth(variant),
            })
            return
        field = self._field(task_id, variant)
        cfg = field["cfg"]
        params = self._params(task_id)
        threads = self.settings.effective_threads(self._solver(task_id).get("threads"))
        frontier = fit_component_frontier(
            stored["problem"], solved["results"], recipes=cfg.get("recipes"), densities=cfg["densities"],
            diameters=cfg["diameters"], steps=cfg["steps"], anchor_factor=float(params.get("anchor_factor", 40.0)),
            axis=cfg["axis"], field=field["field_geometry"], min_width=float(params.get("min_width_mm", 1000.0)),
            time_limit=self.settings.fit_time_limit, allow_class_upgrade=True,
            fit_milp_backend=self.settings.fit_milp_backend, fit_threads=threads,
        )
        result = dict(frontier.get(n, {"n": n, "is_feasible": False, "error": "fit result missing"}))
        result.update(variant=variant, smooth=variant_is_smooth(variant))
        self._variant_call(self.store.save_frontier_result, task_id, cid, n, result, variant=variant)
        self._frontier_ready(task_id, cid, n, result, variant=variant)
        cleanup = getattr(self.store, "delete_solver_result", None)
        if callable(cleanup):
            self._variant_call(cleanup, task_id, cid, n, variant=variant)

    def _frontier_ready(
        self, task_id: str, cid: Any, n: int, result: Mapping[str, Any], *, variant: str = "raw"
    ) -> None:
        self._publish(task_id, {
            "type": "component_fit_finished", "component_id": cid, "n": int(n),
            "status": "feasible" if result.get("is_feasible") else result.get("solve_state", "failed"),
            "variant": variant, "smooth": variant_is_smooth(variant),
        })
        version = self._variant_call(self.store.frontier_version, task_id, variant=variant)
        self.enqueue(
            JobKind.combine_frontiers, task_id,
            {"frontier_version": version, "offset": 0, "variant": variant, "smooth": variant_is_smooth(variant)},
            dedupe_key=f"combine:{task_id}:{variant}:{self.store.generation(task_id)}:{version}",
        )

    def handle_combine_frontiers(self, job: PipelineJob) -> None:
        from A101.reinforcement_components import combine_component_frontiers

        task_id = job.task_id
        variant = payload_variant(job.payload)
        current_version = self._variant_call(self.store.frontier_version, task_id, variant=variant)
        requested_version = int(job.payload.get("frontier_version", current_version))
        if requested_version < current_version and int(job.payload.get("offset", 0)) == 0:
            return
        component_ids = [cid for cid in self._variant_call(self.store.component_ids, task_id, variant=variant) if cid != "whole"]
        frontiers = self._variant_call(self.store.all_frontiers, task_id, variant=variant)
        if not component_ids or any(
            (int(cid) if cid.lstrip("-").isdigit() else cid) not in frontiers for cid in component_ids
        ):
            return
        if any(
            not any(r.get("is_feasible") for r in frontiers[int(cid) if cid.lstrip("-").isdigit() else cid].values())
            for cid in component_ids
        ):
            return
        meta = self.store.get_meta(task_id) or {}
        top_k = int(meta.get("component_result_top_k", self.settings.frontier_top_k))
        combined = combine_component_frontiers(frontiers, top_k=top_k)
        flat = [(int(total_n), rank, candidate) for total_n, rows in sorted(combined.items()) for rank, candidate in enumerate(rows)]
        start = int(job.payload.get("offset", 0))
        end = min(len(flat), start + max(1, int(self.settings.combine_batch_size)))
        queued = 0
        for total_n, _rank, candidate in flat[start:end]:
            candidate_id = stable_digest({
                "task_id": task_id, "source": "components", "variant": variant, "total_n": total_n,
                "component_ns": candidate.get("component_ns", {}),
            })
            candidate = {
                **dict(candidate), "candidate_id": candidate_id, "source": "components", "total_N": total_n,
                "variant": variant, "smooth": variant_is_smooth(variant),
            }
            self.store.save_candidate(task_id, candidate_id, candidate)
            queued += int(self.enqueue(JobKind.layout_solution, task_id, {
                "candidate_id": candidate_id, "total_n": total_n, "source": "components", "variant": variant,
                "smooth": variant_is_smooth(variant),
            }))
        if end < len(flat):
            self.enqueue(
                JobKind.combine_frontiers, task_id,
                {"frontier_version": current_version, "offset": end, "variant": variant, "smooth": variant_is_smooth(variant)},
                dedupe_key=f"combine:{task_id}:{variant}:{self.store.generation(task_id)}:{current_version}:{end}",
            )
        self._publish(task_id, {
            "type": "frontier_updated", "frontier_version": current_version, "total_n": sorted(map(int, combined)),
            "queued_solutions": queued, "variant": variant, "smooth": variant_is_smooth(variant),
        })

    def _layout_candidate(self, task_id: str, candidate: Mapping[str, Any]) -> dict[str, Any]:
        from A101.rebar_field_layout import layout_rebars
        from A101.reinforcement_components import bar_mass_kg

        variant = str(candidate.get("variant", "raw"))
        field = self._field(task_id, variant)
        cfg = field["cfg"]
        params = self._params(task_id)
        layout = dict(layout_rebars(
            polygons=[p["geometry"] for p in field["start_polygons"]],
            boxes=candidate.get("anchored_boxes", []), background=tuple(cfg["back_grid"]), axis=cfg["axis"],
            min_step=float(self.settings.min_internal_step),
        ) or {})
        feasible = bool(layout.get("is_feasible"))
        mass = bar_mass_kg(layout.get("bars", []), float(params.get("steel_density_kg_m3", 7850.0))) if feasible else float("inf")
        component_ns = {str(k): int(v) for k, v in dict(candidate.get("component_ns", {})).items()}
        total_n = int(candidate.get("total_N", candidate.get("total_n", sum(component_ns.values()))))
        source = str(candidate.get("source", "components"))
        solution_id = stable_digest({
            "task_id": task_id, "source": source, "variant": variant, "total_n": total_n,
            "component_ns": component_ns, "candidate": candidate.get("candidate_id"),
        })
        choices = candidate.get("component_choices", {}) or {}
        optimal = bool(choices) and all(bool(row.get("is_optimal")) for row in choices.values())
        return {
            **dict(candidate), "solution_id": solution_id, "source": source, "variant": variant,
            "smooth": variant_is_smooth(variant), "total_N": total_n, "component_ns": component_ns,
            "actual_mass_kg": float(mass), "is_feasible": feasible, "is_optimal": optimal and feasible,
            "status": "feasible" if feasible else str(layout.get("status", "infeasible")), "bar_layout": layout,
            "metadata": {
                "threads": self.settings.effective_threads(self._solver(task_id).get("threads")),
                "created_at": time.time(), "variant": variant, "smooth": variant_is_smooth(variant),
                "rebar_config_source": cfg.get("rebar_config_source"),
            },
        }

    def handle_layout_solution(self, job: PipelineJob) -> None:
        task_id = job.task_id
        candidate = self.store.load_candidate(task_id, str(job.payload["candidate_id"]))
        if candidate is None:
            raise KeyError("candidate missing")
        solution = self._layout_candidate(task_id, candidate)
        variant = str(solution.get("variant", "raw"))
        self.store.save_solution(task_id, solution)
        self._publish(task_id, {
            "type": "solution_available", "solution_id": solution["solution_id"], "source": solution["source"],
            "total_N": solution["total_N"], "is_feasible": solution["is_feasible"],
            "actual_mass_kg": solution["actual_mass_kg"],
            "result_url": f"/v1/tasks/{task_id}/solutions/{solution['solution_id']}",
            "variant": variant, "smooth": variant_is_smooth(variant),
        })
        best = self.store.best_solution(task_id, solution["total_N"], variant=variant)
        if best and best.get("solution_id") == solution["solution_id"]:
            status = "feasible" if solution["is_feasible"] else "infeasible"
            result_url = (
                f"/v1/tasks/{task_id}/results/{solution['total_N']}"
                if variant == "raw"
                else f"/v1/tasks/{task_id}/solutions/{solution['solution_id']}"
            )
            self.store.set_n_status(
                task_id, solution["total_N"], status, variant=variant,
                solution_id=solution["solution_id"], source=solution["source"], result_url=result_url,
            )
            self._publish(task_id, {
                "type": "n_finished", "n": solution["total_N"], "status": status,
                "result_url": result_url, "solution_id": solution["solution_id"], "source": solution["source"],
                "variant": variant, "smooth": variant_is_smooth(variant),
            })
        if (self.store.get_meta(task_id) or {}).get("validate_results") and solution.get("is_feasible"):
            self.enqueue(JobKind.validate_solution, task_id, {
                "solution_id": solution["solution_id"], "variant": variant, "smooth": variant_is_smooth(variant),
            })

    def handle_validate_solution(self, job: PipelineJob) -> None:
        solution = self.store.load_solution(job.task_id, str(job.payload["solution_id"]))
        if solution is None:
            raise KeyError("solution missing")
        solution = {**solution, "validation": {"status": "not_configured"}}
        self.store.save_solution(job.task_id, solution)

    def _whole_component(self, task_id: str, variant: str = "raw") -> dict[str, Any]:
        field = self._field(task_id, variant)
        cfg = field["cfg"]
        rows = []
        for index, raw in enumerate(field["ortho_polygons"]):
            load = raw.get("load", raw.get("value", raw.get("class"))) if isinstance(raw, Mapping) else raw[1]
            geometry = raw.get("geometry") if isinstance(raw, Mapping) else raw[0]
            try:
                cls = int(cfg["load2cls"].get(load, cfg["load2cls"].get(str(load))))
            except Exception:
                continue
            if cls == 0:
                continue
            rows.append({"geometry": geometry, "load": float(load), "class": cls, "source_index": index})
        if not rows:
            return {"id": -1, "axis": cfg["axis"], "polygon_indices": [], "polygons": [],
                    "geometry": Polygon(), "demand_geometry": Polygon(), "bounds": [],
                    "demand_bounds": [], "classes": [], "loads": [], "max_hold": 0.0,
                    "expanded_polygons": [], "variant": variant, "smooth": variant_is_smooth(variant), "no_additional_demand": True}
        demand = unary_union([r["geometry"] for r in rows])
        stable_indices = [
            int(value) for value in (field.get("decomposition", {}) or {}).get("active_indices", [])
        ]
        return {
            "id": -1, "axis": cfg["axis"],
            "polygon_indices": stable_indices or [r["source_index"] for r in rows],
            "polygons": rows, "geometry": demand, "demand_geometry": demand,
            "bounds": tuple(map(float, demand.bounds)), "demand_bounds": tuple(map(float, demand.bounds)),
            "classes": sorted({r["class"] for r in rows}), "loads": sorted({r["load"] for r in rows}),
            "max_hold": max(map(float, cfg.get("holds", {0: 0}).values()), default=0.0),
            "expanded_polygons": [], "variant": variant, "smooth": variant_is_smooth(variant),
        }

    def whole_component_info(self, task_id: str, *, variant: str = "raw") -> dict[str, Any]:
        record = self._variant_call(self.store.load_component, task_id, WHOLE_COMPONENT_KEY, variant=variant)
        if record is not None:
            return dict(record.get("info", record))
        component = self._whole_component(task_id) if variant == "raw" else self._whole_component(task_id, variant)
        info = component_public_info(component, max_useful_n=None, state="available")
        info.update(variant=variant, smooth=variant_is_smooth(variant))
        return info

    def handle_prepare_whole(self, job: PipelineJob) -> None:
        task_id = job.task_id
        variant = payload_variant(job.payload)
        if job.payload.get("schedule_only"):
            record = self._variant_call(self.store.load_component, task_id, "whole", variant=variant) or {}
            self._schedule_plan(
                task_id, "whole", record.get("plan", []), bool(record.get("force_single_box")),
                int(job.payload.get("start", 0)), whole=True, variant=variant,
            )
            return

        component = self._whole_component(task_id) if variant == "raw" else self._whole_component(task_id, variant)
        if component.get("no_additional_demand"):
            self._variant_call(self.store.save_component, task_id, "whole",
                               {"component": component, "state": "prepared", "max_n_state": "ready",
                                "max_useful_n": 0, "plan": [],
                                "max_n_milp": {"feasible": True, "max_useful_n": 0, "reason": "no_positive_n_required"}},
                               variant=variant)
            self._maybe_complete_analysis(task_id, variant, bool(job.payload.get("analysis_auto_solve", True)))
            return
        self._variant_call(
            self.store.save_component, task_id, "whole",
            {
                "component": component, "state": "preparing",
                "info": component_public_info(component), "variant": variant,
                "max_useful_n": None, "max_n_state": "preparing",
            },
            variant=variant,
        )
        from A101.reinforcement_components import CandidateCoverInfeasible
        try:
            prepared = (
                self._prepare_problem(task_id, "whole", component)
                if variant == "raw"
                else self._prepare_problem(task_id, "whole", component, variant=variant)
            )
        except CandidateCoverInfeasible as exc:
            self._record_cover_infeasible(job, "whole", {"component": component}, exc)
            return

        # Legacy/manual whole preparation keeps its historical behavior.
        # New immutable analyses always carry analysis_auto_solve and use the
        # worker-side exact max-N MILP below.
        if "analysis_auto_solve" not in job.payload:
            meta = self.store.get_meta(task_id) or {}
            explicit_requested = job.payload.get("requested_n")
            invalid: list[int] = []
            if explicit_requested is not None:
                plan = list(dict.fromkeys(int(n) for n in explicit_requested))
                invalid = [n for n in plan if n < 1 or n > int(prepared["max_useful_n"])]
                force_single = False
            else:
                plan, force_single = choose_component_ns(
                    self._requested_ns(task_id, variant) or [1],
                    int(prepared["max_useful_n"]),
                    str(meta.get("scan_mode")) == "hard",
                )
            record = {
                "component": component, "state": "prepared", "plan": plan,
                "force_single_box": force_single, "max_useful_n": int(prepared["max_useful_n"]),
                "bounds": prepared["bounds"],
                "info": component_public_info(component, int(prepared["max_useful_n"]), "prepared"),
                "variant": variant, "smooth": variant_is_smooth(variant),
            }
            self._variant_call(self.store.save_component, task_id, "whole", record, variant=variant)
            if invalid:
                raise ValueError(f"n вне допустимого диапазона 1..{prepared['max_useful_n']}: {invalid}")
            if bool(job.payload.get("auto_solve", True)):
                self._schedule_plan(task_id, "whole", plan, force_single, whole=True, variant=variant)
            return

        record = {
            "component": component, "state": "max_n_queued", "plan": [], "force_single_box": False,
            "max_useful_n": None, "max_n_state": "queued", "bounds": prepared["bounds"],
            "info": component_public_info(component, None, "max_n_queued"),
            "analysis_auto_solve": bool(job.payload.get("analysis_auto_solve", True)),
            "variant": variant, "smooth": variant_is_smooth(variant),
        }
        self._variant_call(self.store.save_component, task_id, "whole", record, variant=variant)
        self.enqueue(
            JobKind.compute_max_n_whole, task_id,
            {
                "component_id": "whole", "variant": variant, "smooth": variant_is_smooth(variant),
                "analysis_auto_solve": bool(job.payload.get("analysis_auto_solve", True)),
            },
        )

    def handle_compute_max_n_whole(self, job: PipelineJob) -> None:
        task_id = job.task_id
        variant = payload_variant(job.payload)
        overlay_id = self._current_overlay_id()
        record = self._variant_call(
            self.store.load_component, task_id, "whole", variant=variant, overlay_id=overlay_id
        ) or {}
        get_meta = getattr(self.store, "get_meta", None)
        meta = (get_meta(task_id) or {}) if callable(get_meta) else {}
        scene_task = (
            bool(meta.get("scene_id") and str(meta.get("scene_id")) != str(task_id))
            and callable(getattr(self.store, "component_ids", None))
        )

        if scene_task:
            # For immutable scene-backed tasks, direct whole-field N is not bounded
            # by the number of disconnected components and is not estimated from
            # a separate whole mask-cover MILP. Its useful upper bound is the sum
            # of real-component maxima. Therefore N=1 is always eligible to be
            # attempted whenever there is positive demand, even with 30+ components.
            if str(record.get("max_n_state")) == "ready" and record.get("max_n_source") == "component_sum":
                return
            component_ids = self.real_component_ids(task_id, variant=variant, overlay_id=overlay_id)
            component_maxima: dict[int, int] = {}
            waiting = False
            structural_failure = False
            for cid in component_ids:
                component_record = self._variant_call(
                    self.store.load_component, task_id, cid, variant=variant, overlay_id=overlay_id
                ) or {}
                state = str(component_record.get("max_n_state", ""))
                raw_max = component_record.get("max_useful_n")
                if state == "infeasible":
                    structural_failure = True
                    break
                if state != "ready" or raw_max is None:
                    waiting = True
                    break
                value = int(raw_max)
                if value <= 0:
                    structural_failure = True
                    break
                component_maxima[int(cid)] = value

            if waiting:
                record.update(
                    state="max_n_waiting", max_n_state="waiting_components",
                    max_useful_n=None, max_n_source="component_sum",
                    component_maxima=component_maxima,
                )
                self._variant_call(
                    self.store.save_component, task_id, "whole", record,
                    variant=variant, overlay_id=overlay_id,
                )
                return

            if not component_ids:
                feasible = False
                max_n = None
                result = {
                    "feasible": False, "max_useful_n": None,
                    "reason": "no_real_components", "component_maxima": {},
                }
            elif structural_failure or len(component_maxima) != len(component_ids):
                feasible = False
                max_n = None
                result = {
                    "feasible": False, "max_useful_n": None,
                    "reason": "component_max_n_unavailable",
                    "component_maxima": component_maxima,
                }
            else:
                max_n = sum(component_maxima.values())
                feasible = max_n > 0
                result = {
                    "feasible": feasible,
                    "max_useful_n": max_n if feasible else None,
                    "matrix_max_useful_n": max_n if feasible else None,
                    "reason": "sum_component_maxima",
                    "component_maxima": component_maxima,
                }
        else:
            result = self._compute_exact_max_n(task_id, "whole", variant=variant)
            feasible = bool(result.get("feasible")) and int(result.get("max_useful_n", 0) or 0) > 0
            max_n = int(result.get("max_useful_n")) if feasible else None

        requested = self._requested_ns(task_id, variant, overlay_id)
        plan = edge_to_middle_order(
            n for n in requested if max_n is not None and 1 <= int(n) <= min(max_n, SOLVER_HARD_MAX_N)
        )
        result = {**dict(result), "max_useful_n": max_n}
        record.update(
            state="prepared" if feasible else "max_n_infeasible",
            max_n_state="ready" if feasible else "infeasible",
            max_useful_n=max_n, max_n_milp=result, plan=plan, force_single_box=False,
            max_n_source="component_sum" if scene_task else "whole_matrix",
            component_maxima=result.get("component_maxima"),
            info=component_public_info(record.get("component", {}), max_n, "prepared" if feasible else "infeasible"),
            variant=variant, smooth=variant_is_smooth(variant),
        )
        self._variant_call(
            self.store.save_component, task_id, "whole", record, variant=variant, overlay_id=overlay_id
        )
        self._publish(task_id, {
            "type": "whole_max_n_ready", "component_id": "whole", "max_useful_n": max_n,
            "feasible": feasible, "planned_n": plan, "variant": variant,
            "smooth": variant_is_smooth(variant), "overlay_id": overlay_id,
            "max_n_source": record.get("max_n_source"),
        })
        analysis_auto_solve = bool(record.get("analysis_auto_solve", job.payload.get("analysis_auto_solve", True)))
        if feasible and analysis_auto_solve and plan:
            self._schedule_plan(task_id, WHOLE_COMPONENT_KEY, plan, False, whole=True, variant=variant)
        self._maybe_complete_analysis(task_id, variant, analysis_auto_solve)

    def handle_solve_whole(self, job: PipelineJob) -> None:
        from A101.reinforcement_components import solve_component_frontier

        task_id = job.task_id
        variant = payload_variant(job.payload)
        n = int(job.payload["n"])
        if self._is_n_cancelled(task_id, n, variant):
            return
        meta = self.store.get_meta(task_id) or {}
        scene_task = bool(meta.get("scene_id") and str(meta["scene_id"]) != str(task_id))
        if not scene_task and (n == 1 or job.payload.get("force_single_box")):
            result = self._single_component_frontier(task_id, "whole", "whole", variant=variant)
            self._variant_call(self.store.save_frontier_result, task_id, "whole", 1, result, variant=variant)
            self._queue_whole_layout(task_id, result, variant=variant)
            return
        stored = self._variant_call(self.store.load_problem, task_id, "whole", variant=variant)
        if not stored:
            raise KeyError("whole problem missing")
        if self._is_n_cancelled(task_id, n, variant):
            return
        _, threads, timeout, time_limit, backend, require_optimal = self._solver_options(task_id)
        results, data = solve_component_frontier(
            stored["problem"], [n], data={}, timeout=timeout, solver_time_limit=time_limit, threads=threads,
            backend=backend, require_optimal=require_optimal, return_best_on_timeout=True, raise_errors=False,
        )
        self._variant_call(
            self.store.save_solver_result, task_id, "whole", n,
            {"results": results, "data": data, "threads": threads, "variant": variant}, variant=variant
        )
        self.enqueue(JobKind.fit_whole, task_id, {
            "component_id": "whole", "n": n, "source": "whole", "variant": variant,
            "smooth": variant_is_smooth(variant),
        })

    def handle_fit_whole(self, job: PipelineJob) -> None:
        from A101.reinforcement_components import fit_component_frontier

        task_id, n = job.task_id, int(job.payload["n"])
        variant = payload_variant(job.payload)
        existing = self._variant_call(self.store.load_frontier, task_id, "whole", variant=variant).get(n)
        if existing is not None:
            if existing.get("is_feasible"):
                self._queue_whole_layout(task_id, existing, variant=variant)
            cleanup = getattr(self.store, "delete_solver_result", None)
            if callable(cleanup):
                self._variant_call(cleanup, task_id, "whole", n, variant=variant)
            return
        stored = self._variant_call(self.store.load_problem, task_id, "whole", variant=variant)
        if not stored:
            self._publish(task_id, {
                "type": "job_recovery", "stage": "fit_whole", "reason": "problem_missing",
                "component_id": "whole", "n": n, "variant": variant, "smooth": variant_is_smooth(variant),
            })
            self.enqueue(JobKind.prepare_whole, task_id, {
                "auto_solve": True, "analysis_auto_solve": True, "variant": variant,
                "smooth": variant_is_smooth(variant),
            })
            return
        solved = self._variant_call(self.store.load_solver_result, task_id, "whole", n, variant=variant)
        if not solved:
            self._publish(task_id, {
                "type": "job_recovery", "stage": "fit_whole", "reason": "solver_result_missing",
                "component_id": "whole", "n": n, "variant": variant, "smooth": variant_is_smooth(variant),
            })
            self.enqueue(JobKind.solve_whole, task_id, {
                "component_id": "whole", "n": n, "force_single_box": False, "source": "whole",
                "variant": variant, "smooth": variant_is_smooth(variant),
            })
            return
        field = self._field(task_id, variant)
        cfg = field["cfg"]
        params = self._params(task_id)
        threads = self.settings.effective_threads(self._solver(task_id).get("threads"))
        frontier = fit_component_frontier(
            stored["problem"], solved["results"], recipes=cfg.get("recipes"), densities=cfg["densities"],
            diameters=cfg["diameters"], steps=cfg["steps"], anchor_factor=float(params.get("anchor_factor", 40.0)),
            axis=cfg["axis"], field=field["field_geometry"], min_width=float(params.get("min_width_mm", 1000.0)),
            time_limit=self.settings.fit_time_limit, allow_class_upgrade=True,
            fit_milp_backend=self.settings.fit_milp_backend, fit_threads=threads,
        )
        result = dict(frontier.get(n, {"n": n, "is_feasible": False}))
        result.update(source="whole", variant=variant, smooth=variant_is_smooth(variant))
        self._variant_call(self.store.save_frontier_result, task_id, "whole", n, result, variant=variant)
        if result.get("is_feasible"):
            self._queue_whole_layout(task_id, result, variant=variant)
        cleanup = getattr(self.store, "delete_solver_result", None)
        if callable(cleanup):
            self._variant_call(cleanup, task_id, "whole", n, variant=variant)

    def _queue_whole_layout(
        self, task_id: str, result: Mapping[str, Any], *, variant: str = "raw"
    ) -> None:
        n = int(result.get("n", 1))
        candidate_id = stable_digest({"task_id": task_id, "source": "whole", "variant": variant, "n": n})
        candidate = {
            **dict(result), "candidate_id": candidate_id, "source": "whole", "total_N": n,
            "component_ns": {"whole": n}, "component_choices": {"whole": dict(result)},
            "variant": variant, "smooth": variant_is_smooth(variant),
        }
        self.store.save_candidate(task_id, candidate_id, candidate)
        self.enqueue(JobKind.layout_solution, task_id, {
            "candidate_id": candidate_id, "total_n": n, "source": "whole", "variant": variant,
            "smooth": variant_is_smooth(variant),
        })

    def schedule_requested_for_all(
        self, task_id: str, values: Sequence[int], *, smooth: bool = False
    ) -> dict[str, list[int]]:
        variant = analysis_variant(smooth)
        queued: dict[str, list[int]] = {}
        for cid in self._variant_call(self.store.component_ids, task_id, variant=variant):
            if cid == "whole":
                continue
            record = self._variant_call(self.store.load_component, task_id, cid, variant=variant) or {}
            max_n = record.get("max_useful_n")
            if not max_n:
                continue
            plan, fallback = choose_component_ns(values, int(max_n), False)
            queued[cid] = plan
            for n in plan:
                self.enqueue(JobKind.solve_component, task_id, {
                    "component_id": int(cid), "n": int(n), "force_single_box": bool(fallback and int(n) == 1),
                    "source": "components", "variant": variant, "smooth": smooth,
                })
        whole = self._variant_call(self.store.load_component, task_id, "whole", variant=variant)
        if whole and whole.get("max_useful_n"):
            plan, fallback = choose_component_ns(values, int(whole["max_useful_n"]), False)
            queued["whole"] = plan
            for n in plan:
                self.enqueue(JobKind.solve_whole, task_id, {
                    "component_id": "whole", "n": int(n), "force_single_box": bool(fallback and int(n) == 1),
                    "source": "whole", "variant": variant, "smooth": smooth,
                })
        return queued

    def schedule_component_n(
        self, task_id: str, component_id: int, values: Sequence[int], *, smooth: bool = False
    ) -> list[int]:
        variant = analysis_variant(smooth)
        storage_id = component_storage_id(component_id)
        requested = list(dict.fromkeys(int(n) for n in values))
        hard_max_n = min(int(self.settings.max_n_value), SOLVER_HARD_MAX_N)
        invalid_basic = [n for n in requested if n < 1 or n > hard_max_n]
        if invalid_basic:
            raise ValueError(f"n вне допустимого диапазона 1..{hard_max_n}: {invalid_basic}")
        record = self._variant_call(self.store.load_component, task_id, storage_id, variant=variant)
        if storage_id == WHOLE_COMPONENT_KEY and (record is None or not record.get("max_useful_n")):
            if not self._variant_call(self.store.load_field, task_id, variant=variant):
                raise KeyError(f"field variant={variant} не подготовлен; сначала вызовите /components/prepare?smooth={str(smooth).lower()}")
            self.enqueue(
                JobKind.prepare_whole, task_id,
                ({"auto_solve": True, "requested_n": requested}
                 if variant == "raw" else
                 {"auto_solve": True, "requested_n": requested, "variant": variant, "smooth": True}),
                dedupe_key=(
                    f"prepare-whole-explicit:{task_id}:{variant}:{self.store.generation(task_id)}:{stable_digest(requested)}"
                ),
            )
            return requested
        if record is None or not record.get("max_useful_n"):
            raise KeyError(f"component variant={variant} не подготовлена")
        max_n = int(record["max_useful_n"])
        invalid = [n for n in requested if n < 1 or n > max_n]
        if invalid:
            raise ValueError(f"n вне допустимого диапазона 1..{max_n}: {invalid}")
        whole = storage_id == WHOLE_COMPONENT_KEY
        kind = JobKind.solve_whole if whole else JobKind.solve_component
        for n in requested:
            self.enqueue(kind, task_id, {
                "component_id": WHOLE_COMPONENT_KEY if whole else int(storage_id), "n": n,
                "force_single_box": False, "source": "whole" if whole else "components",
                "variant": variant, "smooth": smooth,
            })
        return requested

    # ---------- overlay-aware workflow overrides ----------
    def _current_overlay_id(self) -> int:
        context = getattr(self, "_overlay_context", None)
        return 0 if context is None else normalize_overlay_id(context.get())

    def _call_with_overlay(self, method, *args, variant: str = "raw", overlay_id: int | None = None):
        selected_overlay = self._current_overlay_id() if overlay_id is None else normalize_overlay_id(overlay_id)
        kwargs: dict[str, Any] = {}
        if variant != "raw":
            kwargs["variant"] = variant
        if selected_overlay:
            kwargs["overlay_id"] = selected_overlay
        try:
            return method(*args, **kwargs)
        except TypeError:
            # Historical test doubles predate overlay support. Keep overlay=0/raw compatible.
            kwargs.pop("overlay_id", None)
            try:
                return method(*args, **kwargs)
            except TypeError:
                if variant == "raw":
                    return method(*args)
                raise

    def _variant_call(self, method, *args, variant: str = "raw", overlay_id: int | None = None):
        return self._call_with_overlay(method, *args, variant=variant, overlay_id=overlay_id)

    def _requested_ns(self, task_id: str, variant: str, overlay_id: int | None = None) -> list[int]:
        selected_overlay = self._current_overlay_id() if overlay_id is None else normalize_overlay_id(overlay_id)
        method = getattr(self.store, "requested_ns", None)
        if callable(method):
            try:
                values = method(task_id, variant=variant, overlay_id=selected_overlay)
            except TypeError:
                try:
                    values = method(task_id, variant=variant)
                except TypeError:
                    values = method(task_id)
            if values:
                return [int(n) for n in values]
        meta = self.store.get_meta(task_id) or {}
        return [int(n) for n in meta.get("requested_n", [1])]

    def _is_n_cancelled(self, task_id: str, n: int, variant: str, overlay_id: int | None = None) -> bool:
        selected_overlay = self._current_overlay_id() if overlay_id is None else normalize_overlay_id(overlay_id)
        method = getattr(self.store, "is_n_cancelled", None)
        if not callable(method):
            return False
        try:
            return bool(method(task_id, int(n), variant=variant, overlay_id=selected_overlay))
        except TypeError:
            try:
                return bool(method(task_id, int(n), variant=variant))
            except TypeError:
                return bool(method(task_id, int(n)))

    def _publish(self, task_id: str, event: Mapping[str, Any]) -> str:
        row = dict(event)
        event_type = str(row.pop("type"))
        selected_overlay = normalize_overlay_id(row.pop("overlay_id", self._current_overlay_id()))
        try:
            return self.store.publish_event(task_id, event_type, row, overlay_id=selected_overlay)
        except TypeError:
            return self.store.publish_event(task_id, event_type, row)

    def enqueue(
        self,
        kind: JobKind | str,
        task_id: str,
        payload: Mapping[str, Any] | None = None,
        *,
        generation: int | None = None,
        dedupe_key: str | None = None,
    ) -> bool:
        if self.store.pending_jobs(task_id) >= int(self.settings.max_jobs_per_task):
            raise RuntimeError(f"REBAR_MAX_JOBS_PER_TASK exceeded for task {task_id}")
        generation = self.store.generation(task_id) if generation is None else int(generation)
        body = dict(payload or {})
        selected_overlay = self._current_overlay_id()
        if selected_overlay and "overlay_id" not in body:
            body["overlay_id"] = selected_overlay
        job = PipelineJob(
            kind.value if isinstance(kind, JobKind) else str(kind), task_id, body,
            generation=generation, dedupe_key=dedupe_key,
        )
        return self.store.enqueue_pipeline_job(job.to_dict())

    def prepare_task(
        self,
        task_id: str,
        *,
        auto_solve: bool | None = None,
        smooth: bool = False,
        overlay_id: int | None = 0,
    ) -> bool:
        meta = self.store.get_meta(task_id)
        if meta is None:
            raise KeyError(task_id)
        if auto_solve is None:
            auto_solve = not bool(meta.get("manual_mode", False))
        variant = analysis_variant(smooth)
        selected_overlay = normalize_overlay_id(overlay_id)

        ensure = getattr(self.store, "ensure_analysis", None)
        mark = getattr(self.store, "mark_analysis_preparing", None)
        if callable(ensure) and callable(mark):
            ensure(task_id, variant=variant, overlay_id=selected_overlay)
            if not bool(mark(task_id, variant=variant, overlay_id=selected_overlay)):
                return False

        self.store.patch_meta(task_id, state="queued_preparation", generation=self.store.generation(task_id))
        self._publish(task_id, {
            "type": "pipeline_queued",
            "requested_n": self._requested_ns(task_id, variant, selected_overlay),
            "scan_mode": meta.get("scan_mode", "requested"), "whole": bool(meta.get("whole", False)),
            "auto_solve": bool(auto_solve), "variant": variant, "smooth": bool(smooth),
            "overlay_id": selected_overlay,
        })
        payload = {"auto_solve": bool(auto_solve), "variant": variant, "smooth": bool(smooth)}
        if selected_overlay:
            payload["overlay_id"] = selected_overlay
        return self.enqueue(JobKind.prepare_field, task_id, payload)

    def prepare_task_components(
        self,
        task_id: str,
        *,
        auto_solve: bool | None = None,
        smooth: bool = False,
        overlay_id: int | None = 0,
    ) -> dict[str, Any]:
        """Build task-scoped field/components synchronously, then queue only heavy component work."""
        meta = self.store.get_meta(task_id)
        if meta is None:
            raise KeyError(task_id)
        if auto_solve is None:
            auto_solve = not bool(meta.get("manual_mode", False))
        variant = analysis_variant(smooth)
        selected_overlay = normalize_overlay_id(overlay_id)

        ensure = getattr(self.store, "ensure_analysis", None)
        mark = getattr(self.store, "mark_analysis_preparing", None)
        if callable(ensure):
            ensure(task_id, variant=variant, overlay_id=selected_overlay)
        if callable(mark):
            mark(task_id, variant=variant, overlay_id=selected_overlay)

        self.store.patch_meta(task_id, state="preparing_components", generation=self.store.generation(task_id))
        self._publish(task_id, {
            "type": "pipeline_queued",
            "requested_n": self._requested_ns(task_id, variant, selected_overlay),
            "scan_mode": meta.get("scan_mode", "requested"),
            "whole": bool(meta.get("whole", False)),
            "auto_solve": bool(auto_solve),
            "variant": variant,
            "smooth": bool(smooth),
            "overlay_id": selected_overlay,
            "component_preparation": "api",
        })
        payload = {
            "auto_solve": bool(auto_solve),
            "variant": variant,
            "smooth": bool(smooth),
            "overlay_id": selected_overlay,
            "task_scoped_components": True,
        }
        token = self._overlay_context.set(selected_overlay)
        try:
            self.handle_prepare_field(PipelineJob(
                JobKind.prepare_field.value, task_id, payload, generation=self.store.generation(task_id)
            ))
            field = self._variant_call(self.store.load_field, task_id, variant=variant, overlay_id=selected_overlay)
            return {} if field is None else dict(field)
        finally:
            self._overlay_context.reset(token)

    def real_component_ids(
        self, task_id: str, *, variant: str = "raw", overlay_id: int | None = 0
    ) -> list[int]:
        selected_overlay = normalize_overlay_id(overlay_id)
        values = self._variant_call(
            self.store.component_ids, task_id, variant=variant, overlay_id=selected_overlay
        )
        return sorted(
            int(cid) for cid in values
            if str(cid) not in {WHOLE_COMPONENT_KEY, str(WHOLE_COMPONENT_ID)}
        )

    def selected_real_component_ids(
        self, task_id: str, *, variant: str = "raw", overlay_id: int | None = 0
    ) -> list[int]:
        """Return real component ids selected for solver work in this immutable task."""
        all_real = self.real_component_ids(task_id, variant=variant, overlay_id=overlay_id)
        meta = self.store.get_meta(task_id) or {}
        selection = [int(x) for x in (meta.get("component_selection") or ([-2] if meta.get("whole") else [-3]))]
        if selection in ([-2], [-3]):
            return all_real
        if selection == [-1]:
            # With a single real component, whole-field solve is exactly the
            # same computational object. Reuse component 0 instead of
            # preparing/solving a duplicate stored ``whole`` problem.
            return all_real if len(all_real) == 1 else []
        wanted = {value for value in selection if value >= 0}
        return [cid for cid in all_real if cid in wanted]

    def aggregate_component_info(
        self, task_id: str, *, variant: str = "raw", overlay_id: int | None = 0
    ) -> dict[str, Any]:
        """Return virtual whole metadata without creating a stored -1 component."""
        selected_overlay = normalize_overlay_id(overlay_id)
        component_ids = self.real_component_ids(task_id, variant=variant, overlay_id=selected_overlay)
        if not component_ids:
            return {
                "id": WHOLE_COMPONENT_ID, "component_ids": [], "max_useful_n": 0,
                "prepared": True, "state": "empty", "variant": variant,
                "smooth": variant_is_smooth(variant), "overlay_id": selected_overlay,
            }

        records = [
            self._variant_call(self.store.load_component, task_id, cid, variant=variant, overlay_id=selected_overlay) or {}
            for cid in component_ids
        ]
        infos = [dict(record.get("info", record) or {}) for record in records]
        maxima: list[int] = []
        prepared = True
        structural_infeasible = False
        for record in records:
            raw_max = record.get("max_useful_n")
            state = str(record.get("max_n_state", ""))
            if state == "infeasible":
                prepared = False
                structural_infeasible = True
                break
            if raw_max is None or int(raw_max) <= 0 or state not in {"", "ready"}:
                prepared = False
                break
            maxima.append(int(raw_max))

        polygon_indices = sorted({int(idx) for info in infos for idx in (info.get("polygon_indices") or [])})
        classes = sorted({int(value) for info in infos for value in (info.get("classes") or [])})
        loads = sorted({float(value) for info in infos for value in (info.get("loads") or [])})
        bounds_rows = [info.get("bounds") for info in infos if info.get("bounds")]
        demand_rows = [info.get("demand_bounds") for info in infos if info.get("demand_bounds")]

        def merged_bounds(rows):
            if not rows:
                return None
            return [
                min(float(row[0]) for row in rows), min(float(row[1]) for row in rows),
                max(float(row[2]) for row in rows), max(float(row[3]) for row in rows),
            ]

        result = {
            "id": WHOLE_COMPONENT_ID, "component_ids": component_ids,
            "polygon_indices": polygon_indices, "classes": classes, "loads": loads,
            "bounds": merged_bounds(bounds_rows), "demand_bounds": merged_bounds(demand_rows),
            "max_useful_n": sum(maxima) if prepared else None,
            "prepared": prepared,
            "state": "prepared" if prepared else "infeasible" if structural_infeasible else "preparing",
            "variant": variant, "smooth": variant_is_smooth(variant), "overlay_id": selected_overlay,
        }
        if len(component_ids) == 1:
            result["alias_component_id"] = component_ids[0]
        return result

    def aggregate_frontier(
        self, task_id: str, *, variant: str = "raw", overlay_id: int | None = 0
    ) -> dict[int, dict[str, Any]]:
        """Return the best current virtual aggregate candidate for each total N."""
        from A101.reinforcement_components import combine_component_frontiers

        selected_overlay = normalize_overlay_id(overlay_id)
        component_ids = self.real_component_ids(task_id, variant=variant, overlay_id=selected_overlay)
        if not component_ids:
            return {}
        if len(component_ids) == 1:
            return self._variant_call(
                self.store.load_frontier, task_id, component_ids[0], variant=variant, overlay_id=selected_overlay
            )
        frontiers = {
            cid: self._variant_call(
                self.store.load_frontier, task_id, cid, variant=variant, overlay_id=selected_overlay
            )
            for cid in component_ids
        }
        combined = combine_component_frontiers(
            frontiers, top_k=int((self.store.get_meta(task_id) or {}).get("component_result_top_k", self.settings.frontier_top_k))
        )
        return {
            int(total): {
                **dict(rows[0]), "n": int(total), "total_N": int(total),
                "is_feasible": True, "status": "feasible", "source": "components",
            }
            for total, rows in combined.items() if rows
        }

    def aggregate_requested(
        self, task_id: str, *, variant: str = "raw", overlay_id: int | None = 0
    ) -> bool:
        get_meta = getattr(self.store, "get_meta", None)
        if not callable(get_meta):
            # Pre-scene/legacy stores always combined component frontiers downstream.
            return True
        meta = get_meta(task_id) or {}
        scene_task = bool(meta.get("scene_id") and str(meta.get("scene_id")) != str(task_id))
        if not scene_task:
            return True
        selection = [int(x) for x in (meta.get("component_selection") or ([-2] if meta.get("whole") else [-3]))]
        if selection == [-1]:
            return len(self.real_component_ids(task_id, variant=variant, overlay_id=overlay_id)) == 1
        if selection in ([-2], [-3]):
            return True
        return len([value for value in selection if value >= 0]) > 1

    def schedule_requested_for_all(
        self, task_id: str, values: Sequence[int], *, smooth: bool = False, overlay_id: int | None = 0, register_request: bool = True
    ) -> dict[str, list[int]]:
        """Schedule user-supplied N as local N for each selected solver unit."""
        variant = analysis_variant(smooth)
        selected_overlay = normalize_overlay_id(overlay_id)
        requested = list(dict.fromkeys(int(n) for n in values))
        hard_max_n = min(int(self.settings.max_n_value), SOLVER_HARD_MAX_N)
        invalid = [n for n in requested if n < 1 or n > hard_max_n]
        if invalid:
            raise ValueError(f"n вне допустимого диапазона 1..{hard_max_n}: {invalid}")

        add_requested = getattr(self.store, "add_requested_ns", None)
        if register_request and callable(add_requested):
            try:
                add_requested(task_id, requested, variant=variant, overlay_id=selected_overlay)
            except TypeError:
                try:
                    add_requested(task_id, requested, variant=variant)
                except TypeError:
                    add_requested(task_id, requested)

        meta = self.store.get_meta(task_id) or {}
        state_method = getattr(self.store, "analysis_state", None)
        if callable(state_method):
            ensure = getattr(self.store, "ensure_analysis", None)
            if callable(ensure):
                ensure(task_id, variant=variant, overlay_id=selected_overlay)
            state = state_method(task_id, variant=variant, overlay_id=selected_overlay) or {}
            if str(state.get("preparation_state", "stored")) != "prepared":
                scene_task = bool(meta.get("scene_id") and str(meta.get("scene_id")) != str(task_id))
                load_field = getattr(self.store, "load_field", None)
                field = (
                    self._variant_call(load_field, task_id, variant=variant, overlay_id=selected_overlay)
                    if callable(load_field) else None
                )
                if not field:
                    if scene_task:
                        self.prepare_task_components(
                            task_id, auto_solve=True, smooth=smooth, overlay_id=selected_overlay
                        )
                    else:
                        self.prepare_task(task_id, auto_solve=True, smooth=smooth, overlay_id=selected_overlay)
                    return {"preparing": requested}
                if not scene_task:
                    # Historical tasks keep their old all-at-once preparation gate.
                    return {"preparing": requested}
                # Scene-backed tasks may already have some max-N bounds while
                # other selected units are still preparing. Schedule every
                # ready unit now; late units will read the accumulated local-N
                # request when their max-N calculation finishes.

        context = getattr(self, "_overlay_context", None)
        token = context.set(selected_overlay) if context is not None else None
        try:
            selection = [int(x) for x in (meta.get("component_selection") or ([-2] if meta.get("whole") else [-3]))]
            all_real = self.real_component_ids(
                task_id, variant=variant, overlay_id=selected_overlay
            )
            selected_real = self.selected_real_component_ids(
                task_id, variant=variant, overlay_id=selected_overlay
            )
            # A single component aliases whole and must never be solved twice.
            whole_requested = selection in ([-1], [-2]) and len(all_real) != 1
            hard_scan = str(meta.get("scan_mode", "requested")) == "hard"

            unit_plans: dict[Any, list[int]] = {}
            unit_kinds: dict[Any, JobKind] = {}
            for cid in selected_real:
                record = self._variant_call(
                    self.store.load_component, task_id, cid, variant=variant, overlay_id=selected_overlay
                ) or {}
                raw_max = record.get("max_useful_n")
                if raw_max is None or int(raw_max) <= 0 or str(record.get("max_n_state", "ready")) != "ready":
                    continue
                max_n = min(int(raw_max), hard_max_n)
                plan = list(range(1, max_n + 1)) if hard_scan else [n for n in requested if n <= max_n]
                if plan:
                    unit_plans[cid] = edge_to_middle_order(plan)
                    unit_kinds[cid] = JobKind.solve_component

            if whole_requested:
                whole = self._variant_call(
                    self.store.load_component, task_id, WHOLE_COMPONENT_KEY, variant=variant, overlay_id=selected_overlay
                ) or {}
                raw_max = whole.get("max_useful_n")
                if raw_max is not None and int(raw_max) > 0 and str(whole.get("max_n_state", "ready")) == "ready":
                    max_n = min(int(raw_max), hard_max_n)
                    plan = list(range(1, max_n + 1)) if hard_scan else [n for n in requested if n <= max_n]
                    if plan:
                        unit_plans[WHOLE_COMPONENT_KEY] = edge_to_middle_order(plan)
                        unit_kinds[WHOLE_COMPONENT_KEY] = JobKind.solve_whole

            completed: dict[Any, set[int]] = {}
            read_done = getattr(self.store, "completed_frontier_ns", None)
            if callable(read_done):
                for unit in unit_plans:
                    completed[unit] = self._variant_call(
                        read_done, task_id, unit, variant=variant, overlay_id=selected_overlay
                    )

            for unit, n in round_robin_unit_plans(unit_plans):
                if n in completed.get(unit, set()):
                    continue
                if self._is_n_cancelled(task_id, n, variant, selected_overlay):
                    continue
                whole = unit == WHOLE_COMPONENT_KEY
                self.enqueue(
                    unit_kinds[unit], task_id,
                    {
                        "component_id": WHOLE_COMPONENT_KEY if whole else int(unit),
                        "n": int(n), "force_single_box": False,
                        "source": "whole" if whole else "components",
                        "variant": variant, "smooth": smooth,
                    },
                )
            return {str(unit): list(plan) for unit, plan in unit_plans.items()}
        finally:
            if context is not None and token is not None:
                context.reset(token)

    def schedule_component_n(
        self, task_id: str, component_id: int, values: Sequence[int], *, smooth: bool = False,
        overlay_id: int | None = 0,
    ) -> list[int]:
        variant = analysis_variant(smooth)
        selected_overlay = normalize_overlay_id(overlay_id)
        state_method = getattr(self.store, "analysis_state", None)
        if callable(state_method):
            state = state_method(task_id, variant=variant, overlay_id=selected_overlay)
            if not state or str(state.get("preparation_state")) != "prepared":
                raise AnalysisNotPreparedError(
                    "Analysis is not prepared; schedule N through /v1/tasks/{task_id}/n first"
                )

        context = getattr(self, "_overlay_context", None)
        token = context.set(selected_overlay) if context is not None else None
        try:
            requested = list(dict.fromkeys(int(n) for n in values))
            hard_max_n = min(int(self.settings.max_n_value), SOLVER_HARD_MAX_N)
            invalid_basic = [n for n in requested if n < 1 or n > hard_max_n]
            if invalid_basic:
                raise ValueError(f"n вне допустимого диапазона 1..{hard_max_n}: {invalid_basic}")

            meta = self.store.get_meta(task_id) or {}
            immutable = bool(meta.get("scene_id") and str(meta["scene_id"]) != str(task_id))
            selection = [int(x) for x in (meta.get("component_selection") or [-2])]

            # Historical tasks persisted a real ``whole`` component and may still
            # have old clients/jobs addressing component -1. Keep that contract
            # intact. For scene-backed tasks, scheduling -1 means direct whole-field
            # local N; reading -1 metadata remains the virtual aggregate summary.
            if not immutable:
                storage_id = component_storage_id(component_id)
                record = self._variant_call(
                    self.store.load_component, task_id, storage_id, variant=variant, overlay_id=selected_overlay
                )
                if storage_id == WHOLE_COMPONENT_KEY and (record is None or not record.get("max_useful_n")):
                    load_field = getattr(self.store, "load_field", None)
                    field = (
                        self._variant_call(load_field, task_id, variant=variant, overlay_id=selected_overlay)
                        if callable(load_field) else None
                    )
                    if not field:
                        raise KeyError(
                            f"field variant={variant} не подготовлен; сначала вызовите /components/prepare?smooth={str(smooth).lower()}"
                        )
                    payload = {"auto_solve": True, "requested_n": requested, "variant": variant, "smooth": smooth}
                    self.enqueue(
                        JobKind.prepare_whole, task_id, payload,
                        dedupe_key=(
                            f"prepare-whole-explicit:{task_id}:{variant}:{selected_overlay}:"
                            f"{self.store.generation(task_id)}:{stable_digest(requested)}"
                        ),
                    )
                    return requested
                if record is None or not record.get("max_useful_n"):
                    raise KeyError(f"component variant={variant} не подготовлена")
                max_n = min(int(record["max_useful_n"]), hard_max_n)
                invalid = [n for n in requested if n > max_n]
                if invalid:
                    raise ValueError(f"n вне допустимого диапазона 1..{max_n}: {invalid}")
                whole = storage_id == WHOLE_COMPONENT_KEY
                kind = JobKind.solve_whole if whole else JobKind.solve_component
                for n in requested:
                    self.enqueue(kind, task_id, {
                        "component_id": WHOLE_COMPONENT_KEY if whole else int(storage_id),
                        "n": int(n), "force_single_box": False,
                        "source": "whole" if whole else "components",
                        "variant": variant, "smooth": smooth,
                    })
                return requested

            component_ids = self.real_component_ids(task_id, variant=variant, overlay_id=selected_overlay)

            if int(component_id) == WHOLE_COMPONENT_ID:
                if selection not in ([-1], [-2]):
                    raise ValueError("Whole -1 не входит в неизменяемый набор задачи; создайте новый task")
                if not component_ids:
                    raise ValueError("Расчетные компоненты отсутствуют")

                # One real component is the whole field already; never solve it twice.
                if len(component_ids) == 1:
                    cid = int(component_ids[0])
                    record = self._variant_call(
                        self.store.load_component, task_id, cid, variant=variant, overlay_id=selected_overlay
                    ) or {}
                    raw_max = record.get("max_useful_n")
                    if raw_max is None or int(raw_max) <= 0 or str(record.get("max_n_state", "ready")) != "ready":
                        raise ValueError(f"Компонента {cid} не имеет подготовленного допустимого max N")
                    max_n = min(int(raw_max), hard_max_n)
                    invalid = [n for n in requested if n > max_n]
                    if invalid:
                        raise ValueError(f"n вне допустимого диапазона 1..{max_n}: {invalid}")
                    completed = set()
                    read_done = getattr(self.store, "completed_frontier_ns", None)
                    if callable(read_done):
                        completed = self._variant_call(
                            read_done, task_id, cid, variant=variant, overlay_id=selected_overlay
                        )
                    for n in requested:
                        if n in completed or self._is_n_cancelled(task_id, n, variant, selected_overlay):
                            continue
                        self.enqueue(JobKind.solve_component, task_id, {
                            "component_id": cid, "n": int(n), "force_single_box": False,
                            "source": "components", "variant": variant, "smooth": smooth,
                        })
                    return requested

                # With multiple real components, -1 is a separate direct whole-field
                # solver unit. User-supplied N remains local N for that unit.
                whole = self._variant_call(
                    self.store.load_component, task_id, WHOLE_COMPONENT_KEY,
                    variant=variant, overlay_id=selected_overlay
                ) or {}
                raw_max = whole.get("max_useful_n")
                if raw_max is None or int(raw_max) <= 0 or str(whole.get("max_n_state", "ready")) != "ready":
                    raise ValueError("Whole-поле не имеет подготовленного допустимого max N")
                max_n = min(int(raw_max), hard_max_n)
                invalid = [n for n in requested if n > max_n]
                if invalid:
                    raise ValueError(f"n вне допустимого диапазона 1..{max_n}: {invalid}")
                completed = set()
                read_done = getattr(self.store, "completed_frontier_ns", None)
                if callable(read_done):
                    completed = self._variant_call(
                        read_done, task_id, WHOLE_COMPONENT_KEY, variant=variant, overlay_id=selected_overlay
                    )
                for n in requested:
                    if n in completed or self._is_n_cancelled(task_id, n, variant, selected_overlay):
                        continue
                    self.enqueue(JobKind.solve_whole, task_id, {
                        "component_id": WHOLE_COMPONENT_KEY, "n": int(n), "force_single_box": False,
                        "source": "whole", "variant": variant, "smooth": smooth,
                    })
                return requested

            cid = int(component_id)
            if cid not in component_ids:
                raise KeyError(f"component {cid} variant={variant} overlay={selected_overlay} не найдена")
            if immutable:
                allowed = selection in ([-2], [-3]) or (selection and selection[0] >= 0 and cid in selection)
                if not allowed:
                    raise ValueError("Компонента не входит в неизменяемый набор задачи; создайте новый task")
            record = self._variant_call(
                self.store.load_component, task_id, cid, variant=variant, overlay_id=selected_overlay
            ) or {}
            raw_max = record.get("max_useful_n")
            if raw_max is None or int(raw_max) <= 0 or str(record.get("max_n_state", "ready")) != "ready":
                raise ValueError("Компонента не имеет подготовленного допустимого max N")
            max_n = min(int(raw_max), SOLVER_HARD_MAX_N)
            invalid = [n for n in requested if n > max_n]
            if invalid:
                raise ValueError(f"n вне допустимого диапазона 1..{max_n}: {invalid}")
            for n in requested:
                self.enqueue(JobKind.solve_component, task_id, {
                    "component_id": cid, "n": int(n), "force_single_box": False,
                    "source": "components", "variant": variant, "smooth": smooth,
                })
            return requested
        finally:
            if context is not None and token is not None:
                context.reset(token)

    def dispatch(self, job: PipelineJob) -> None:
        kind = str(job.kind)
        if kind != JobKind.materialize_scene.value and int(job.generation) != self.store.generation(job.task_id):
            return
        handler = getattr(self, "handle_" + kind, None)
        if handler is None:
            raise ValueError(f"Неизвестный job kind: {job.kind}")
        if kind == JobKind.materialize_scene.value:
            handler(job)
            return
        token = self._overlay_context.set(payload_overlay_id(job.payload))
        try:
            handler(job)
        finally:
            self._overlay_context.reset(token)

    def _field(self, task_id: str, variant: str = "raw") -> dict[str, Any]:
        field = self._variant_call(self.store.load_field, task_id, variant=variant)
        if not field:
            raise KeyError(
                f"field variant={variant} overlay={self._current_overlay_id()} не подготовлен для task {task_id}"
            )
        return field

    def handle_prepare_field(self, job: PipelineJob) -> None:
        from A101.axis_orientation import class_holds, normalize_axis
        from A101.calculate_mass import ReinforcementCapacityError, resolve_rebar_config
        from A101.grid_work import clean_poly
        from A101.poly_bbox import rect_polygons
        from A101.reinforcement_components import split_reinforcement_components

        task_id = job.task_id
        variant = payload_variant(job.payload)
        overlay_id = payload_overlay_id(job.payload)
        task_scoped_components = bool(job.payload.get("task_scoped_components", False))
        smooth = variant_is_smooth(variant)
        auto_solve = bool(job.payload.get("auto_solve", True))
        self.store.patch_meta(task_id, state="preparing_components")
        self._publish(task_id, {
            "type": "component_prepare_started", "variant": variant, "smooth": smooth,
            "overlay_id": overlay_id,
        })
        input_obj = self.store.get_object(task_id, "input")
        params = self._params(task_id)
        persisted_polygons = self._persisted_variant_polygons(task_id, variant, input_obj)
        all_polygons = polygons_from_input({"kind": "polygons", "units": "mm", "polygons": persisted_polygons})

        # Rebar configuration deliberately depends on the immutable source variant, not on overlay edits.
        try:
            cfg = resolve_rebar_config(
                all_polygons,
                back_grid=params.get("back_grid"), stock=params.get("stock"), max_layers=params.get("max_layers"),
            )
        except ReinforcementCapacityError as exc:
            detail = {
                "reason": "reinforcement_capacity",
                "load": float(exc.load),
                "max_supported_load": float(exc.max_supported_load),
                "max_layers": exc.max_layers,
                "back_grid": None if exc.back_grid is None else list(exc.back_grid),
                "variant": variant,
                "smooth": smooth,
                "overlay_id": overlay_id,
            }
            mark = getattr(self.store, "mark_analysis_infeasible", None)
            if callable(mark):
                try:
                    mark(task_id, variant=variant, overlay_id=overlay_id, detail=detail)
                except TypeError:
                    mark(task_id, variant=variant, overlay_id=overlay_id)
            for requested_n in self._requested_ns(task_id, variant, overlay_id):
                try:
                    self.store.set_n_status(
                        task_id, int(requested_n), "infeasible", variant=variant, overlay_id=overlay_id, **detail
                    )
                except TypeError:
                    self.store.set_n_status(task_id, int(requested_n), "infeasible", **detail)
            self.store.patch_meta(task_id, state="completed" if auto_solve else "ready")
            self._publish(task_id, {"type": "analysis_infeasible", **detail})
            return
        axis = normalize_axis(str(params.get("axis", "y")))
        cfg["axis"] = axis
        anchor_factor = float(params.get("anchor_factor", 40.0))
        base_holds, holds, leaves = class_holds(cfg["diameters"], cfg.get("recipes"), anchor_factor)
        cfg.update(base_holds=base_holds, holds=holds, recipe_leaves=leaves)

        meta_before = self.store.get_meta(task_id) or {}
        effective = dict(meta_before.get("effective_rebar_config", {}) or {})
        effective[variant] = {
            "back_grid": tuple(cfg["back_grid"]), "stock": [tuple(row) for row in cfg["stock"]],
            "max_layers": int(cfg["max_layers"]), "source": cfg.get("rebar_config_source"),
        }
        self.store.patch_meta(task_id, effective_rebar_config=effective)

        resolve = getattr(self.store, "resolved_source_polygons", None)
        if callable(resolve):
            resolved = list(resolve(task_id, variant=variant, overlay_id=overlay_id))
        else:
            resolved = [
                {**dict(row), "source_index": i, "overlay_state": "active", "active": True, "real": False}
                for i, row in enumerate(persisted_polygons)
            ]
        sets = overlay_polygon_sets(resolved)
        demand_polygons = polygons_from_input({"kind": "polygons", "units": "mm", "polygons": sets["active"]}) if sets["active"] else []
        for target, source in zip(demand_polygons, sets["active"]):
            target["source_index"] = int(source["source_index"])
        physical_polygons = polygons_from_input({"kind": "polygons", "units": "mm", "polygons": sets["physical"]}) if sets["physical"] else []
        for target, source in zip(physical_polygons, sets["physical"]):
            target.update(
                source_index=int(source["source_index"]), overlay_state=str(source["overlay_state"]),
                active=bool(source.get("active", source["overlay_state"] == "active")), real=bool(source.get("real", False)),
            )

        ortho = clean_poly(rect_polygons(demand_polygons)) if demand_polygons else []
        meta_context = self.store.get_meta(task_id) or {}
        stable_method = getattr(self.store, "scene_components", None)
        scene_id = str(meta_context.get("scene_id") or task_id)
        stable_defs = []
        if not task_scoped_components and callable(stable_method):
            try:
                stable_defs = list(stable_method(scene_id))
            except (KeyError, TypeError):
                stable_defs = []

        if stable_defs:
            stable_components = build_scene_analysis_components(
                demand_polygons, stable_defs, load2cls=cfg["load2cls"], axis=axis, holds=cfg.get("holds")
            )
            split = {
                "axis": axis,
                "components": stable_components,
                "active_indices": list(sets["active_indices"]),
                "background_only_indices": list(sets["background_only_indices"]),
                "removed_indices": list(sets["removed_indices"]),
                "degenerate_indices": [],
                "stats": {"components": len(stable_components), "stable_scene_components": True},
            }
        else:
            split = split_reinforcement_components(
                ortho,
                load2cls=cfg["load2cls"], recipes=cfg.get("recipes"), diameters=cfg["diameters"],
                anchor_factor=anchor_factor, axis=axis,
            ) if ortho else {"components": [], "active_indices": [], "background_only_indices": [], "degenerate_indices": []}
            split = dict(split)
            map_components_to_source_indices(list(split.get("components", [])), demand_polygons)
            split["active_indices"] = list(sets["active_indices"])
            split["background_only_indices"] = list(sets["background_only_indices"])
            split["removed_indices"] = list(sets["removed_indices"])
            split.setdefault("degenerate_indices", [])

        geometries = [p["geometry"] for p in physical_polygons if p.get("geometry") is not None and not p["geometry"].is_empty]
        field_geometry = unary_union(geometries) if geometries else Polygon()
        field = {
            "filename": str(input_obj.get("filename", "input.dxf")) if isinstance(input_obj, Mapping) else "input.dxf",
            "start_polygons": physical_polygons, "ortho_polygons": ortho, "cfg": cfg,
            "decomposition": split, "field_geometry": field_geometry,
            "variant": variant, "smooth": smooth, "overlay_id": overlay_id,
        }
        all_components = list(split.get("components", []))
        selection = [int(x) for x in (meta_context.get("component_selection") or ([-2] if meta_context.get("whole") else [-3]))]
        available = {int(component["id"]) for component in all_components}
        if task_scoped_components and selection and selection[0] >= 0:
            missing = sorted(set(selection) - available)
            if missing:
                raise ValueError(f"Unknown component ids: {missing}")

        if task_scoped_components:
            single_whole_alias = len(available) == 1 and selection in ([-1], [-2])
            if selection in ([-2], [-3]) or single_whole_alias:
                selected_ids = set(available)
            elif selection == [-1]:
                selected_ids = set()
            else:
                selected_ids = {value for value in selection if value >= 0}
            prepare_whole = selection in ([-1], [-2]) and bool(available) and not single_whole_alias
            if single_whole_alias:
                field["whole_alias_component_id"] = next(iter(available))
        else:
            if selection == [-1]:
                selected_ids = set()
            elif selection and selection[0] >= 0:
                selected_ids = {value for value in selection if value >= 0}
            else:
                selected_ids = set(available)
            prepare_whole = selection in ([-1], [-2])

        field["aggregate_requested"] = task_scoped_components and (
            selection in ([-2], [-3]) or len(selected_ids) > 1
            or (selection == [-1] and len(selected_ids) == 1)
        )
        # Direct whole-field solve uses the sum of real-component max-N values as
        # its upper bound. For [-1] with multiple components we therefore prepare
        # component problems/max-N as bound-only helpers, but never schedule their
        # solve_component jobs.
        bound_only_ids = (
            set(available)
            if task_scoped_components and selection == [-1] and prepare_whole
            else set()
        )
        field["whole_bound_component_ids"] = sorted(bound_only_ids)
        field["expected_solver_units"] = sorted(selected_ids) + ([WHOLE_COMPONENT_KEY] if prepare_whole else [])
        # Preserve the full task decomposition for inspection even when only whole or a subset is solved.
        split["components"] = all_components
        self._variant_call(self.store.save_field, task_id, field, variant=variant)

        for component in all_components:
            cid = int(component["id"])
            selected = cid in selected_ids
            bound_only = cid in bound_only_ids
            needs_max_n = selected or bound_only
            self._variant_call(
                self.store.save_component, task_id, cid,
                {
                    "component": component,
                    "info": component_public_info(component, None, "queued" if needs_max_n else "available"),
                    "state": "queued" if needs_max_n else "available",
                    "max_n_state": "queued" if needs_max_n else "not_requested",
                    "max_useful_n": None,
                    "variant": variant,
                    "overlay_id": overlay_id,
                },
                variant=variant,
            )
            if needs_max_n:
                payload = {
                    "component_id": cid, "auto_solve": False,
                    # Bound-only component preparation for [-1] must never create
                    # local component solutions; it exists solely to determine the
                    # whole-field upper N bound.
                    "analysis_auto_solve": auto_solve if selected else False,
                    "variant": variant, "smooth": smooth,
                }
                if bound_only:
                    payload["whole_bound_only"] = True
                self.enqueue(JobKind.prepare_component, task_id, payload)

        if prepare_whole:
            self.enqueue(
                JobKind.prepare_whole, task_id,
                {
                    "auto_solve": False, "analysis_auto_solve": auto_solve,
                    "variant": variant, "smooth": smooth,
                },
            )

        self.store.patch_meta(task_id, state="components_ready")
        self._publish(task_id, {
            "type": "components_ready", "components": [component_public_info(c) for c in all_components],
            "active_indices": split.get("active_indices", []),
            "background_only_indices": split.get("background_only_indices", []),
            "removed_indices": split.get("removed_indices", []),
            "degenerate_indices": split.get("degenerate_indices", []),
            "variant": variant, "smooth": smooth, "overlay_id": overlay_id,
        })

        # Empty analyses have no component prepare jobs that could close preparation.
        if not all_components and not prepare_whole:
            if str(meta_context.get("scene_id") or task_id) != task_id:
                self._maybe_complete_analysis(task_id, variant, auto_solve)
            else:
                mark = getattr(self.store, "mark_analysis_prepared", None)
                if callable(mark):
                    mark(task_id, variant=variant, overlay_id=overlay_id)
                if auto_solve:
                    self.schedule_requested_for_all(task_id, self._requested_ns(task_id, variant, overlay_id),
                                                    smooth=smooth, overlay_id=overlay_id)

    def _schedule_plan(
        self, task_id: str, component_id: Any, plan: Sequence[int], force_single: bool,
        start: int = 0, whole: bool = False, *, variant: str = "raw",
    ) -> None:
        batch = max(1, int(self.settings.scheduler_batch_size))
        end = min(len(plan), int(start) + batch)
        kind = JobKind.solve_whole if whole else JobKind.solve_component
        smooth = variant_is_smooth(variant)
        overlay_id = self._current_overlay_id()
        for n in plan[int(start):end]:
            if self._is_n_cancelled(task_id, int(n), variant, overlay_id):
                continue
            self.enqueue(kind, task_id, {
                "component_id": component_id, "n": int(n),
                "force_single_box": bool(force_single and int(n) == 1),
                "source": "whole" if whole else "components", "variant": variant, "smooth": smooth,
            })
        if end < len(plan):
            self.enqueue(
                JobKind.prepare_whole if whole else JobKind.prepare_component,
                task_id,
                {"component_id": component_id, "schedule_only": True, "start": end, "variant": variant, "smooth": smooth},
                dedupe_key=(
                    f"schedule:{task_id}:{variant}:{overlay_id}:{component_id}:{end}:{self.store.generation(task_id)}"
                ),
            )

    def _frontier_ready(
        self, task_id: str, cid: Any, n: int, result: Mapping[str, Any], *, variant: str = "raw"
    ) -> None:
        overlay_id = self._current_overlay_id()
        is_feasible = bool(result.get("is_feasible"))
        is_optimal = bool(result.get("is_optimal")) and is_feasible
        status = (
            "optimal" if is_optimal else "feasible" if is_feasible
            else str(result.get("status", result.get("solve_state", "infeasible"))).lower()
        )
        self._publish(task_id, {
            "type": "component_fit_finished", "component_id": cid, "n": int(n), "status": status,
            "variant": variant, "smooth": variant_is_smooth(variant), "overlay_id": overlay_id,
        })
        # Local infeasible is a normal frontier outcome. Aggregate recomputation is needed
        # only when the immutable task actually requested the virtual whole view.
        if not self.aggregate_requested(task_id, variant=variant, overlay_id=overlay_id):
            return
        version = self._variant_call(self.store.frontier_version, task_id, variant=variant)
        self.enqueue(
            JobKind.combine_frontiers, task_id,
            {"frontier_version": version, "offset": 0, "variant": variant, "smooth": variant_is_smooth(variant)},
            dedupe_key=f"combine:{task_id}:{variant}:{overlay_id}:{self.store.generation(task_id)}:{version}",
        )

    def handle_combine_frontiers(self, job: PipelineJob) -> None:
        from A101.reinforcement_components import combine_component_frontiers

        task_id = job.task_id
        variant = payload_variant(job.payload)
        overlay_id = payload_overlay_id(job.payload)
        if not self.aggregate_requested(task_id, variant=variant, overlay_id=overlay_id):
            return
        current_version = self._variant_call(self.store.frontier_version, task_id, variant=variant)
        requested_version = int(job.payload.get("frontier_version", current_version))
        if requested_version < current_version and int(job.payload.get("offset", 0)) == 0:
            self.enqueue(
                JobKind.combine_frontiers, task_id,
                {"frontier_version": current_version, "offset": 0,
                 "variant": variant, "smooth": variant_is_smooth(variant)},
                dedupe_key=f"combine:{task_id}:{variant}:{overlay_id}:{self.store.generation(task_id)}:{current_version}",
            )
            return

        component_ids = self.selected_real_component_ids(task_id, variant=variant, overlay_id=overlay_id)
        if not component_ids:
            return
        maxima: dict[int, int] = {}
        frontiers: dict[int, dict[int, dict[str, Any]]] = {}
        for cid in component_ids:
            record = self._variant_call(
                self.store.load_component, task_id, cid, variant=variant, overlay_id=overlay_id
            ) or {}
            raw_max = record.get("max_useful_n")
            if raw_max is None or int(raw_max) <= 0 or str(record.get("max_n_state", "ready")) != "ready":
                return
            maxima[cid] = int(raw_max)
            frontiers[cid] = self._variant_call(
                self.store.load_frontier, task_id, cid, variant=variant, overlay_id=overlay_id
            )

        meta = self.store.get_meta(task_id) or {}
        top_k = int(meta.get("component_result_top_k", self.settings.frontier_top_k))
        all_combined = combine_component_frontiers(frontiers, top_k=top_k)
        combined = {int(total): rows for total, rows in all_combined.items() if rows}
        flat = [
            (int(total_n), rank, candidate)
            for total_n, rows in sorted(combined.items())
            for rank, candidate in enumerate(rows)
        ]
        start = int(job.payload.get("offset", 0))
        end = min(len(flat), start + max(1, int(self.settings.combine_batch_size)))
        queued = 0
        for total_n, _rank, raw_candidate in flat[start:end]:
            candidate_id = stable_digest({
                "task_id": task_id, "source": "components", "variant": variant, "overlay_id": overlay_id,
                "total_n": total_n, "component_ns": raw_candidate.get("component_ns", {}),
            })
            candidate = {
                **dict(raw_candidate), "candidate_id": candidate_id, "source": "components", "total_N": total_n,
                "variant": variant, "smooth": variant_is_smooth(variant), "overlay_id": overlay_id,
            }
            self.store.save_candidate(task_id, candidate_id, candidate)
            queued += int(self.enqueue(JobKind.layout_solution, task_id, {
                "candidate_id": candidate_id, "total_n": total_n, "source": "components",
                "variant": variant, "smooth": variant_is_smooth(variant),
            }))
        if end < len(flat):
            self.enqueue(
                JobKind.combine_frontiers, task_id,
                {"frontier_version": current_version, "offset": end, "variant": variant, "smooth": variant_is_smooth(variant)},
                dedupe_key=(
                    f"combine:{task_id}:{variant}:{overlay_id}:{self.store.generation(task_id)}:{current_version}:{end}"
                ),
            )

        self._publish(task_id, {
            "type": "frontier_updated", "frontier_version": current_version,
            "total_n": sorted(map(int, combined)), "queued_solutions": queued,
            "variant": variant, "smooth": variant_is_smooth(variant), "overlay_id": overlay_id,
        })

    def _queue_whole_layout(self, task_id: str, result: Mapping[str, Any], *, variant: str = "raw") -> None:
        n = int(result.get("n", 1))
        overlay_id = self._current_overlay_id()
        candidate_id = stable_digest({
            "task_id": task_id, "source": "whole", "variant": variant, "overlay_id": overlay_id, "n": n,
        })
        candidate = {
            **dict(result), "candidate_id": candidate_id, "source": "whole", "total_N": n,
            "component_ns": {"whole": n}, "component_choices": {"whole": dict(result)},
            "variant": variant, "smooth": variant_is_smooth(variant), "overlay_id": overlay_id,
        }
        self.store.save_candidate(task_id, candidate_id, candidate)
        self.enqueue(JobKind.layout_solution, task_id, {
            "candidate_id": candidate_id, "total_n": n, "source": "whole", "variant": variant,
            "smooth": variant_is_smooth(variant),
        })

    def _layout_candidate(self, task_id: str, candidate: Mapping[str, Any]) -> dict[str, Any]:
        from A101.rebar_field_layout import layout_rebars
        from A101.reinforcement_components import bar_mass_kg

        variant = str(candidate.get("variant", "raw"))
        overlay_id = normalize_overlay_id(candidate.get("overlay_id", self._current_overlay_id()))
        field = self._field(task_id, variant)
        cfg = field["cfg"]
        params = self._params(task_id)
        layout = dict(layout_rebars(
            polygons=[p["geometry"] for p in field["start_polygons"]],
            boxes=candidate.get("anchored_boxes", []), background=tuple(cfg["back_grid"]), axis=cfg["axis"],
            min_step=float(self.settings.min_internal_step),
        ) or {})
        feasible = bool(layout.get("is_feasible"))
        density = float(params.get("steel_density_kg_m3", 7850.0))
        if feasible:
            layout = augment_layout_mass_metrics(
                layout, steel_density_kg_m3=density,
                anchor_factor=float(params.get("anchor_factor", 40.0)),
            )
            from .compact_zones import compact_zones_from_layout
            compact_zones = compact_zones_from_layout(layout)
            mass = float(layout["mass_metrics"]["with_anchorage_kg"])
        else:
            compact_zones = []
            mass = float("inf")
        component_ns = {str(k): int(v) for k, v in dict(candidate.get("component_ns", {})).items()}
        total_n = int(candidate.get("total_N", candidate.get("total_n", sum(component_ns.values()))))
        source = str(candidate.get("source", "components"))
        solution_id = stable_digest({
            "task_id": task_id, "source": source, "variant": variant, "overlay_id": overlay_id,
            "total_n": total_n, "component_ns": component_ns, "candidate": candidate.get("candidate_id"),
        })
        choices = candidate.get("component_choices", {}) or {}
        optimal = bool(choices) and all(bool(row.get("is_optimal")) for row in choices.values())
        is_optimal = bool(optimal and feasible)
        status = "optimal" if is_optimal else "feasible" if feasible else str(layout.get("status", "infeasible")).lower()
        return {
            **dict(candidate), "solution_id": solution_id, "source": source, "variant": variant,
            "smooth": variant_is_smooth(variant), "overlay_id": overlay_id, "total_N": total_n,
            "component_ns": component_ns, "actual_mass_kg": float(mass), "is_feasible": feasible,
            "is_optimal": is_optimal, "status": status, "bar_layout": layout,
            "compact_zones": compact_zones,
            "mass_metrics": dict(layout.get("mass_metrics", {}) or {}),
            "metadata": {
                "threads": self.settings.effective_threads(self._solver(task_id).get("threads")),
                "created_at": time.time(), "variant": variant, "smooth": variant_is_smooth(variant),
                "overlay_id": overlay_id, "rebar_config_source": cfg.get("rebar_config_source"),
            },
        }

    def handle_layout_solution(self, job: PipelineJob) -> None:
        task_id = job.task_id
        overlay_id = payload_overlay_id(job.payload)
        refresh_solution_id = job.payload.get("relayout_solution_id")
        if refresh_solution_id:
            saved = self.store.load_solution(task_id, str(refresh_solution_id), overlay_id=overlay_id)
            if saved is None:
                raise KeyError(f"saved solution missing: {refresh_solution_id}")
            if str(saved.get("variant", "raw")) != payload_variant(job.payload):
                raise ValueError("Контекст варианта не совпадает с сохранённым решением")
            if int(saved.get("overlay_id", 0)) != overlay_id:
                raise ValueError("Контекст overlay не совпадает с сохранённым решением")
            if "anchored_boxes" not in saved:
                raise ValueError("В решении нет anchored_boxes для повторного layout")
            candidate = {key: value for key, value in saved.items() if key not in {
                "bar_layout", "mass_metrics", "validation", "metadata", "actual_mass_kg",
            }}
        else:
            try:
                candidate = self.store.load_candidate(task_id, str(job.payload["candidate_id"]), overlay_id=overlay_id)
            except TypeError:
                candidate = self.store.load_candidate(task_id, str(job.payload["candidate_id"]))
            if candidate is None:
                raise KeyError("candidate missing")

        from .postgres_store import restore_json_geometries
        candidate = dict(candidate)
        candidate["anchored_boxes"] = restore_json_geometries(candidate.get("anchored_boxes", []))
        solution = self._layout_candidate(task_id, candidate)
        if refresh_solution_id:
            solution["solution_id"] = str(refresh_solution_id)
            solution["metadata"] = {
                **dict(solution.get("metadata", {})),
                "layout_refresh_id": job.payload.get("layout_refresh"),
                "layout_refreshed_at": time.time(),
            }

        variant = str(solution.get("variant", "raw"))
        total_n = int(solution["total_N"])
        try:
            previous_best = self.store.best_solution(task_id, total_n, variant=variant, overlay_id=overlay_id)
        except TypeError:
            previous_best = self.store.best_solution(task_id, total_n, variant=variant)

        self.store.save_solution(task_id, solution)
        solution_url = f"/v1/tasks/{task_id}/solutions/{solution['solution_id']}?overlay={overlay_id}"
        self._publish(task_id, {
            "type": "solution_available", "solution_id": solution["solution_id"], "source": solution["source"],
            "total_N": total_n, "is_feasible": solution["is_feasible"],
            "is_optimal": solution.get("is_optimal", False), "status": solution.get("status"),
            "actual_mass_kg": solution["actual_mass_kg"], "result_url": solution_url,
            "variant": variant, "smooth": variant_is_smooth(variant), "overlay_id": overlay_id,
        })

        try:
            best = self.store.best_solution(task_id, total_n, variant=variant, overlay_id=overlay_id)
        except TypeError:
            best = self.store.best_solution(task_id, total_n, variant=variant)

        def _mass(row: Mapping[str, Any] | None) -> float | None:
            if not row or row.get("actual_mass_kg") is None:
                return None
            try:
                return float(row["actual_mass_kg"])
            except (TypeError, ValueError):
                return None

        previous_mass = _mass(previous_best)
        best_mass = _mass(best)
        best_changed = bool(best) and (
            previous_best is None
            or str(best.get("solution_id")) != str(previous_best.get("solution_id"))
            or best_mass != previous_mass
            or bool(best.get("is_optimal")) != bool(previous_best.get("is_optimal"))
        )
        current_is_best = bool(best) and str(best.get("solution_id")) == str(solution.get("solution_id"))

        if current_is_best and best_changed:
            status = (
                "optimal" if best.get("is_optimal") else
                "feasible" if best.get("is_feasible") else "infeasible"
            )
            query = f"smooth={'true' if variant_is_smooth(variant) else 'false'}&overlay={overlay_id}"
            result_url = f"/v1/tasks/{task_id}/results/{total_n}?{query}"
            # Only direct whole N is the same semantic quantity as a user-requested local N.
            # Aggregate component totals are outputs and must never enter task_n_requests.
            if str(best.get("source", solution.get("source", "components"))) == "whole":
                try:
                    self.store.set_n_status(
                        task_id, total_n, status, variant=variant, overlay_id=overlay_id,
                        solution_id=best["solution_id"], source=best["source"], result_url=result_url,
                    )
                except TypeError:
                    self.store.set_n_status(
                        task_id, total_n, status, variant=variant,
                        solution_id=best["solution_id"], source=best["source"], result_url=result_url,
                    )
            self._publish(task_id, {
                "type": "n_finished", "n": total_n, "status": status,
                "result_url": result_url, "solution_id": best["solution_id"], "source": best["source"],
                "actual_mass_kg": best_mass,
                "variant": variant, "smooth": variant_is_smooth(variant), "overlay_id": overlay_id,
            })
            self._publish(task_id, {
                "type": "result_updated", "n": total_n, "status": status,
                "solution_id": best["solution_id"], "source": best["source"],
                "actual_mass_kg": best_mass, "previous_mass_kg": previous_mass,
                "result_url": result_url, "variant": variant,
                "smooth": variant_is_smooth(variant), "overlay_id": overlay_id,
            })

        if (self.store.get_meta(task_id) or {}).get("validate_results") and solution.get("is_feasible"):
            self.enqueue(JobKind.validate_solution, task_id, {
                "solution_id": solution["solution_id"], "variant": variant, "smooth": variant_is_smooth(variant),
            })

    def _maybe_complete_analysis(self, task_id: str, variant: str, auto_solve: bool) -> bool:
        state_method = getattr(self.store, "analysis_state", None)
        mark_method = getattr(self.store, "mark_analysis_prepared", None)
        if not callable(state_method) or not callable(mark_method):
            return False
        overlay_id = self._current_overlay_id()
        state = state_method(task_id, variant=variant, overlay_id=overlay_id)
        already_prepared = bool(state and str(state.get("preparation_state")) == "prepared")
        field = self._field(task_id, variant)
        component_ids = self._variant_call(self.store.component_ids, task_id, variant=variant)
        available_units = {str(cid) for cid in component_ids}
        expected = list(field.get("expected_solver_units", [cid for cid in component_ids if str(cid) != "whole"]))
        if not {str(cid) for cid in expected}.issubset(available_units):
            return False

        unit_records = []
        for unit in expected:
            record = self._variant_call(self.store.load_component, task_id, unit, variant=variant)
            if not record or str(record.get("max_n_state", "")) not in {"ready", "infeasible"}:
                return False
            unit_records.append(record)

        failed_units = [record for record in unit_records if str(record.get("max_n_state")) == "infeasible"]
        no_demand = not expected or (
            bool(unit_records) and all(
                str(record.get("max_n_state")) == "ready" and int(record.get("max_useful_n") or 0) == 0
                for record in unit_records
            )
        )
        if failed_units or no_demand:
            detail = (
                {
                    "reason": "no_positive_n_required",
                    "detail": "Additional demand is empty; no positive solver N is required",
                    "max_useful_n": 0,
                }
                if no_demand else
                {
                    "reason": "component_preparation_infeasible",
                    "detail": "At least one selected solver unit has no valid preparation/max-N bound",
                    "max_useful_n": None,
                }
            )
            mark_infeasible = getattr(self.store, "mark_analysis_infeasible", None)
            if callable(mark_infeasible):
                mark_infeasible(task_id, variant=variant, overlay_id=overlay_id, detail=detail)
            set_status = getattr(self.store, "set_n_status", None)
            if callable(set_status):
                for n in self._requested_ns(task_id, variant, overlay_id):
                    set_status(task_id, n, "infeasible", variant=variant, overlay_id=overlay_id, **detail)
            self._publish(task_id, {
                "type": "analysis_infeasible", "variant": variant, "overlay_id": overlay_id, **detail,
            })
            return True

        if not already_prepared:
            mark_method(task_id, variant=variant, overlay_id=overlay_id)
            self._publish(task_id, {
                "type": "analysis_prepared", "variant": variant,
                "smooth": variant_is_smooth(variant), "overlay_id": overlay_id,
            })

        meta = self.store.get_meta(task_id) or {}
        scene_task = bool(meta.get("scene_id") and str(meta.get("scene_id")) != str(task_id))
        # New scene tasks schedule each unit immediately when its max-N becomes ready.
        # Historical tasks keep the old final scheduling transition.
        if auto_solve and not scene_task and not already_prepared:
            values = self._requested_ns(task_id, variant, overlay_id)
            if values:
                self.schedule_requested_for_all(
                    task_id, values, smooth=variant_is_smooth(variant),
                    overlay_id=overlay_id, register_request=False,
                )
        return True

    def component_layout_view(self, task_id: str, component_id: Any, n: int, row: Mapping[str, Any], *, variant: str = "raw", overlay_id: int = 0) -> dict[str, Any]:
        token = self._overlay_context.set(normalize_overlay_id(overlay_id))
        try:
            candidate = {"candidate_id": stable_digest({"task": task_id, "component": str(component_id), "n": n, "variant": variant, "overlay_id": overlay_id}),
                         "variant": variant, "overlay_id": overlay_id, "source": "components", "total_N": int(n),
                         "component_ns": {str(component_id): int(n)}, "component_choices": {str(component_id): dict(row)},
                         "anchored_boxes": row.get("anchored_boxes", []), "proxy_mass": row.get("proxy_mass")}
            return self._layout_candidate(task_id, candidate)
        finally:
            self._overlay_context.reset(token)

    def whole_component_info(
        self, task_id: str, *, variant: str = "raw", overlay_id: int | None = 0
    ) -> dict[str, Any]:
        selected_overlay = normalize_overlay_id(overlay_id)
        token = self._overlay_context.set(selected_overlay)
        try:
            record = self._variant_call(self.store.load_component, task_id, WHOLE_COMPONENT_KEY, variant=variant)
            if record is not None:
                return {**dict(record.get("info", record)), "overlay_id": selected_overlay}
            component = self._whole_component(task_id) if variant == "raw" else self._whole_component(task_id, variant)
            info = component_public_info(component, max_useful_n=None, state="available")
            info.update(variant=variant, smooth=variant_is_smooth(variant), overlay_id=selected_overlay)
            return info
        finally:
            self._overlay_context.reset(token)
