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


WHOLE_COMPONENT_ID = -1
WHOLE_COMPONENT_KEY = "whole"


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



class JobKind(str, Enum):
    materialize_source = "materialize_source"
    prepare_field = "prepare_field"
    prepare_component = "prepare_component"
    solve_component = "solve_component"
    fit_component = "fit_component"
    combine_frontiers = "combine_frontiers"
    layout_solution = "layout_solution"
    validate_solution = "validate_solution"
    prepare_whole = "prepare_whole"
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
            for key in ("component_id", "n", "total_n", "solution_id", "source", "frontier_version", "offset", "variant", "overlay_id")
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



def _metric_bar_mass(bars: Sequence[Sequence[float]], diameter: float, density: float) -> float:
    from math import hypot, pi

    area_m2 = pi * (float(diameter) / 1000.0) ** 2 / 4.0
    return float(sum(float(density) * area_m2 * (hypot(b[2] - b[0], b[3] - b[1]) / 1000.0) for b in bars))


def _trim_metric_bars(bars: Sequence[Sequence[float]], bounds: Sequence[float], axis: str) -> list[tuple[float, float, float, float]]:
    x0, y0, x1, y1 = map(float, bounds[:4])
    out: list[tuple[float, float, float, float]] = []
    for raw in bars or []:
        bx0, by0, bx1, by1 = map(float, raw[:4])
        if axis == "y":
            lo, hi = max(min(by0, by1), y0), min(max(by0, by1), y1)
            if hi > lo:
                out.append((bx0, lo, bx1, hi))
        else:
            lo, hi = max(min(bx0, bx1), x0), min(max(bx0, bx1), x1)
            if hi > lo:
                out.append((lo, by0, hi, by1))
    return out


def augment_layout_mass_metrics(layout: Mapping[str, Any], *, steel_density_kg_m3: float = 7850.0) -> dict[str, Any]:
    """Attach pre/post anchorage and clipped/unclipped bar/mass diagnostics.

    Canonical physical layout remains the clipped layout returned by ``layout_rebars``.
    Unclipped metrics extend only supplemental-zone tracks to rectangle bounds; background
    reinforcement remains the same physical clipped background in every total.
    """
    from A101.reinforcement_components import bar_mass_kg

    out = dict(layout or {})
    axis = str(out.get("axis", "y")).lower()
    zones = [dict(row) for row in out.get("zones", []) or []]
    tracks = {int(row["id"]): dict(row) for row in out.get("tracks", []) or [] if row.get("id") is not None}
    background_rows = [row for row in out.get("bars", []) or [] if isinstance(row, Mapping) and row.get("background")]
    background_mass = bar_mass_kg(background_rows, steel_density_kg_m3) if background_rows else 0.0

    totals = {
        "without_anchorage_kg": float(background_mass),
        "with_anchorage_kg": float(background_mass),
        "without_anchorage_unclipped_kg": float(background_mass),
        "with_anchorage_unclipped_kg": float(background_mass),
    }
    additional = {key: 0.0 for key in totals}

    for zone in zones:
        if zone.get("background") or zone.get("class") is None:
            continue
        diameter = float(zone.get("diameter") or 0.0)
        if diameter <= 0:
            continue
        anchored_clipped = tuple(map(float, zone.get("bounds") or zone.get("primary_bounds") or ()))
        fitted = tuple(map(float, zone.get("fitted_bounds") or zone.get("primary_bounds") or anchored_clipped))
        anchored_unclipped = tuple(map(float, zone.get("anchored_bounds_unclipped") or anchored_clipped))
        if len(fitted) != 4 or len(anchored_clipped) != 4 or len(anchored_unclipped) != 4:
            continue

        anchored_clipped_bars = [tuple(map(float, row[:4])) for row in zone.get("bars", []) or []]
        no_anchor_clipped_bars = _trim_metric_bars(anchored_clipped_bars, fitted, axis)

        coords: list[float] = []
        for track_id in zone.get("track_ids", []) or []:
            track = tracks.get(int(track_id))
            if not track:
                continue
            value = track.get("x") if axis == "y" else track.get("y")
            if value is not None:
                coords.append(float(value))
        if not coords:
            coords = sorted({float(row[0] if axis == "y" else row[1]) for row in anchored_clipped_bars})
        else:
            coords = sorted(set(coords))

        if axis == "y":
            no_anchor_unclipped_bars = [(x, fitted[1], x, fitted[3]) for x in coords]
            anchored_unclipped_bars = [(x, anchored_unclipped[1], x, anchored_unclipped[3]) for x in coords]
        else:
            no_anchor_unclipped_bars = [(fitted[0], y, fitted[2], y) for y in coords]
            anchored_unclipped_bars = [(anchored_unclipped[0], y, anchored_unclipped[2], y) for y in coords]

        masses = {
            "without_anchorage_kg": _metric_bar_mass(no_anchor_clipped_bars, diameter, steel_density_kg_m3),
            "with_anchorage_kg": _metric_bar_mass(anchored_clipped_bars, diameter, steel_density_kg_m3),
            "without_anchorage_unclipped_kg": _metric_bar_mass(no_anchor_unclipped_bars, diameter, steel_density_kg_m3),
            "with_anchorage_unclipped_kg": _metric_bar_mass(anchored_unclipped_bars, diameter, steel_density_kg_m3),
        }
        for key, value in masses.items():
            totals[key] += value
            additional[key] += value

        zone.update({
            "final_rectangle_without_anchorage": fitted,
            "final_rectangle_with_anchorage": anchored_clipped,
            "final_rectangle_without_anchorage_unclipped": fitted,
            "final_rectangle_with_anchorage_unclipped": anchored_unclipped,
            "bars_without_anchorage": no_anchor_clipped_bars,
            "bars_with_anchorage": anchored_clipped_bars,
            "bars_without_anchorage_unclipped": no_anchor_unclipped_bars,
            "bars_with_anchorage_unclipped": anchored_unclipped_bars,
            "zone_mass_without_anchorage_kg": masses["without_anchorage_kg"],
            "zone_mass_with_anchorage_kg": masses["with_anchorage_kg"],
            "zone_mass_without_anchorage_unclipped_kg": masses["without_anchorage_unclipped_kg"],
            "zone_mass_with_anchorage_unclipped_kg": masses["with_anchorage_unclipped_kg"],
        })

    out["zones"] = zones
    # Preserve exact canonical physical mass as a consistency guard.
    canonical = bar_mass_kg(out.get("bars", []) or [], steel_density_kg_m3) if out.get("bars") else 0.0
    totals["with_anchorage_kg"] = float(canonical)
    out["mass_metrics"] = {
        **{key: float(value) for key, value in totals.items()},
        "anchorage_kg": float(totals["with_anchorage_kg"] - totals["without_anchorage_kg"]),
        "anchorage_unclipped_kg": float(totals["with_anchorage_unclipped_kg"] - totals["without_anchorage_unclipped_kg"]),
        "additional": {key: float(value) for key, value in additional.items()},
        "background_clipped_kg": float(background_mass),
    }
    return out


def _compatibility_zones(layout: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    fit_zones: list[dict[str, Any]] = []
    summary_zones: list[dict[str, Any]] = []
    for zone in layout.get("zones", []) or []:
        if zone.get("background") or zone.get("class") is None:
            continue
        anchored_bounds = tuple(map(float, zone.get("final_rectangle_with_anchorage") or zone.get("bounds") or zone.get("primary_bounds") or ()))
        if len(anchored_bounds) != 4:
            continue
        no_anchor_bounds = tuple(map(float, zone.get("final_rectangle_without_anchorage") or zone.get("fitted_bounds") or zone.get("primary_bounds") or anchored_bounds))
        no_anchor_unclipped = tuple(map(float, zone.get("final_rectangle_without_anchorage_unclipped") or no_anchor_bounds))
        anchored_unclipped = tuple(map(float, zone.get("final_rectangle_with_anchorage_unclipped") or zone.get("anchored_bounds_unclipped") or anchored_bounds))
        primary = tuple(map(float, zone.get("primary_bounds") or no_anchor_bounds))
        cls = int(zone["class"])
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
        method = self.store.is_n_cancelled
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

    def materialize_task_source(self, task_id: str, *, continue_pipeline: bool, smooth: bool = False) -> bool:
        return self.enqueue(
            JobKind.materialize_source,
            task_id,
            {"continue_pipeline": bool(continue_pipeline), "smooth": bool(smooth), "variant": analysis_variant(smooth)},
        )

    def handle_materialize_source(self, job: PipelineJob) -> None:
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
            self.prepare_task(job.task_id, auto_solve=True, smooth=smooth)

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
        anchor_factor = float(params.get("anchor_factor", 32.0))
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
        if prepared_max_n in (0, "0"):
            prepared_max_n = None
        if prepared_max_n is not None:
            prepared_max_n = min(int(prepared_max_n), self.settings.max_n_value)
        problem = prepare_component_problem(
            component,
            load2cls=cfg["load2cls"],
            recipes=cfg.get("recipes"),
            densities=cfg["densities"],
            diameters=cfg["diameters"],
            anchor_factor=float(params.get("anchor_factor", 32.0)),
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
        )
        bounds = component_n_bounds(problem["prepared"], cap=prepared_max_n or self.settings.max_n_value)
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

    def handle_prepare_component(self, job: PipelineJob) -> None:
        task_id, cid = job.task_id, int(job.payload["component_id"])
        variant = payload_variant(job.payload)
        record = self._variant_call(self.store.load_component, task_id, cid, variant=variant)
        if record is None:
            raise KeyError(f"component {cid} variant={variant} not found")
        if job.payload.get("schedule_only"):
            self._schedule_plan(
                task_id, cid, list(record.get("plan", [])), bool(record.get("force_single_box")),
                int(job.payload.get("start", 0)), variant=variant,
            )
            return
        prepared = self._prepare_problem(task_id, cid, record["component"]) if variant == "raw" else self._prepare_problem(task_id, cid, record["component"], variant=variant)
        meta = self.store.get_meta(task_id) or {}
        plan, force_single = choose_component_ns(
            self._requested_ns(task_id, variant) or [1], prepared["max_useful_n"], str(meta.get("scan_mode")) == "hard"
        )
        record.update(
            state="prepared", plan=plan, force_single_box=force_single,
            max_useful_n=prepared["max_useful_n"], bounds=prepared["bounds"],
            info=component_public_info(record["component"], prepared["max_useful_n"], "prepared"),
            variant=variant, smooth=variant_is_smooth(variant),
        )
        self._variant_call(self.store.save_component, task_id, cid, record, variant=variant)
        self._publish(task_id, {
            "type": "component_prepared", "component_id": cid, "max_useful_n": prepared["max_useful_n"],
            "planned_n": plan, "fallback_single_box": force_single, "variant": variant,
            "smooth": variant_is_smooth(variant),
        })
        if bool(job.payload.get("auto_solve", True)):
            self._schedule_plan(task_id, cid, plan, force_single, variant=variant)
        self._maybe_complete_analysis(
            task_id, variant, bool(job.payload.get("analysis_auto_solve", job.payload.get("auto_solve", True)))
        )

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
            anchor_factor=float(params.get("anchor_factor", 32.0)), axis=cfg["axis"], field=field["field_geometry"],
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
        if n == 1 or job.payload.get("force_single_box"):
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
            diameters=cfg["diameters"], steps=cfg["steps"], anchor_factor=float(params.get("anchor_factor", 32.0)),
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
            raise ValueError("В whole field нет дополнительного армирования")
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
        self._variant_call(
            self.store.save_component, task_id, "whole",
            {"component": component, "state": "preparing", "info": component_public_info(component), "variant": variant},
            variant=variant,
        )
        prepared = self._prepare_problem(task_id, "whole", component) if variant == "raw" else self._prepare_problem(task_id, "whole", component, variant=variant)
        meta = self.store.get_meta(task_id) or {}
        explicit_requested = job.payload.get("requested_n")
        invalid: list[int] = []
        if explicit_requested is not None:
            plan = list(dict.fromkeys(int(n) for n in explicit_requested))
            invalid = [n for n in plan if n < 1 or n > int(prepared["max_useful_n"])]
            force_single = False
        else:
            plan, force_single = choose_component_ns(
                self._requested_ns(task_id, variant) or [1], prepared["max_useful_n"], str(meta.get("scan_mode")) == "hard"
            )
        record = {
            "component": component, "state": "prepared", "plan": plan, "force_single_box": force_single,
            "max_useful_n": prepared["max_useful_n"], "bounds": prepared["bounds"],
            "info": component_public_info(component, prepared["max_useful_n"], "prepared"),
            "variant": variant, "smooth": variant_is_smooth(variant),
        }
        self._variant_call(self.store.save_component, task_id, "whole", record, variant=variant)
        if invalid:
            raise ValueError(f"n вне допустимого диапазона 1..{prepared['max_useful_n']}: {invalid}")
        if bool(job.payload.get("auto_solve", True)):
            self._schedule_plan(task_id, "whole", plan, force_single, whole=True, variant=variant)
        self._maybe_complete_analysis(
            task_id, variant, bool(job.payload.get("analysis_auto_solve", job.payload.get("auto_solve", True)))
        )

    def handle_solve_whole(self, job: PipelineJob) -> None:
        from A101.reinforcement_components import solve_component_frontier

        task_id = job.task_id
        variant = payload_variant(job.payload)
        n = int(job.payload["n"])
        if n == 1 or job.payload.get("force_single_box"):
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
            diameters=cfg["diameters"], steps=cfg["steps"], anchor_factor=float(params.get("anchor_factor", 32.0)),
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
        invalid_basic = [n for n in requested if n < 1 or n > int(self.settings.max_n_value)]
        if invalid_basic:
            raise ValueError(f"n вне допустимого диапазона 1..{self.settings.max_n_value}: {invalid_basic}")
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
        method = self.store.is_n_cancelled
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

    def schedule_requested_for_all(
        self, task_id: str, values: Sequence[int], *, smooth: bool = False, overlay_id: int | None = 0
    ) -> dict[str, list[int]]:
        variant = analysis_variant(smooth)
        selected_overlay = normalize_overlay_id(overlay_id)
        requested = list(dict.fromkeys(int(n) for n in values))
        invalid = [n for n in requested if n < 1 or n > int(self.settings.max_n_value)]
        if invalid:
            raise ValueError(f"n вне допустимого диапазона 1..{self.settings.max_n_value}: {invalid}")

        add_requested = getattr(self.store, "add_requested_ns", None)
        if callable(add_requested):
            try:
                add_requested(task_id, requested, variant=variant, overlay_id=selected_overlay)
            except TypeError:
                try:
                    add_requested(task_id, requested, variant=variant)
                except TypeError:
                    add_requested(task_id, requested)

        state_method = getattr(self.store, "analysis_state", None)
        if callable(state_method):
            ensure = getattr(self.store, "ensure_analysis", None)
            if callable(ensure):
                ensure(task_id, variant=variant, overlay_id=selected_overlay)
            state = state_method(task_id, variant=variant, overlay_id=selected_overlay) or {}
            if str(state.get("preparation_state", "stored")) != "prepared":
                self.prepare_task(task_id, auto_solve=True, smooth=smooth, overlay_id=selected_overlay)
                return {"preparing": requested}

        context = getattr(self, "_overlay_context", None)
        token = context.set(selected_overlay) if context is not None else None
        try:
            queued: dict[str, list[int]] = {}
            for cid in self._variant_call(self.store.component_ids, task_id, variant=variant):
                if cid == "whole":
                    continue
                record = self._variant_call(self.store.load_component, task_id, cid, variant=variant) or {}
                max_n = record.get("max_useful_n")
                if not max_n:
                    continue
                hard = str((self.store.get_meta(task_id) or {}).get("scan_mode", "requested")) == "hard"
                plan, fallback = choose_component_ns(requested, int(max_n), hard)
                queued[str(cid)] = plan
                for n in plan:
                    self.enqueue(JobKind.solve_component, task_id, {
                        "component_id": int(cid), "n": int(n), "force_single_box": bool(fallback and int(n) == 1),
                        "source": "components", "variant": variant, "smooth": smooth,
                    })
            whole = self._variant_call(self.store.load_component, task_id, "whole", variant=variant)
            if whole and whole.get("max_useful_n"):
                hard = str((self.store.get_meta(task_id) or {}).get("scan_mode", "requested")) == "hard"
                plan, fallback = choose_component_ns(requested, int(whole["max_useful_n"]), hard)
                queued["whole"] = plan
                for n in plan:
                    self.enqueue(JobKind.solve_whole, task_id, {
                        "component_id": "whole", "n": int(n), "force_single_box": bool(fallback and int(n) == 1),
                        "source": "whole", "variant": variant, "smooth": smooth,
                    })
            return queued
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
            storage_id = component_storage_id(component_id)
            requested = list(dict.fromkeys(int(n) for n in values))
            invalid_basic = [n for n in requested if n < 1 or n > int(self.settings.max_n_value)]
            if invalid_basic:
                raise ValueError(f"n вне допустимого диапазона 1..{self.settings.max_n_value}: {invalid_basic}")
            record = self._variant_call(self.store.load_component, task_id, storage_id, variant=variant)
            if storage_id == WHOLE_COMPONENT_KEY and (record is None or not record.get("max_useful_n")):
                if not self._variant_call(self.store.load_field, task_id, variant=variant):
                    raise AnalysisNotPreparedError(
                        "Analysis is not prepared; schedule N through /v1/tasks/{task_id}/n first"
                    )
                self.enqueue(
                    JobKind.prepare_whole, task_id,
                    {"auto_solve": True, "requested_n": requested, "variant": variant, "smooth": smooth},
                    dedupe_key=(
                        f"prepare-whole-explicit:{task_id}:{variant}:{selected_overlay}:"
                        f"{self.store.generation(task_id)}:{stable_digest(requested)}"
                    ),
                )
                return requested
            if record is None or not record.get("max_useful_n"):
                raise KeyError(f"component variant={variant} overlay={selected_overlay} не подготовлена")
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
        finally:
            if context is not None and token is not None:
                context.reset(token)

    def dispatch(self, job: PipelineJob) -> None:
        if int(job.generation) != self.store.generation(job.task_id):
            return
        handler = getattr(self, "handle_" + str(job.kind), None)
        if handler is None:
            raise ValueError(f"Неизвестный job kind: {job.kind}")
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
        anchor_factor = float(params.get("anchor_factor", 32.0))
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
        self._variant_call(self.store.save_field, task_id, field, variant=variant)

        components = list(split.get("components", []))
        for component in components:
            cid = int(component["id"])
            self._variant_call(
                self.store.save_component, task_id, cid,
                {"component": component, "info": component_public_info(component), "state": "queued", "variant": variant,
                 "overlay_id": overlay_id},
                variant=variant,
            )
            payload = {"component_id": cid, "auto_solve": False, "analysis_auto_solve": auto_solve, "variant": variant, "smooth": smooth}
            self.enqueue(JobKind.prepare_component, task_id, payload)

        meta = self.store.get_meta(task_id) or {}
        prepare_whole = bool(meta.get("whole", False))
        if prepare_whole:
            self.enqueue(JobKind.prepare_whole, task_id, {"auto_solve": False, "analysis_auto_solve": auto_solve, "variant": variant, "smooth": smooth})

        self.store.patch_meta(task_id, state="components_ready")
        self._publish(task_id, {
            "type": "components_ready", "components": [component_public_info(c) for c in components],
            "active_indices": split.get("active_indices", []),
            "background_only_indices": split.get("background_only_indices", []),
            "removed_indices": split.get("removed_indices", []),
            "degenerate_indices": split.get("degenerate_indices", []),
            "variant": variant, "smooth": smooth, "overlay_id": overlay_id,
        })

        # Empty analyses have no component prepare jobs that could close preparation.
        if not components and not prepare_whole:
            mark = getattr(self.store, "mark_analysis_prepared", None)
            if callable(mark):
                mark(task_id, variant=variant, overlay_id=overlay_id)
            if auto_solve:
                self.schedule_requested_for_all(
                    task_id, self._requested_ns(task_id, variant, overlay_id), smooth=smooth, overlay_id=overlay_id
                )

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
        status = "optimal" if is_optimal else "feasible" if is_feasible else str(result.get("solve_state", "failed"))
        self._publish(task_id, {
            "type": "component_fit_finished", "component_id": cid, "n": int(n), "status": status,
            "variant": variant, "smooth": variant_is_smooth(variant), "overlay_id": overlay_id,
        })
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
        current_version = self._variant_call(self.store.frontier_version, task_id, variant=variant)
        requested_version = int(job.payload.get("frontier_version", current_version))
        if requested_version < current_version and int(job.payload.get("offset", 0)) == 0:
            return
        component_ids = [
            cid for cid in self._variant_call(self.store.component_ids, task_id, variant=variant) if cid != "whole"
        ]
        frontiers = self._variant_call(self.store.all_frontiers, task_id, variant=variant)
        if not component_ids or any(
            (int(cid) if str(cid).lstrip("-").isdigit() else cid) not in frontiers for cid in component_ids
        ):
            return
        if any(
            not any(row.get("is_feasible") for row in frontiers[int(cid) if str(cid).lstrip("-").isdigit() else cid].values())
            for cid in component_ids
        ):
            return
        meta = self.store.get_meta(task_id) or {}
        top_k = int(meta.get("component_result_top_k", self.settings.frontier_top_k))
        combined = combine_component_frontiers(frontiers, top_k=top_k)
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
            layout = augment_layout_mass_metrics(layout, steel_density_kg_m3=density)
            mass = float(layout["mass_metrics"]["with_anchorage_kg"])
        else:
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
        try:
            candidate = self.store.load_candidate(task_id, str(job.payload["candidate_id"]), overlay_id=overlay_id)
        except TypeError:
            candidate = self.store.load_candidate(task_id, str(job.payload["candidate_id"]))
        if candidate is None:
            raise KeyError("candidate missing")
        solution = self._layout_candidate(task_id, candidate)
        variant = str(solution.get("variant", "raw"))
        self.store.save_solution(task_id, solution)
        solution_url = f"/v1/tasks/{task_id}/solutions/{solution['solution_id']}?overlay={overlay_id}"
        self._publish(task_id, {
            "type": "solution_available", "solution_id": solution["solution_id"], "source": solution["source"],
            "total_N": solution["total_N"], "is_feasible": solution["is_feasible"],
            "is_optimal": solution.get("is_optimal", False), "status": solution.get("status"),
            "actual_mass_kg": solution["actual_mass_kg"], "result_url": solution_url,
            "variant": variant, "smooth": variant_is_smooth(variant), "overlay_id": overlay_id,
        })
        try:
            best = self.store.best_solution(task_id, solution["total_N"], variant=variant, overlay_id=overlay_id)
        except TypeError:
            best = self.store.best_solution(task_id, solution["total_N"], variant=variant)
        if best and best.get("solution_id") == solution["solution_id"]:
            status = (
                "optimal" if solution.get("is_optimal") else
                "feasible" if solution.get("is_feasible") else "infeasible"
            )
            query = f"smooth={'true' if variant_is_smooth(variant) else 'false'}&overlay={overlay_id}"
            result_url = f"/v1/tasks/{task_id}/results/{solution['total_N']}?{query}"
            try:
                self.store.set_n_status(
                    task_id, solution["total_N"], status, variant=variant, overlay_id=overlay_id,
                    solution_id=solution["solution_id"], source=solution["source"], result_url=result_url,
                )
            except TypeError:
                self.store.set_n_status(
                    task_id, solution["total_N"], status, variant=variant,
                    solution_id=solution["solution_id"], source=solution["source"], result_url=result_url,
                )
            self._publish(task_id, {
                "type": "n_finished", "n": solution["total_N"], "status": status,
                "result_url": result_url, "solution_id": solution["solution_id"], "source": solution["source"],
                "variant": variant, "smooth": variant_is_smooth(variant), "overlay_id": overlay_id,
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
        if state and str(state.get("preparation_state")) == "prepared":
            return True
        self._field(task_id, variant)
        component_ids = self._variant_call(self.store.component_ids, task_id, variant=variant)
        expected = [int(cid) for cid in component_ids if str(cid) != "whole"]
        for cid in expected:
            record = self._variant_call(self.store.load_component, task_id, cid, variant=variant)
            if not record or not record.get("max_useful_n"):
                return False
        meta = self.store.get_meta(task_id) or {}
        if bool(meta.get("whole", False)):
            whole = self._variant_call(self.store.load_component, task_id, "whole", variant=variant)
            if not whole or not whole.get("max_useful_n"):
                return False
        mark_method(task_id, variant=variant, overlay_id=overlay_id)
        self._publish(task_id, {
            "type": "analysis_prepared", "variant": variant, "smooth": variant_is_smooth(variant),
            "overlay_id": overlay_id,
        })
        if auto_solve:
            values = self._requested_ns(task_id, variant, overlay_id)
            if values:
                self.schedule_requested_for_all(
                    task_id, values, smooth=variant_is_smooth(variant), overlay_id=overlay_id
                )
        return True

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
