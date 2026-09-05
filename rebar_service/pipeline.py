from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5

import numpy as np
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from .config import Settings
from .store import RedisStore


class JobKind(str, Enum):
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
            for key in ("component_id", "n", "total_n", "solution_id", "source", "frontier_version", "offset")
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
        if zone.get("background") or zone.get("class") is None:
            continue
        bounds = tuple(map(float, zone.get("bounds") or zone.get("primary_bounds") or ()))
        if len(bounds) != 4:
            continue
        primary = tuple(map(float, zone.get("primary_bounds") or bounds))
        cls = int(zone["class"])
        bars = [tuple(map(float, row[:4])) for row in zone.get("bars", []) or []]
        fit_zones.append({
            "id": zone.get("id"),
            "class": cls,
            "bounds": bounds,
            "diameter": zone.get("diameter"),
            "step": zone.get("step"),
            "bars": bars,
        })
        summary_zones.append({
            "class": cls,
            "diameter": zone.get("diameter"),
            "step": zone.get("step"),
            "primary rectangle": primary,
            "final rectangle": bounds,
            # The physical global layout already contains the final anchored bar geometry.
            "final rectangle with anchorage": bounds,
            "bars": bars,
            "bars with anchorage": bars,
            "width": abs(bounds[2] - bounds[0]),
            "length": abs(bounds[3] - bounds[1]),
        })
    return fit_zones, summary_zones


def to_compat_result(solution: Mapping[str, Any]) -> dict[str, Any]:
    """Return the historical result shape, backed by the current component solution."""
    row = dict(solution)
    choices = row.get("component_choices", {}) or {}
    solver_results = {
        str(cid): dict(value.get("solver_result", {}) or {})
        for cid, value in choices.items()
        if isinstance(value, Mapping)
    }
    fit_results = {
        str(cid): dict(value.get("fit_result", {}) or {})
        for cid, value in choices.items()
        if isinstance(value, Mapping)
    }
    rectangles = list(row.get("rectangles", []) or [])
    anchored = list(row.get("anchored_boxes", []) or [])
    layout = dict(row.get("bar_layout", {}) or {})
    fit_zones, summary_zones = _compatibility_zones(layout)
    actual_mass = row.get("actual_mass_kg")
    proxy_mass = row.get("proxy_mass")
    feasible = bool(row.get("is_feasible"))
    total_n = int(row.get("total_N", 0))
    return {
        "is_feasible": feasible,
        "is_optimal": bool(row.get("is_optimal", False)),
        "status": row.get("status", "feasible" if feasible else "infeasible"),
        "total_cost": proxy_mass,
        "solver_result": {
            "is_feasible": feasible,
            "component_results": solver_results,
            "component_ns": dict(row.get("component_ns", {}) or {}),
        },
        "primary_rectangles": rectangles or anchored,
        "rectangles": rectangles,
        "anchored_boxes": anchored,
        "fit_result": {
            "is_feasible": feasible,
            "component_results": fit_results,
            "rectangles": rectangles,
            "zones": fit_zones,
        },
        "summary": {
            "mass": actual_mass,
            "mass_kg": actual_mass,
            "mass with anchorage": actual_mass,
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
        "bar_layout": layout,
        "metadata": dict(row.get("metadata", {}) or {}),
    }


class PipelineWorkflow:
    """The only production calculation workflow used by the API and workers."""

    def __init__(self, store: RedisStore, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    def _publish(self, task_id: str, event: Mapping[str, Any]) -> str:
        row = dict(event)
        event_type = str(row.pop("type"))
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
        job = PipelineJob(
            kind.value if isinstance(kind, JobKind) else str(kind),
            task_id,
            dict(payload or {}),
            generation=generation,
            dedupe_key=dedupe_key,
        )
        return self.store.enqueue_pipeline_job(job.to_dict())

    def bootstrap_task(self, task_id: str) -> bool:
        meta = self.store.get_meta(task_id)
        if meta is None:
            raise KeyError(task_id)
        self.store.patch_meta(task_id, state="queued_preparation", generation=self.store.generation(task_id))
        self._publish(
            task_id,
            {
                "type": "pipeline_queued",
                "requested_n": list(meta.get("requested_n", [])),
                "scan_mode": meta.get("scan_mode", "requested"),
                "whole": bool(meta.get("whole", False)),
            },
        )
        return self.enqueue(JobKind.prepare_field, task_id)

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

    def _field(self, task_id: str) -> dict[str, Any]:
        field = self.store.load_field(task_id)
        if not field:
            raise KeyError(f"field не подготовлен для task {task_id}")
        return field

    def handle_prepare_field(self, job: PipelineJob) -> None:
        from A101.axis_orientation import class_holds, normalize_axis
        from A101.calculate_mass import make_rebar_classes
        from A101.grid_work import clean_poly
        from A101.poly_bbox import rect_polygons
        from A101.reinforcement_components import split_reinforcement_components

        task_id = job.task_id
        self.store.patch_meta(task_id, state="preparing_components")
        self._publish(task_id, {"type": "component_prepare_started"})
        input_obj = self.store.get_object(task_id, "input")
        params = self._params(task_id)
        polygons = polygons_from_input(input_obj)
        loads = sorted({float(row["load"]) for row in polygons})
        cfg = make_rebar_classes(
            loads,
            tuple(params.get("back_grid", (18, 300))),
            [tuple(row) for row in params.get("stock", [])],
            max_lay=int(params.get("max_layers", 2)),
        )
        cfg["back_grid"] = tuple(params.get("back_grid", (18, 300)))
        cfg["stock"] = [tuple(row) for row in params.get("stock", [])]
        axis = normalize_axis(str(params.get("axis", "y")))
        cfg["axis"] = axis
        anchor_factor = float(params.get("anchor_factor", 32.0))
        base_holds, holds, leaves = class_holds(cfg["diameters"], cfg.get("recipes"), anchor_factor)
        cfg.update(base_holds=base_holds, holds=holds, recipe_leaves=leaves)
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
        }
        self.store.save_field(task_id, field)
        for component in split.get("components", []):
            cid = int(component["id"])
            self.store.save_component(
                task_id,
                cid,
                {"component": component, "info": component_public_info(component), "state": "queued"},
            )
            self.enqueue(JobKind.prepare_component, task_id, {"component_id": cid})
        if bool((self.store.get_meta(task_id) or {}).get("whole")):
            self.enqueue(JobKind.prepare_whole, task_id)
        self.store.patch_meta(task_id, state="components_ready", component_count=len(split.get("components", [])))
        self._publish(
            task_id,
            {
                "type": "components_ready",
                "components": [component_public_info(c) for c in split.get("components", [])],
                "active_indices": split.get("active_indices", []),
                "background_only_indices": split.get("background_only_indices", []),
                "degenerate_indices": split.get("degenerate_indices", []),
            },
        )

    def _prepare_problem(self, task_id: str, component_id: Any, component: Mapping[str, Any]) -> dict[str, Any]:
        from A101.reinforcement_components import component_n_bounds, prepare_component_problem

        field = self._field(task_id)
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
        stored = {"problem": problem, "bounds": bounds, "max_useful_n": max_useful}
        self.store.save_problem(task_id, component_id, stored)
        return stored

    def _schedule_plan(
        self,
        task_id: str,
        component_id: Any,
        plan: Sequence[int],
        force_single: bool,
        start: int = 0,
        whole: bool = False,
    ) -> None:
        batch = max(1, int(self.settings.scheduler_batch_size))
        end = min(len(plan), int(start) + batch)
        kind = JobKind.solve_whole if whole else JobKind.solve_component
        for n in plan[int(start) : end]:
            if self.store.is_n_cancelled(task_id, int(n)):
                continue
            self.enqueue(
                kind,
                task_id,
                {
                    "component_id": component_id,
                    "n": int(n),
                    "force_single_box": bool(force_single and int(n) == 1),
                    "source": "whole" if whole else "components",
                },
            )
        if end < len(plan):
            self.enqueue(
                JobKind.prepare_whole if whole else JobKind.prepare_component,
                task_id,
                {"component_id": component_id, "schedule_only": True, "start": end},
                dedupe_key=f"schedule:{task_id}:{component_id}:{end}:{self.store.generation(task_id)}",
            )

    def handle_prepare_component(self, job: PipelineJob) -> None:
        task_id, cid = job.task_id, int(job.payload["component_id"])
        record = self.store.load_component(task_id, cid)
        if record is None:
            raise KeyError(f"component {cid} not found")
        if job.payload.get("schedule_only"):
            self._schedule_plan(
                task_id,
                cid,
                list(record.get("plan", [])),
                bool(record.get("force_single_box")),
                int(job.payload.get("start", 0)),
            )
            return
        prepared = self._prepare_problem(task_id, cid, record["component"])
        meta = self.store.get_meta(task_id) or {}
        plan, force_single = choose_component_ns(
            meta.get("requested_n", [1]),
            prepared["max_useful_n"],
            str(meta.get("scan_mode")) == "hard",
        )
        record.update(
            state="prepared",
            plan=plan,
            force_single_box=force_single,
            max_useful_n=prepared["max_useful_n"],
            bounds=prepared["bounds"],
            info=component_public_info(record["component"], prepared["max_useful_n"], "prepared"),
        )
        self.store.save_component(task_id, cid, record)
        self._publish(
            task_id,
            {
                "type": "component_prepared",
                "component_id": cid,
                "max_useful_n": prepared["max_useful_n"],
                "planned_n": plan,
                "fallback_single_box": force_single,
            },
        )
        self._schedule_plan(task_id, cid, plan, force_single)

    def _single_component_frontier(self, task_id: str, component_id: Any, source: str = "components") -> dict[str, Any]:
        from A101.axis_orientation import add_box_anchorage
        from A101.fit_box_layout import fit_box_layout

        field = self._field(task_id)
        cfg = field["cfg"]
        params = self._params(task_id)
        stored = self.store.load_problem(task_id, component_id)
        if not stored:
            raise KeyError("component problem missing")
        problem = stored["problem"]
        component = problem.get("component", {})
        bounds = tuple(
            map(
                float,
                component.get("demand_bounds")
                or component.get("bounds")
                or problem["component"]["geometry"].bounds,
            )
        )
        cls = max(map(int, component.get("classes", [1])))
        fitted = fit_box_layout(
            problem["poly_mos"],
            [(*bounds, cls)],
            recipes=cfg.get("recipes"),
            densities=cfg["densities"],
            min_w=float(params.get("min_width_mm", 1000.0)),
            time_limit=self.settings.fit_time_limit,
            allow_class_upgrade=True,
            milp_backend=self.settings.fit_milp_backend,
            threads=self.settings.effective_threads(self._solver(task_id).get("threads")),
        )
        if not fitted.get("is_feasible"):
            raise RuntimeError(f"single component box fit failed: {fitted.get('errors')}")
        anchored = add_box_anchorage(
            fitted,
            recipes=cfg.get("recipes"),
            diameters=cfg["diameters"],
            steps=cfg["steps"],
            anchor_factor=float(params.get("anchor_factor", 32.0)),
            axis=cfg["axis"],
            field=field["field_geometry"],
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
        return {
            "n": 1,
            "component_id": component_id,
            "is_feasible": True,
            "is_optimal": False,
            "solve_state": "feasible",
            "solver_result": {
                "is_feasible": True,
                "fallback": "single_component_box",
                "rectangles": [(*bounds, cls)],
            },
            "rectangles": [(*bounds, cls)],
            "fit_result": fitted,
            "class_changes": list(fitted.get("class_changes", [])),
            "anchored_boxes": anchored,
            "proxy_mass": float(proxy_mass),
            "source": source,
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
        if self.store.is_n_cancelled(task_id, n):
            self._publish(task_id, {"type": "component_n_cancelled", "component_id": cid, "n": n})
            return
        if job.payload.get("force_single_box"):
            result = self._single_component_frontier(task_id, cid)
            self.store.save_frontier_result(task_id, cid, 1, result)
            self._frontier_ready(task_id, cid, 1, result)
            return
        stored = self.store.load_problem(task_id, cid)
        if not stored:
            raise KeyError("problem missing")
        _, threads, timeout, time_limit, backend, require_optimal = self._solver_options(task_id)
        results, data = solve_component_frontier(
            stored["problem"],
            [n],
            data={},
            timeout=timeout,
            solver_time_limit=time_limit,
            threads=threads,
            backend=backend,
            require_optimal=require_optimal,
            return_best_on_timeout=True,
            raise_errors=False,
        )
        self.store.save_solver_result(task_id, cid, n, {"results": results, "data": data, "threads": threads})
        self.enqueue(JobKind.fit_component, task_id, {"component_id": cid, "n": n, "source": "components"})
        self._publish(task_id, {"type": "component_n_finished", "component_id": cid, "n": n, "stage": "solver"})

    def handle_fit_component(self, job: PipelineJob) -> None:
        from A101.reinforcement_components import fit_component_frontier

        task_id, cid, n = job.task_id, int(job.payload["component_id"]), int(job.payload["n"])
        stored = self.store.load_problem(task_id, cid)
        solved = self.store.load_solver_result(task_id, cid, n)
        if not stored or not solved:
            raise KeyError("problem/solver result missing")
        field = self._field(task_id)
        cfg = field["cfg"]
        params = self._params(task_id)
        threads = self.settings.effective_threads(self._solver(task_id).get("threads"))
        frontier = fit_component_frontier(
            stored["problem"],
            solved["results"],
            recipes=cfg.get("recipes"),
            densities=cfg["densities"],
            diameters=cfg["diameters"],
            steps=cfg["steps"],
            anchor_factor=float(params.get("anchor_factor", 32.0)),
            axis=cfg["axis"],
            field=field["field_geometry"],
            min_width=float(params.get("min_width_mm", 1000.0)),
            time_limit=self.settings.fit_time_limit,
            allow_class_upgrade=True,
            fit_milp_backend=self.settings.fit_milp_backend,
            fit_threads=threads,
        )
        result = dict(frontier.get(n, {"n": n, "is_feasible": False, "error": "fit result missing"}))
        self.store.save_frontier_result(task_id, cid, n, result)
        self._frontier_ready(task_id, cid, n, result)

    def _frontier_ready(self, task_id: str, cid: Any, n: int, result: Mapping[str, Any]) -> None:
        self._publish(
            task_id,
            {
                "type": "component_fit_finished",
                "component_id": cid,
                "n": int(n),
                "status": "feasible" if result.get("is_feasible") else result.get("solve_state", "failed"),
            },
        )
        version = self.store.frontier_version(task_id)
        self.enqueue(
            JobKind.combine_frontiers,
            task_id,
            {"frontier_version": version, "offset": 0},
            dedupe_key=f"combine:{task_id}:{self.store.generation(task_id)}:{version}",
        )

    def handle_combine_frontiers(self, job: PipelineJob) -> None:
        from A101.reinforcement_components import combine_component_frontiers

        task_id = job.task_id
        current_version = self.store.frontier_version(task_id)
        requested_version = int(job.payload.get("frontier_version", current_version))
        if requested_version < current_version and int(job.payload.get("offset", 0)) == 0:
            return
        component_ids = [cid for cid in self.store.component_ids(task_id) if cid != "whole"]
        frontiers = self.store.all_frontiers(task_id)
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
        flat = [
            (int(total_n), rank, candidate)
            for total_n, rows in sorted(combined.items())
            for rank, candidate in enumerate(rows)
        ]
        start = int(job.payload.get("offset", 0))
        end = min(len(flat), start + max(1, int(self.settings.combine_batch_size)))
        queued = 0
        for total_n, _rank, candidate in flat[start:end]:
            candidate_id = stable_digest(
                {
                    "task_id": task_id,
                    "source": "components",
                    "total_n": total_n,
                    "component_ns": candidate.get("component_ns", {}),
                }
            )
            candidate = {
                **dict(candidate),
                "candidate_id": candidate_id,
                "source": "components",
                "total_N": total_n,
            }
            self.store.save_candidate(task_id, candidate_id, candidate)
            queued += int(
                self.enqueue(
                    JobKind.layout_solution,
                    task_id,
                    {"candidate_id": candidate_id, "total_n": total_n, "source": "components"},
                )
            )
        if end < len(flat):
            self.enqueue(
                JobKind.combine_frontiers,
                task_id,
                {"frontier_version": current_version, "offset": end},
                dedupe_key=f"combine:{task_id}:{self.store.generation(task_id)}:{current_version}:{end}",
            )
        self._publish(
            task_id,
            {
                "type": "frontier_updated",
                "frontier_version": current_version,
                "total_n": sorted(map(int, combined)),
                "queued_solutions": queued,
            },
        )

    def _layout_candidate(self, task_id: str, candidate: Mapping[str, Any]) -> dict[str, Any]:
        from A101.rebar_field_layout import layout_rebars
        from A101.reinforcement_components import bar_mass_kg

        field = self._field(task_id)
        cfg = field["cfg"]
        params = self._params(task_id)
        layout = dict(
            layout_rebars(
                polygons=[p["geometry"] for p in field["start_polygons"]],
                boxes=candidate.get("anchored_boxes", []),
                background=tuple(params.get("back_grid", cfg.get("back_grid", (18, 300)))),
                axis=cfg["axis"],
                min_step=float(self.settings.min_internal_step),
            )
            or {}
        )
        feasible = bool(layout.get("is_feasible"))
        mass = (
            bar_mass_kg(layout.get("bars", []), float(params.get("steel_density_kg_m3", 7850.0)))
            if feasible
            else float("inf")
        )
        component_ns = {str(k): int(v) for k, v in dict(candidate.get("component_ns", {})).items()}
        total_n = int(candidate.get("total_N", candidate.get("total_n", sum(component_ns.values()))))
        source = str(candidate.get("source", "components"))
        solution_id = stable_digest(
            {
                "task_id": task_id,
                "source": source,
                "total_n": total_n,
                "component_ns": component_ns,
                "candidate": candidate.get("candidate_id"),
            }
        )
        choices = candidate.get("component_choices", {}) or {}
        optimal = bool(choices) and all(bool(row.get("is_optimal")) for row in choices.values())
        return {
            **dict(candidate),
            "solution_id": solution_id,
            "source": source,
            "total_N": total_n,
            "component_ns": component_ns,
            "actual_mass_kg": float(mass),
            "is_feasible": feasible,
            "is_optimal": optimal and feasible,
            "status": "feasible" if feasible else str(layout.get("status", "infeasible")),
            "bar_layout": layout,
            "metadata": {
                "threads": self.settings.effective_threads(self._solver(task_id).get("threads")),
                "created_at": time.time(),
            },
        }

    def handle_layout_solution(self, job: PipelineJob) -> None:
        task_id = job.task_id
        candidate = self.store.load_candidate(task_id, str(job.payload["candidate_id"]))
        if candidate is None:
            raise KeyError("candidate missing")
        solution = self._layout_candidate(task_id, candidate)
        self.store.save_solution(task_id, solution)
        self._publish(
            task_id,
            {
                "type": "solution_available",
                "solution_id": solution["solution_id"],
                "source": solution["source"],
                "total_N": solution["total_N"],
                "is_feasible": solution["is_feasible"],
                "actual_mass_kg": solution["actual_mass_kg"],
                "result_url": f"/v1/tasks/{task_id}/solutions/{solution['solution_id']}",
            },
        )
        best = self.store.best_solution(task_id, solution["total_N"])
        if best and best.get("solution_id") == solution["solution_id"]:
            compatible = to_compat_result(solution)
            self.store.save_best_result(task_id, solution["total_N"], compatible, "final")
            status = "feasible" if solution["is_feasible"] else "infeasible"
            self.store.set_n_status(
                task_id,
                solution["total_N"],
                status,
                solution_id=solution["solution_id"],
                source=solution["source"],
                result_url=f"/v1/tasks/{task_id}/results/{solution['total_N']}",
            )
            self._publish(
                task_id,
                {
                    "type": "n_finished",
                    "n": solution["total_N"],
                    "status": status,
                    "result_url": f"/v1/tasks/{task_id}/results/{solution['total_N']}",
                    "solution_id": solution["solution_id"],
                    "source": solution["source"],
                },
            )
        if (self.store.get_meta(task_id) or {}).get("validate_results") and solution.get("is_feasible"):
            self.enqueue(JobKind.validate_solution, task_id, {"solution_id": solution["solution_id"]})

    def handle_validate_solution(self, job: PipelineJob) -> None:
        solution = self.store.load_solution(job.task_id, str(job.payload["solution_id"]))
        if solution is None:
            raise KeyError("solution missing")
        solution = {**solution, "validation": {"status": "not_configured"}}
        self.store.save_solution(job.task_id, solution)

    def _whole_component(self, task_id: str) -> dict[str, Any]:
        field = self._field(task_id)
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
        return {
            "id": -1,
            "axis": cfg["axis"],
            "polygon_indices": [r["source_index"] for r in rows],
            "polygons": rows,
            "geometry": demand,
            "demand_geometry": demand,
            "bounds": tuple(map(float, demand.bounds)),
            "demand_bounds": tuple(map(float, demand.bounds)),
            "classes": sorted({r["class"] for r in rows}),
            "loads": sorted({r["load"] for r in rows}),
            "max_hold": max(map(float, cfg.get("holds", {0: 0}).values()), default=0.0),
            "expanded_polygons": [],
        }

    def handle_prepare_whole(self, job: PipelineJob) -> None:
        task_id = job.task_id
        if job.payload.get("schedule_only"):
            record = self.store.load_component(task_id, "whole") or {}
            self._schedule_plan(
                task_id,
                "whole",
                record.get("plan", []),
                bool(record.get("force_single_box")),
                int(job.payload.get("start", 0)),
                whole=True,
            )
            return
        component = self._whole_component(task_id)
        self.store.save_component(
            task_id,
            "whole",
            {"component": component, "state": "preparing", "info": component_public_info(component)},
        )
        prepared = self._prepare_problem(task_id, "whole", component)
        meta = self.store.get_meta(task_id) or {}
        plan, force_single = choose_component_ns(
            meta.get("requested_n", [1]),
            prepared["max_useful_n"],
            str(meta.get("scan_mode")) == "hard",
        )
        record = {
            "component": component,
            "state": "prepared",
            "plan": plan,
            "force_single_box": force_single,
            "max_useful_n": prepared["max_useful_n"],
            "bounds": prepared["bounds"],
            "info": component_public_info(component, prepared["max_useful_n"], "prepared"),
        }
        self.store.save_component(task_id, "whole", record)
        self._schedule_plan(task_id, "whole", plan, force_single, whole=True)

    def handle_solve_whole(self, job: PipelineJob) -> None:
        from A101.reinforcement_components import solve_component_frontier

        task_id = job.task_id
        if job.payload.get("force_single_box"):
            result = self._single_component_frontier(task_id, "whole", "whole")
            self.store.save_frontier_result(task_id, "whole", 1, result)
            self._queue_whole_layout(task_id, result)
            return
        stored = self.store.load_problem(task_id, "whole")
        if not stored:
            raise KeyError("whole problem missing")
        n = int(job.payload["n"])
        if self.store.is_n_cancelled(task_id, n):
            return
        _, threads, timeout, time_limit, backend, require_optimal = self._solver_options(task_id)
        results, data = solve_component_frontier(
            stored["problem"],
            [n],
            data={},
            timeout=timeout,
            solver_time_limit=time_limit,
            threads=threads,
            backend=backend,
            require_optimal=require_optimal,
            return_best_on_timeout=True,
            raise_errors=False,
        )
        self.store.save_solver_result(task_id, "whole", n, {"results": results, "data": data, "threads": threads})
        self.enqueue(JobKind.fit_whole, task_id, {"component_id": "whole", "n": n, "source": "whole"})

    def handle_fit_whole(self, job: PipelineJob) -> None:
        from A101.reinforcement_components import fit_component_frontier

        task_id, n = job.task_id, int(job.payload["n"])
        stored = self.store.load_problem(task_id, "whole")
        solved = self.store.load_solver_result(task_id, "whole", n)
        if not stored or not solved:
            raise KeyError("whole problem/solver result missing")
        field = self._field(task_id)
        cfg = field["cfg"]
        params = self._params(task_id)
        threads = self.settings.effective_threads(self._solver(task_id).get("threads"))
        frontier = fit_component_frontier(
            stored["problem"],
            solved["results"],
            recipes=cfg.get("recipes"),
            densities=cfg["densities"],
            diameters=cfg["diameters"],
            steps=cfg["steps"],
            anchor_factor=float(params.get("anchor_factor", 32.0)),
            axis=cfg["axis"],
            field=field["field_geometry"],
            min_width=float(params.get("min_width_mm", 1000.0)),
            time_limit=self.settings.fit_time_limit,
            allow_class_upgrade=True,
            fit_milp_backend=self.settings.fit_milp_backend,
            fit_threads=threads,
        )
        result = dict(frontier.get(n, {"n": n, "is_feasible": False}))
        result["source"] = "whole"
        self.store.save_frontier_result(task_id, "whole", n, result)
        if result.get("is_feasible"):
            self._queue_whole_layout(task_id, result)

    def _queue_whole_layout(self, task_id: str, result: Mapping[str, Any]) -> None:
        n = int(result.get("n", 1))
        candidate_id = stable_digest({"task_id": task_id, "source": "whole", "n": n})
        candidate = {
            **dict(result),
            "candidate_id": candidate_id,
            "source": "whole",
            "total_N": n,
            "component_ns": {"whole": n},
            "component_choices": {"whole": dict(result)},
        }
        self.store.save_candidate(task_id, candidate_id, candidate)
        self.enqueue(
            JobKind.layout_solution,
            task_id,
            {"candidate_id": candidate_id, "total_n": n, "source": "whole"},
        )

    def schedule_requested_for_all(self, task_id: str, values: Sequence[int]) -> dict[str, list[int]]:
        queued: dict[str, list[int]] = {}
        for cid in self.store.component_ids(task_id):
            if cid == "whole":
                continue
            record = self.store.load_component(task_id, cid) or {}
            max_n = record.get("max_useful_n")
            if not max_n:
                continue
            plan, fallback = choose_component_ns(values, int(max_n), False)
            queued[cid] = plan
            for n in plan:
                self.enqueue(
                    JobKind.solve_component,
                    task_id,
                    {
                        "component_id": int(cid),
                        "n": int(n),
                        "force_single_box": bool(fallback and int(n) == 1),
                        "source": "components",
                    },
                )
        whole = self.store.load_component(task_id, "whole")
        if whole and whole.get("max_useful_n"):
            plan, fallback = choose_component_ns(values, int(whole["max_useful_n"]), False)
            queued["whole"] = plan
            for n in plan:
                self.enqueue(
                    JobKind.solve_whole,
                    task_id,
                    {
                        "component_id": "whole",
                        "n": int(n),
                        "force_single_box": bool(fallback and int(n) == 1),
                        "source": "whole",
                    },
                )
        return queued

    def schedule_component_n(self, task_id: str, component_id: int, values: Sequence[int]) -> list[int]:
        record = self.store.load_component(task_id, component_id)
        if record is None or not record.get("max_useful_n"):
            raise KeyError("component не подготовлена")
        max_n = int(record["max_useful_n"])
        requested = list(dict.fromkeys(int(n) for n in values))
        invalid = [n for n in requested if n < 1 or n > max_n]
        if invalid:
            raise ValueError(f"n вне допустимого диапазона 1..{max_n}: {invalid}")
        for n in requested:
            self.enqueue(
                JobKind.solve_component,
                task_id,
                {"component_id": int(component_id), "n": n, "force_single_box": False, "source": "components"},
            )
        return requested
