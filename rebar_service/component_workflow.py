from __future__ import annotations

import json
import os
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union

from shapely.geometry import box
from shapely.ops import unary_union

from .component_jobs import JobEnvelope, JobKind, stable_digest
from .component_source import axis_from_filename, load_polygons, parse_request_snapshot, requested_ns, solver_options
from .component_store import ComponentStore
from .legacy_bridge import LegacyBridge
from .result_adapter import to_legacy_result


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.lower() not in {"0", "false", "no"}


def _env_optional_float(name: str, default: Optional[float]) -> Optional[float]:
    raw = os.getenv(name)
    if raw is None:
        return default
    return None if raw.lower() in {"none", "null", ""} else float(raw)


@dataclass
class WorkflowConfig:
    max_lay: int = field(default_factory=lambda: _env_int("REBAR_MAX_LAYERS", 2))
    min_width: float = field(default_factory=lambda: _env_float("REBAR_MIN_ZONE_WIDTH", 1000))
    anchor_factor: float = field(default_factory=lambda: _env_float("REBAR_ANCHOR_FACTOR", 32))
    grid_size: float = field(default_factory=lambda: _env_float("REBAR_GRID_SIZE", 300))
    fill_notches: float = field(default_factory=lambda: _env_float("REBAR_FILL_NOTCHES", 1000))
    short_edge: float = field(default_factory=lambda: _env_float("REBAR_SHORT_EDGE", 300))
    simplify_step: float = field(default_factory=lambda: _env_float("REBAR_SIMPLIFY_STEP", 1000))
    prepared_max_n: int = field(default_factory=lambda: _env_int("REBAR_PREPARED_MAX_N", 100))
    use_mosaic: bool = field(default_factory=lambda: _env_bool("REBAR_USE_MOSAIC", True))
    solver_timeout: Optional[float] = field(default_factory=lambda: _env_optional_float("REBAR_SOLVER_TIMEOUT", 900.0))
    solver_time_limit: Optional[float] = field(default_factory=lambda: _env_optional_float("REBAR_SOLVER_TIME_LIMIT", 840.0))
    solver_backend: str = field(default_factory=lambda: os.getenv("REBAR_SOLVER_BACKEND", "highs"))
    require_optimal: bool = field(default_factory=lambda: _env_bool("REBAR_REQUIRE_OPTIMAL", False))
    fit_time_limit: Optional[float] = field(default_factory=lambda: _env_optional_float("REBAR_FIT_TIME_LIMIT", 100.0))
    fit_backend: str = field(default_factory=lambda: os.getenv("REBAR_FIT_MILP_BACKEND", "auto"))
    min_internal_step: float = field(default_factory=lambda: _env_float("REBAR_MIN_INTERNAL_STEP", 100))
    steel_density: float = field(default_factory=lambda: _env_float("REBAR_STEEL_DENSITY", 7850))
    component_top_k: int = field(default_factory=lambda: _env_int("REBAR_COMPONENT_RESULT_TOP_K", 5))
    scheduler_batch_size: int = field(default_factory=lambda: _env_int("REBAR_SCHEDULER_BATCH_SIZE", 256))
    combine_batch_size: int = field(default_factory=lambda: _env_int("REBAR_COMBINE_BATCH_SIZE", 256))
    max_jobs_per_task: int = field(default_factory=lambda: _env_int("REBAR_MAX_JOBS_PER_TASK", 28031998))
    default_threads: int = field(default_factory=lambda: _env_int("REBAR_DEFAULT_SOLVER_THREADS", 1))
    max_threads: int = field(default_factory=lambda: _env_int("REBAR_MAX_SOLVER_THREADS", 4))

    def effective_threads(self, requested: Any = None) -> int:
        try:
            value = int(requested)
        except (TypeError, ValueError):
            value = self.default_threads
        return max(1, min(value, self.max_threads))


def choose_component_ns(requested: Sequence[int], max_useful_n: int, hard: bool = False) -> tuple[List[int], bool]:
    max_n = max(1, int(max_useful_n))
    if hard:
        return list(range(1, max_n + 1)), False
    values = list(dict.fromkeys(int(n) for n in requested if int(n) > 0 and int(n) <= max_n))
    return (values, False) if values else ([1], True)


def component_public_info(component: Mapping[str, Any], max_useful_n: Optional[int] = None, state: str = "created") -> Dict[str, Any]:
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


class ComponentWorkflow:
    def __init__(self, store: ComponentStore, config: Optional[WorkflowConfig] = None, legacy: Optional[LegacyBridge] = None) -> None:
        self.store = store
        self.config = config or WorkflowConfig()
        self.legacy = legacy or LegacyBridge()

    def enqueue(self, kind: Union[JobKind, str], task_id: str, payload: Optional[Mapping[str, Any]] = None, generation: Optional[int] = None, dedupe_key: Optional[str] = None) -> bool:
        pending_key = self.store.key("task", task_id, "pending")
        if int(self.store.redis.scard(pending_key) or 0) >= int(self.config.max_jobs_per_task):
            raise RuntimeError("REBAR_MAX_JOBS_PER_TASK exceeded for task %s" % task_id)
        generation = self.store.generation(task_id) if generation is None else int(generation)
        return self.store.enqueue(JobEnvelope(str(kind.value if isinstance(kind, JobKind) else kind), task_id, dict(payload or {}), generation=generation, dedupe_key=dedupe_key))

    def bootstrap_task(self, task_id: str, request_snapshot: Mapping[str, Any], options: Optional[Mapping[str, Any]] = None) -> bool:
        snapshot = dict(request_snapshot)
        self.store.save_request(task_id, snapshot)
        parsed = parse_request_snapshot(snapshot)
        opts = dict(options or {})
        opts.update({k: parsed[k] for k in ("scan_mode", "whole", "component_result_top_k", "validate_results") if k in parsed})
        ns = requested_ns(parsed)
        solver = solver_options(parsed)
        meta = self.store.update_task_meta(
            task_id,
            state="component_queued",
            requested_n=ns or [1],
            scan_mode=str(opts.get("scan_mode", "requested")),
            whole=bool(opts.get("whole", False)),
            component_result_top_k=int(opts.get("component_result_top_k", self.config.component_top_k)),
            validate_results=bool(opts.get("validate_results", False)),
            solver=solver,
            generation=self.store.generation(task_id),
        )
        self.store.publish(task_id, {"type": "component_pipeline_queued", "requested_n": meta["requested_n"], "scan_mode": meta["scan_mode"], "whole": meta["whole"]})
        return self.enqueue(JobKind.prepare_field, task_id)

    def dispatch(self, job: JobEnvelope) -> None:
        if int(job.generation) != self.store.generation(job.task_id):
            return
        handler = getattr(self, "handle_" + str(job.kind), None)
        if handler is None:
            raise ValueError("Неизвестный component job kind: %s" % job.kind)
        handler(job)

    def _field(self, task_id: str) -> Dict[str, Any]:
        field = self.store.load_field(task_id)
        if not field:
            raise KeyError("field не подготовлен для task %s" % task_id)
        return field

    def handle_prepare_field(self, job: JobEnvelope) -> None:
        from A101.axis_orientation import class_holds, normalize_axis
        from A101.calculate_mass import select_rebar_config
        from A101.grid_work import clean_poly
        from A101.poly_bbox import rect_polygons
        from A101.reinforcement_components import split_reinforcement_components

        task_id = job.task_id
        self.store.update_task_meta(task_id, state="preparing_components")
        self.store.publish(task_id, {"type": "component_prepare_started"})
        snapshot = self.store.load_request(task_id)
        payload = parse_request_snapshot(snapshot)
        polygons, filename = load_polygons(payload, snapshot)
        cfg = select_rebar_config(polygons, max_lay=self.config.max_lay, strategy=str(payload.get("strategy", "min_proxy")))
        axis = normalize_axis(str(payload.get("axis") or axis_from_filename(filename)))
        cfg["axis"] = axis
        base_holds, holds, leaves = class_holds(cfg["diameters"], cfg.get("recipes"), self.config.anchor_factor)
        cfg.update(base_holds=base_holds, holds=holds, recipe_leaves=leaves)
        ortho = clean_poly(rect_polygons(polygons))
        split = split_reinforcement_components(
            ortho,
            load2cls=cfg["load2cls"],
            recipes=cfg.get("recipes"),
            diameters=cfg["diameters"],
            anchor_factor=self.config.anchor_factor,
            axis=axis,
        )
        field_geometry = unary_union([p["geometry"] for p in polygons if p.get("geometry") is not None and not p["geometry"].is_empty])
        field = {
            "filename": filename,
            "start_polygons": polygons,
            "ortho_polygons": ortho,
            "cfg": cfg,
            "decomposition": split,
            "field_geometry": field_geometry,
        }
        self.store.save_field(task_id, field)
        for component in split.get("components", []):
            cid = int(component["id"])
            record = {"component": component, "info": component_public_info(component), "state": "queued"}
            self.store.save_component(task_id, cid, record)
            self.enqueue(JobKind.prepare_component, task_id, {"component_id": cid})
        if self.store.task_meta(task_id).get("whole"):
            self.enqueue(JobKind.prepare_whole, task_id)
        self.store.update_task_meta(task_id, state="components_ready", component_count=len(split.get("components", [])))
        event = {
            "type": "components_ready",
            "components": [component_public_info(c) for c in split.get("components", [])],
            "active_indices": split.get("active_indices", []),
            "background_only_indices": split.get("background_only_indices", []),
            "degenerate_indices": split.get("degenerate_indices", []),
        }
        self.store.publish(task_id, event)
        self.legacy.publish(task_id, event)

    def _prepare_problem(self, task_id: str, component_id: Any, component: Mapping[str, Any]) -> Dict[str, Any]:
        from A101.reinforcement_components import component_n_bounds, prepare_component_problem

        field = self._field(task_id)
        cfg = field["cfg"]
        problem = prepare_component_problem(
            component,
            load2cls=cfg["load2cls"],
            recipes=cfg.get("recipes"),
            densities=cfg["densities"],
            diameters=cfg["diameters"],
            anchor_factor=self.config.anchor_factor,
            axis=cfg["axis"],
            min_width=self.config.min_width,
            grid_size=self.config.grid_size,
            fill_notches_threshold=self.config.fill_notches,
            short_edge_threshold=self.config.short_edge,
            simplify_steps_threshold=self.config.simplify_step,
            max_n=self.config.prepared_max_n,
            use_mosaic=self.config.use_mosaic,
            preserve_demand_classes=True,
            strict_grid_coverage=True,
            refine_unrepresentable_cells=False,
            refine_mixed_cells=False,
        )
        bounds = component_n_bounds(problem["prepared"], cap=self.config.prepared_max_n)
        max_useful = max(1, int(bounds["nonredundant_upper_bound"]))
        self.store.save_problem(task_id, component_id, {"problem": problem, "bounds": bounds, "max_useful_n": max_useful})
        return {"problem": problem, "bounds": bounds, "max_useful_n": max_useful}

    def _schedule_plan(self, task_id: str, component_id: Any, plan: Sequence[int], force_single: bool, start: int = 0, whole: bool = False) -> None:
        batch = max(1, self.config.scheduler_batch_size)
        end = min(len(plan), int(start) + batch)
        kind = JobKind.solve_whole if whole else JobKind.solve_component
        for n in plan[int(start):end]:
            self.enqueue(kind, task_id, {"component_id": component_id, "n": int(n), "force_single_box": bool(force_single and int(n) == 1), "source": "whole" if whole else "components"})
        if end < len(plan):
            self.enqueue(JobKind.prepare_whole if whole else JobKind.prepare_component, task_id, {"component_id": component_id, "schedule_only": True, "start": end}, dedupe_key="schedule:%s:%s:%s:%s" % (task_id, component_id, end, self.store.generation(task_id)))

    def handle_prepare_component(self, job: JobEnvelope) -> None:
        task_id, cid = job.task_id, int(job.payload["component_id"])
        record = self.store.load_component(task_id, cid)
        if record is None:
            raise KeyError("component %s not found" % cid)
        if job.payload.get("schedule_only"):
            plan = list(record.get("plan", []))
            self._schedule_plan(task_id, cid, plan, bool(record.get("force_single_box")), int(job.payload.get("start", 0)))
            return
        prepared = self._prepare_problem(task_id, cid, record["component"])
        meta = self.store.task_meta(task_id)
        plan, force_single = choose_component_ns(meta.get("requested_n", [1]), prepared["max_useful_n"], str(meta.get("scan_mode")) == "hard")
        record.update(
            state="prepared",
            plan=plan,
            force_single_box=force_single,
            max_useful_n=prepared["max_useful_n"],
            bounds=prepared["bounds"],
            info=component_public_info(record["component"], prepared["max_useful_n"], "prepared"),
        )
        self.store.save_component(task_id, cid, record)
        event = {"type": "component_prepared", "component_id": cid, "max_useful_n": prepared["max_useful_n"], "planned_n": plan, "fallback_single_box": force_single}
        self.store.publish(task_id, event)
        self.legacy.publish(task_id, event)
        self._schedule_plan(task_id, cid, plan, force_single)

    def _single_component_frontier(self, task_id: str, component_id: Any, source: str = "components") -> Dict[str, Any]:
        from A101.axis_orientation import add_box_anchorage
        from A101.fit_box_layout import fit_box_layout

        field = self._field(task_id)
        cfg = field["cfg"]
        stored = self.store.load_problem(task_id, component_id)
        if not stored:
            raise KeyError("component problem missing")
        problem = stored["problem"]
        component = problem.get("component", {})
        bounds = tuple(map(float, component.get("demand_bounds") or component.get("bounds") or problem["component"]["geometry"].bounds))
        cls = max(map(int, component.get("classes", [1])))
        fitted = fit_box_layout(
            problem["poly_mos"],
            [(*bounds, cls)],
            recipes=cfg.get("recipes"),
            densities=cfg["densities"],
            min_w=self.config.min_width,
            time_limit=self.config.fit_time_limit,
            allow_class_upgrade=True,
            milp_backend=self.config.fit_backend,
            threads=self.config.effective_threads(self.store.task_meta(task_id).get("solver", {}).get("threads")),
        )
        if not fitted.get("is_feasible"):
            raise RuntimeError("single component box fit failed: %s" % fitted.get("errors"))
        anchored = add_box_anchorage(
            fitted,
            recipes=cfg.get("recipes"),
            diameters=cfg["diameters"],
            steps=cfg["steps"],
            anchor_factor=self.config.anchor_factor,
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
            "solver_result": {"is_feasible": True, "fallback": "single_component_box", "rectangles": [(*bounds, cls)]},
            "rectangles": [(*bounds, cls)],
            "fit_result": fitted,
            "class_changes": list(fitted.get("class_changes", [])),
            "anchored_boxes": anchored,
            "proxy_mass": float(proxy_mass),
            "source": source,
        }

    def handle_solve_component(self, job: JobEnvelope) -> None:
        from A101.reinforcement_components import solve_component_frontier

        task_id, cid, n = job.task_id, int(job.payload["component_id"]), int(job.payload["n"])
        if job.payload.get("force_single_box"):
            result = self._single_component_frontier(task_id, cid)
            self.store.save_frontier_result(task_id, cid, 1, result)
            self._frontier_ready(task_id, cid, 1, result)
            return
        stored = self.store.load_problem(task_id, cid)
        if not stored:
            raise KeyError("problem missing")
        threads = self.config.effective_threads(self.store.task_meta(task_id).get("solver", {}).get("threads"))
        results, data = solve_component_frontier(
            stored["problem"],
            [n],
            data={},
            timeout=self.config.solver_timeout,
            solver_time_limit=self.config.solver_time_limit,
            threads=threads,
            backend=self.config.solver_backend,
            require_optimal=self.config.require_optimal,
            return_best_on_timeout=True,
            raise_errors=False,
        )
        self.store.save_solver_result(task_id, cid, n, {"results": results, "data": data, "threads": threads})
        self.enqueue(JobKind.fit_component, task_id, {"component_id": cid, "n": n, "source": "components"})
        self.store.publish(task_id, {"type": "component_n_finished", "component_id": cid, "n": n, "stage": "solver"})

    def handle_fit_component(self, job: JobEnvelope) -> None:
        from A101.reinforcement_components import fit_component_frontier

        task_id, cid, n = job.task_id, int(job.payload["component_id"]), int(job.payload["n"])
        stored = self.store.load_problem(task_id, cid)
        solved = self.store.load_solver_result(task_id, cid, n)
        if not stored or not solved:
            raise KeyError("problem/solver result missing")
        field = self._field(task_id)
        cfg = field["cfg"]
        threads = self.config.effective_threads(self.store.task_meta(task_id).get("solver", {}).get("threads"))
        frontier = fit_component_frontier(
            stored["problem"],
            solved["results"],
            recipes=cfg.get("recipes"),
            densities=cfg["densities"],
            diameters=cfg["diameters"],
            steps=cfg["steps"],
            anchor_factor=self.config.anchor_factor,
            axis=cfg["axis"],
            field=field["field_geometry"],
            min_width=self.config.min_width,
            time_limit=self.config.fit_time_limit,
            allow_class_upgrade=True,
            fit_milp_backend=self.config.fit_backend,
            fit_threads=threads,
        )
        result = dict(frontier.get(n, {"n": n, "is_feasible": False, "error": "fit result missing"}))
        self.store.save_frontier_result(task_id, cid, n, result)
        self._frontier_ready(task_id, cid, n, result)

    def _frontier_ready(self, task_id: str, cid: Any, n: int, result: Mapping[str, Any]) -> None:
        event = {"type": "component_fit_finished", "component_id": cid, "n": int(n), "status": "feasible" if result.get("is_feasible") else result.get("solve_state", "failed")}
        self.store.publish(task_id, event)
        self.legacy.publish(task_id, event)
        version = int(self.store.redis.get(self.store.key("task", task_id, "frontier-version")) or 0)
        self.enqueue(JobKind.combine_frontiers, task_id, {"frontier_version": version, "offset": 0}, dedupe_key="combine:%s:%s:%s" % (task_id, self.store.generation(task_id), version))

    def handle_combine_frontiers(self, job: JobEnvelope) -> None:
        from A101.reinforcement_components import combine_component_frontiers

        task_id = job.task_id
        current_version = int(self.store.redis.get(self.store.key("task", task_id, "frontier-version")) or 0)
        requested_version = int(job.payload.get("frontier_version", current_version))
        if requested_version < current_version and int(job.payload.get("offset", 0)) == 0:
            return
        component_ids = [cid for cid in self.store.component_ids(task_id) if cid != "whole"]
        frontiers = self.store.all_frontiers(task_id)
        if not component_ids or any((int(cid) if cid.lstrip("-").isdigit() else cid) not in frontiers for cid in component_ids):
            return
        if any(not any(r.get("is_feasible") for r in frontiers[int(cid) if cid.lstrip("-").isdigit() else cid].values()) for cid in component_ids):
            return
        top_k = int(self.store.task_meta(task_id).get("component_result_top_k", self.config.component_top_k))
        combined = combine_component_frontiers(frontiers, top_k=top_k)
        flat = [(int(total_n), rank, candidate) for total_n, rows in sorted(combined.items()) for rank, candidate in enumerate(rows)]
        start = int(job.payload.get("offset", 0))
        end = min(len(flat), start + max(1, self.config.combine_batch_size))
        queued = 0
        for total_n, rank, candidate in flat[start:end]:
            candidate_id = stable_digest({"task_id": task_id, "source": "components", "total_n": total_n, "component_ns": candidate.get("component_ns", {})})
            candidate = {**dict(candidate), "candidate_id": candidate_id, "source": "components", "total_N": total_n}
            self.store.save_candidate(task_id, candidate_id, candidate)
            queued += int(self.enqueue(JobKind.layout_solution, task_id, {"candidate_id": candidate_id, "total_n": total_n, "source": "components"}))
        if end < len(flat):
            self.enqueue(JobKind.combine_frontiers, task_id, {"frontier_version": current_version, "offset": end}, dedupe_key="combine:%s:%s:%s:%s" % (task_id, self.store.generation(task_id), current_version, end))
        event = {"type": "frontier_updated", "frontier_version": current_version, "total_n": sorted(map(int, combined)), "queued_solutions": queued}
        self.store.publish(task_id, event)
        self.legacy.publish(task_id, event)

    def _layout_candidate(self, task_id: str, candidate: Mapping[str, Any]) -> Dict[str, Any]:
        from A101.rebar_field_layout import layout_rebars
        from A101.reinforcement_components import bar_mass_kg

        field = self._field(task_id)
        cfg = field["cfg"]
        layout = dict(layout_rebars(
            polygons=[p["geometry"] for p in field["start_polygons"]],
            boxes=candidate.get("anchored_boxes", []),
            background=cfg["back_grid"],
            axis=cfg["axis"],
            min_step=self.config.min_internal_step,
        ) or {})
        feasible = bool(layout.get("is_feasible"))
        mass = bar_mass_kg(layout.get("bars", []), self.config.steel_density) if feasible else float("inf")
        component_ns = {str(k): int(v) for k, v in dict(candidate.get("component_ns", {})).items()}
        total_n = int(candidate.get("total_N", candidate.get("total_n", sum(component_ns.values()))))
        source = str(candidate.get("source", "components"))
        solution_id = stable_digest({"task_id": task_id, "source": source, "total_n": total_n, "component_ns": component_ns, "candidate": candidate.get("candidate_id")})
        return {
            **dict(candidate),
            "solution_id": solution_id,
            "source": source,
            "total_N": total_n,
            "component_ns": component_ns,
            "actual_mass_kg": float(mass),
            "is_feasible": feasible,
            "status": "feasible" if feasible else str(layout.get("status", "infeasible")),
            "bar_layout": layout,
            "metadata": {"threads": self.config.effective_threads(self.store.task_meta(task_id).get("solver", {}).get("threads")), "created_at": time.time()},
        }

    def handle_layout_solution(self, job: JobEnvelope) -> None:
        task_id = job.task_id
        candidate = self.store.load_candidate(task_id, str(job.payload["candidate_id"]))
        if candidate is None:
            raise KeyError("candidate missing")
        previous = self.store.best_solution(task_id, int(candidate.get("total_N", job.payload.get("total_n", 0))))
        solution = self._layout_candidate(task_id, candidate)
        self.store.save_solution(task_id, solution)
        event = {
            "type": "solution_available",
            "solution_id": solution["solution_id"],
            "source": solution["source"],
            "total_N": solution["total_N"],
            "is_feasible": solution["is_feasible"],
            "actual_mass_kg": solution["actual_mass_kg"],
            "result_url": "/v1/tasks/%s/solutions/%s" % (task_id, solution["solution_id"]),
        }
        self.store.publish(task_id, event)
        self.legacy.publish(task_id, event)
        best = self.store.best_solution(task_id, solution["total_N"])
        if best and best.get("solution_id") == solution["solution_id"]:
            self.legacy.save_result(task_id, solution["total_N"], solution)
            legacy_event = {
                "type": "n_finished",
                "n": solution["total_N"],
                "status": "feasible" if solution["is_feasible"] else "infeasible",
                "result_url": "/v1/tasks/%s/results/%s" % (task_id, solution["total_N"]),
                "solution_id": solution["solution_id"],
                "source": solution["source"],
            }
            self.store.publish(task_id, legacy_event)
            self.legacy.publish(task_id, legacy_event)
        if self.store.task_meta(task_id).get("validate_results") and solution.get("is_feasible"):
            self.enqueue(JobKind.validate_solution, task_id, {"solution_id": solution["solution_id"]})

    def handle_validate_solution(self, job: JobEnvelope) -> None:
        solution = self.store.load_solution(job.task_id, str(job.payload["solution_id"]))
        if solution is None:
            raise KeyError("solution missing")
        # Project-specific validation remains optional and isolated from result publication.
        solution = {**solution, "validation": {"status": "not_configured"}}
        self.store.save_solution(job.task_id, solution)

    def _whole_component(self, task_id: str) -> Dict[str, Any]:
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

    def handle_prepare_whole(self, job: JobEnvelope) -> None:
        task_id = job.task_id
        if job.payload.get("schedule_only"):
            record = self.store.load_component(task_id, "whole") or {}
            self._schedule_plan(task_id, "whole", record.get("plan", []), bool(record.get("force_single_box")), int(job.payload.get("start", 0)), whole=True)
            return
        component = self._whole_component(task_id)
        self.store.save_component(task_id, "whole", {"component": component, "state": "preparing", "info": component_public_info(component)})
        prepared = self._prepare_problem(task_id, "whole", component)
        meta = self.store.task_meta(task_id)
        plan, force_single = choose_component_ns(meta.get("requested_n", [1]), prepared["max_useful_n"], str(meta.get("scan_mode")) == "hard")
        record = {"component": component, "state": "prepared", "plan": plan, "force_single_box": force_single, "max_useful_n": prepared["max_useful_n"], "bounds": prepared["bounds"], "info": component_public_info(component, prepared["max_useful_n"], "prepared")}
        self.store.save_component(task_id, "whole", record)
        self._schedule_plan(task_id, "whole", plan, force_single, whole=True)

    def handle_solve_whole(self, job: JobEnvelope) -> None:
        proxy = JobEnvelope(JobKind.solve_component.value, job.task_id, {**job.payload, "component_id": "whole"}, generation=job.generation, dedupe_key=job.dedupe_key, job_id=job.job_id, created_at=job.created_at)
        if job.payload.get("force_single_box"):
            result = self._single_component_frontier(job.task_id, "whole", "whole")
            self.store.save_frontier_result(job.task_id, "whole", 1, result)
            self._queue_whole_layout(job.task_id, result)
            return
        from A101.reinforcement_components import solve_component_frontier
        stored = self.store.load_problem(job.task_id, "whole")
        n = int(job.payload["n"])
        threads = self.config.effective_threads(self.store.task_meta(job.task_id).get("solver", {}).get("threads"))
        results, data = solve_component_frontier(stored["problem"], [n], data={}, timeout=self.config.solver_timeout, solver_time_limit=self.config.solver_time_limit, threads=threads, backend=self.config.solver_backend, require_optimal=self.config.require_optimal, return_best_on_timeout=True, raise_errors=False)
        self.store.save_solver_result(job.task_id, "whole", n, {"results": results, "data": data, "threads": threads})
        self.enqueue(JobKind.fit_whole, job.task_id, {"component_id": "whole", "n": n, "source": "whole"})

    def handle_fit_whole(self, job: JobEnvelope) -> None:
        from A101.reinforcement_components import fit_component_frontier
        task_id, n = job.task_id, int(job.payload["n"])
        stored = self.store.load_problem(task_id, "whole")
        solved = self.store.load_solver_result(task_id, "whole", n)
        field, cfg = self._field(task_id), self._field(task_id)["cfg"]
        threads = self.config.effective_threads(self.store.task_meta(task_id).get("solver", {}).get("threads"))
        frontier = fit_component_frontier(stored["problem"], solved["results"], recipes=cfg.get("recipes"), densities=cfg["densities"], diameters=cfg["diameters"], steps=cfg["steps"], anchor_factor=self.config.anchor_factor, axis=cfg["axis"], field=field["field_geometry"], min_width=self.config.min_width, time_limit=self.config.fit_time_limit, allow_class_upgrade=True, fit_milp_backend=self.config.fit_backend, fit_threads=threads)
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
        self.enqueue(JobKind.layout_solution, task_id, {"candidate_id": candidate_id, "total_n": n, "source": "whole"})

    def schedule_requested_for_all(self, task_id: str, values: Sequence[int]) -> Dict[str, List[int]]:
        queued: Dict[str, List[int]] = {}
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
                self.enqueue(JobKind.solve_component, task_id, {"component_id": int(cid), "n": int(n), "force_single_box": bool(fallback and int(n) == 1), "source": "components"})
        whole = self.store.load_component(task_id, "whole")
        if whole and whole.get("max_useful_n"):
            plan, fallback = choose_component_ns(values, int(whole["max_useful_n"]), False)
            queued["whole"] = plan
            for n in plan:
                self.enqueue(JobKind.solve_whole, task_id, {"component_id": "whole", "n": int(n), "force_single_box": bool(fallback and int(n) == 1), "source": "whole"})
        return queued

    def schedule_component_n(self, task_id: str, component_id: int, values: Sequence[int]) -> List[int]:
        record = self.store.load_component(task_id, component_id)
        if record is None or not record.get("max_useful_n"):
            raise KeyError("component не подготовлена")
        max_n = int(record["max_useful_n"])
        requested = list(dict.fromkeys(int(n) for n in values))
        invalid = [n for n in requested if n < 1 or n > max_n]
        if invalid:
            raise ValueError("n вне допустимого диапазона 1..%s: %s" % (max_n, invalid))
        for n in requested:
            self.enqueue(JobKind.solve_component, task_id, {"component_id": int(component_id), "n": n, "force_single_box": False, "source": "components"})
        return requested
