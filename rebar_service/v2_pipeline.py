from __future__ import annotations

from typing import Any, Mapping

import logging
import numpy as np
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from A101.max_n_milp import estimate_max_useful_n

from .config import Settings
from .pipeline import overlay_polygon_sets, polygons_from_input
from .v2_jobs import V2Job


logger = logging.getLogger("rebar.v2_pipeline")


def _arm_pair(value: Mapping[str, Any] | None) -> tuple[float, float] | None:
    if not value:
        return None
    return float(value["d"]), float(value["step"])


def build_v2_field_context(store: Any, task: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve a reusable scene snapshot into one whole-field solver component."""
    from A101.axis_orientation import class_holds, normalize_axis
    from A101.calculate_mass import resolve_rebar_config
    from A101.grid_work import clean_poly
    from A101.poly_bbox import rect_polygons
    from A101.reinforcement_components import _load_class

    scene_id = str(task["scene_id"])
    variant = "smooth" if bool(task.get("smooth")) else "raw"
    overlay_id = int(task.get("overlay_id", 0))
    params = dict(task.get("config", {}) or {})

    persisted = list(store.load_scene_variant_polygons(scene_id, variant=variant))
    all_polygons = polygons_from_input({"kind": "polygons", "units": "mm", "polygons": persisted})
    cfg = resolve_rebar_config(
        all_polygons,
        back_grid=_arm_pair(params.get("back_grid")),
        stock=[_arm_pair(row) for row in params.get("stock", [])],
        max_layers=params.get("max_layers"),
    )
    axis = normalize_axis(str(params.get("axis", "y")))
    cfg["axis"] = axis
    anchor_factor = float(params.get("anchor_factor", 40.0))
    base_holds, holds, leaves = class_holds(cfg["diameters"], cfg.get("recipes"), anchor_factor)
    cfg.update(base_holds=base_holds, holds=holds, recipe_leaves=leaves)

    resolved = list(store.resolved_scene_polygons(scene_id, variant=variant, overlay_id=overlay_id))
    sets = overlay_polygon_sets(resolved)
    demand_polygons = (
        polygons_from_input({"kind": "polygons", "units": "mm", "polygons": sets["active"]})
        if sets["active"] else []
    )
    for target, source in zip(demand_polygons, sets["active"]):
        target["source_index"] = int(source["source_index"])
    physical_polygons = (
        polygons_from_input({"kind": "polygons", "units": "mm", "polygons": sets["physical"]})
        if sets["physical"] else []
    )
    for target, source in zip(physical_polygons, sets["physical"]):
        target.update(
            source_index=int(source["source_index"]),
            overlay_state=str(source["overlay_state"]),
            active=bool(source.get("active", source["overlay_state"] == "active")),
            real=bool(source.get("real", False)),
        )

    ortho = clean_poly(rect_polygons(demand_polygons)) if demand_polygons else []
    physical_geometries = [
        row["geometry"] for row in physical_polygons
        if row.get("geometry") is not None and not row["geometry"].is_empty
    ]
    field_geometry = unary_union(physical_geometries) if physical_geometries else Polygon()

    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(ortho):
        load = raw.get("load", raw.get("value", raw.get("class"))) if isinstance(raw, Mapping) else raw[1]
        geometry = raw.get("geometry") if isinstance(raw, Mapping) else raw[0]
        try:
            cls = int(_load_class(cfg["load2cls"], load))
        except (KeyError, TypeError, ValueError):
            continue
        if cls <= 0:
            continue
        rows.append({"geometry": geometry, "load": float(load), "class": cls, "source_index": index})

    if rows:
        demand = unary_union([row["geometry"] for row in rows])
        component = {
            "id": -1,
            "axis": axis,
            "polygon_indices": [int(x) for x in sets["active_indices"]],
            "polygons": rows,
            "geometry": demand,
            "demand_geometry": demand,
            "bounds": tuple(map(float, demand.bounds)),
            "demand_bounds": tuple(map(float, demand.bounds)),
            "classes": sorted({int(row["class"]) for row in rows}),
            "loads": sorted({float(row["load"]) for row in rows}),
            "max_hold": max(map(float, holds.values()), default=0.0),
            "expanded_polygons": [],
        }
    else:
        component = {
            "id": -1, "axis": axis, "polygon_indices": [], "polygons": [],
            "geometry": Polygon(), "demand_geometry": Polygon(), "bounds": [], "demand_bounds": [],
            "classes": [], "loads": [], "max_hold": 0.0, "expanded_polygons": [],
        }

    field = {
        "scene_id": scene_id,
        "variant": variant,
        "smooth": bool(task.get("smooth")),
        "overlay_id": overlay_id,
        "start_polygons": physical_polygons,
        "ortho_polygons": ortho,
        "field_geometry": field_geometry,
        "cfg": cfg,
        "active_indices": list(sets["active_indices"]),
        "background_only_indices": list(sets["background_only_indices"]),
        "removed_indices": list(sets["removed_indices"]),
    }
    return {"field": field, "component": component, "cfg": cfg, "params": params}


def build_v2_matrix_problem(context: Mapping[str, Any], settings: Settings) -> dict[str, Any]:
    from A101.reinforcement_components import prepare_component_matrix

    cfg = dict(context["cfg"])
    params = dict(context["params"])
    return prepare_component_matrix(
        context["component"],
        load2cls=cfg["load2cls"],
        recipes=cfg.get("recipes"),
        densities=cfg["densities"],
        diameters=cfg["diameters"],
        anchor_factor=float(params.get("anchor_factor", 40.0)),
        axis=cfg["axis"],
        grid_size=float(settings.grid_size),
        fill_notches_threshold=float(settings.fill_notches),
        short_edge_threshold=float(settings.short_edge),
        simplify_steps_threshold=float(settings.simplify_step),
        preserve_demand_classes=True,
        strict_grid_coverage=True,
        refine_unrepresentable_cells=False,
        refine_mixed_cells=False,
        max_dense_cells=int(settings.prepare_max_dense_cells),
        physical_geometry=context["field"].get("field_geometry"),
    )


def build_v2_solver_problem(
    matrix_problem: Mapping[str, Any],
    context: Mapping[str, Any],
    settings: Settings,
    *,
    max_n: int,
) -> dict[str, Any]:
    from A101.reinforcement_components import prepare_component_problem_from_matrix

    cfg = dict(context["cfg"])
    params = dict(context["params"])
    return prepare_component_problem_from_matrix(
        matrix_problem,
        load2cls=cfg["load2cls"],
        recipes=cfg.get("recipes"),
        densities=cfg["densities"],
        diameters=cfg["diameters"],
        anchor_factor=float(params.get("anchor_factor", 40.0)),
        min_width=float(params.get("min_width_mm", 1000.0)),
        max_n=int(max_n),
        use_mosaic=bool(settings.use_mosaic),
    )





def _expand_v2_recipe_layers(cls: int, recipes: Mapping[Any, Any] | None) -> tuple[int, ...]:
    normalized: dict[int, tuple[int, ...]] = {}
    for raw_key, raw_value in dict(recipes or {}).items():
        key = int(raw_key)
        if isinstance(raw_value, (str, bytes)):
            values = (int(raw_value),)
        else:
            try:
                values = tuple(int(value) for value in raw_value)
            except TypeError:
                values = (int(raw_value),)
        normalized[key] = values
    cache: dict[int, tuple[int, ...]] = {}

    def expand(value: int, trail: tuple[int, ...] = ()) -> tuple[int, ...]:
        value = int(value)
        if value in cache:
            return cache[value]
        children = normalized.get(value)
        if not children:
            cache[value] = (value,)
            return cache[value]
        if value in trail:
            raise ValueError("cyclic reinforcement recipe")
        cache[value] = tuple(
            leaf for child in children for leaf in expand(child, (*trail, value))
        )
        return cache[value]

    return expand(int(cls))


def analytical_v2_minimum(
    problem: Mapping[str, Any], context: Mapping[str, Any], n: int
) -> dict[str, Any] | None:
    """Return an exact analytical result for below/minimum N, or None for normal HiGHS.

    A physical void (`work_matrix < 0`) disables the full-bounds minimum shortcut
    for N == min_n, matching the current whole-field v1 semantics.  The below-min
    proof remains valid because it depends only on primitive recipe layer count.
    """
    component = dict(problem.get("component", {}) or {})
    classes = [int(value) for value in component.get("classes", []) if int(value) > 0]
    if not classes:
        classes = [
            int(row.get("class", 0))
            for row in component.get("polygons", [])
            if isinstance(row, Mapping) and int(row.get("class", 0)) > 0
        ]
    if not classes:
        return None
    cfg = dict(context.get("cfg", {}) or {})
    layers = _expand_v2_recipe_layers(max(classes), cfg.get("recipes"))
    if not layers:
        return None
    min_n = len(layers)
    if int(n) < min_n:
        return {
            "n": int(n), "is_feasible": False, "is_optimal": True,
            "solve_state": "infeasible", "status": "Infeasible",
            "reason": "below_minimum_n", "min_useful_n": min_n,
            "analytical_min_n": True,
        }
    if int(n) != min_n:
        return None
    matrix = np.asarray(problem.get("work_matrix", []))
    if matrix.size and np.any(matrix < 0):
        return None

    bounds_value = component.get("demand_bounds") or component.get("bounds")
    if not bounds_value and component.get("geometry") is not None:
        bounds_value = component["geometry"].bounds
    if not bounds_value or len(bounds_value) != 4:
        return None
    bounds = tuple(map(float, bounds_value))
    rectangles = [(*bounds, int(layer)) for layer in layers]
    from A101.axis_orientation import add_box_anchorage

    params = dict(context.get("params", {}) or {})
    field = dict(context.get("field", {}) or {})
    anchored = add_box_anchorage(
        rectangles,
        recipes=cfg.get("recipes"), diameters=cfg["diameters"], steps=cfg["steps"],
        anchor_factor=float(params.get("anchor_factor", 40.0)), axis=cfg.get("axis", "y"),
        field=field.get("field_geometry"),
    )
    proxy_mass = 0.0
    for row in anchored:
        geometry = row.get("geometry") if isinstance(row, Mapping) else None
        if geometry is None:
            geometry = box(*row["bounds"][:4])
        density = cfg["densities"].get(row.get("class"), cfg["densities"].get(str(row.get("class")), 1.0))
        proxy_mass += float(geometry.area) * float(density)
    fit_result = {
        "n": min_n, "is_feasible": True, "is_optimal": True,
        "solve_state": "optimal", "rectangles": rectangles,
        "fit_result": {
            "status": f"N={min_n} analytical full-field cover",
            "is_feasible": True, "is_optimal": True, "rectangles": rectangles,
            "objective": 0.0, "class_changes": [],
            "stats": {"analytical_min_n_fast_path": True},
            "analytical_min_n_fast_path": True,
        },
        "class_changes": [], "anchored_boxes": anchored,
        "proxy_mass": float(proxy_mass), "analytical_min_n_fast_path": True,
    }
    return {
        "n": min_n, "is_feasible": True, "is_optimal": True,
        "solve_state": "optimal", "status": "Optimal", "total_cost": float(proxy_mass),
        "min_useful_n": min_n, "primitive_layers": list(map(int, layers)),
        "analytical_min_n_fast_path": True, "analytical_fit": fit_result,
    }


def solve_v2_problem(
    problem: Mapping[str, Any],
    n: int,
    *,
    threads: int,
    timeout: float | None,
    solver_time_limit: float | None,
    backend: str,
    require_optimal: bool,
    highs_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one whole-field N and preserve the real solver classification."""
    from A101.reinforcement_components import solve_component_frontier

    results, _state = solve_component_frontier(
        problem, [int(n)], data={}, timeout=timeout, solver_time_limit=solver_time_limit,
        threads=int(threads), backend=str(backend), require_optimal=bool(require_optimal),
        return_best_on_timeout=True, raise_errors=True, highs_options=highs_options,
    )
    return dict(results[int(n)])


def fit_v2_problem(
    problem: Mapping[str, Any],
    solver_result: Mapping[str, Any],
    context: Mapping[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    """Fit one whole-field solver result without v1 orchestration side effects."""
    from A101.reinforcement_components import fit_component_frontier

    n = int(solver_result["n"])
    cfg = dict(context["cfg"])
    params = dict(context["params"])
    frontier = fit_component_frontier(
        problem, {n: dict(solver_result)},
        recipes=cfg.get("recipes"), densities=cfg["densities"],
        diameters=cfg["diameters"], steps=cfg["steps"],
        anchor_factor=float(params.get("anchor_factor", 40.0)),
        axis=cfg["axis"], field=context["field"].get("field_geometry"),
        min_width=float(params.get("min_width_mm", 1000.0)),
        time_limit=settings.fit_time_limit, allow_class_upgrade=True,
        fit_milp_backend=settings.fit_milp_backend,
        fit_threads=settings.effective_threads(None),
    )
    return dict(frontier.get(n, {"n": n, "is_feasible": False}))



def persistable_v2_fit(fitted: Mapping[str, Any]) -> dict[str, Any]:
    """Strip anchorage-expanded geometry from durable v2 fit artifacts.

    Fitting may construct anchored geometry transiently for physics/mass metadata,
    but v2 storage keeps only visible fitted bounds plus scalar anchorage metadata.
    """
    out = dict(fitted)
    clean_layers: list[dict[str, Any]] = []
    forbidden = {
        "geometry", "bounds", "anchored_bounds", "anchored_bounds_unclipped",
        "final_rectangle_with_anchorage", "final_rectangle_with_anchorage_unclipped",
        "bars", "bars_with_anchorage", "bars_with_anchorage_unclipped",
    }
    for raw in fitted.get("anchored_boxes", []) or []:
        row = {key: value for key, value in dict(raw).items() if key not in forbidden}
        clean_layers.append(row)
    out["anchored_boxes"] = clean_layers
    return out


def compact_v2_zones_from_fit(
    fitted: Mapping[str, Any], context: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Return compact public zones whose geometry never contains anchorage."""
    cfg = dict(context["cfg"])
    axis = str(cfg.get("axis", "y"))
    background = cfg.get("back_grid")
    zones: list[dict[str, Any]] = []
    if background is not None:
        bg_d, bg_step = map(float, background)
        zones.append({"id": 0, "kind": "bg", "arm": {"d": bg_d, "step": bg_step}})

    for index, raw in enumerate(fitted.get("anchored_boxes", []) or [], start=1):
        row = dict(raw)
        bounds = row.get("fitted_bounds") or row.get("original_bounds")
        if not bounds or len(bounds) < 4:
            raise ValueError("fitted reinforcement layer has no non-anchored bounds")
        x0, y0, x1, y1 = map(float, bounds[:4])
        diameter = float(row.get("diameter") or 0)
        step = float(row.get("step") or 0)
        if diameter <= 0 or step <= 0:
            raise ValueError("fitted reinforcement layer has invalid diameter/step")
        hold = float(row.get("hold") or 0.0)
        if axis == "x":
            direction = [0.0, -1.0]
            length = max(0.0, x1 - x0)
            cross_span = max(0.0, y1 - y0)
            origin = [x0, y1]
        else:
            direction = [1.0, 0.0]
            length = max(0.0, y1 - y0)
            cross_span = max(0.0, x1 - x0)
            origin = [x0, y0]
        count = max(1, int(round(cross_span / step)) + 1)
        zones.append({
            "id": index, "kind": "additional",
            "arm": {"d": diameter, "step": step},
            "left": 0, "right": count - 1, "length": length,
            "start_anchorage": hold, "end_anchorage": hold,
            "origin": origin, "direction": direction,
        })
    return zones




def _request_scene_rows(store: Any, request: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    scene_id = str(request["scene_id"])
    variant = "smooth" if bool(request.get("smooth")) else "raw"
    overlay_id = int(request.get("overlay_id", 0))
    resolved = list(store.resolved_scene_polygons(scene_id, variant=variant, overlay_id=overlay_id))
    sets = overlay_polygon_sets(resolved)
    physical = (
        polygons_from_input({"kind": "polygons", "units": "mm", "polygons": sets["physical"]})
        if sets["physical"] else []
    )
    for target, source in zip(physical, sets["physical"]):
        target.update(
            source_index=int(source["source_index"]),
            overlay_state=str(source.get("overlay_state", "active")),
        )
    source_rows = []
    for raw in resolved:
        row = dict(raw)
        converted = polygons_from_input({"kind": "polygons", "units": "mm", "polygons": [row]})[0]
        converted.update(
            source_index=int(row.get("source_index", len(source_rows))),
            overlay_state=str(row.get("overlay_state", "active")),
        )
        source_rows.append(converted)
    return physical, source_rows


def build_v2_bars_for_context(
    context: Mapping[str, Any], zones: list[dict[str, Any]], settings: Settings, *, steel_density_kg_m3: float
) -> dict[str, Any]:
    from .v2_layout import build_v2_bar_result

    field = dict(context["field"])
    params = dict(context.get("params") or {})
    polygons = [row["geometry"] for row in field.get("start_polygons", []) if row.get("geometry") is not None]
    result, _raw = build_v2_bar_result(
        polygons, zones, axis=str(context["cfg"].get("axis", "y")),
        min_step=float(settings.min_internal_step), steel_density_kg_m3=float(steel_density_kg_m3),
        anchor_factor=float(params.get("anchor_factor", 40.0)),
    )
    return result


def build_v2_bars_for_request(
    store: Any, request: Mapping[str, Any], settings: Settings
) -> dict[str, Any]:
    from .v2_layout import build_v2_bar_result

    physical, _source = _request_scene_rows(store, request)
    config = dict(request.get("config", {}) or {})
    zones = [dict(row) for row in request.get("zones", []) or []]
    result, _raw = build_v2_bar_result(
        [row["geometry"] for row in physical], zones, axis=str(config.get("axis", "y")),
        min_step=float(settings.min_internal_step),
        steel_density_kg_m3=float(config.get("steel_density_kg_m3") or settings.default_steel_density_kg_m3),
        anchor_factor=float(config.get("anchor_factor", 40.0)),
    )
    return result


def verify_v2_request(
    store: Any, request: Mapping[str, Any], bar_result: Mapping[str, Any]
) -> list[dict[str, Any]]:
    from .v2_layout import reinforcement_by_polygon

    _physical, source_rows = _request_scene_rows(store, request)
    config = dict(request.get("config", {}) or {})
    return reinforcement_by_polygon(
        source_rows, list((bar_result.get("bar_layout") or {}).get("bars", []) or []),
        steel_density_kg_m3=float(config["steel_density_kg_m3"]),
        thickness_mm=float(config["t"]),
    )


class V2Pipeline:
    def __init__(self, store: Any, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    def dispatch(self, job: V2Job) -> None:
        handler = getattr(self, f"handle_{job.kind}", None)
        if handler is None:
            raise ValueError(f"Unknown v2 job kind: {job.kind}")
        handler(job)

    def _n_job_is_current(self, task_id: str, n: int, attempt: int, expected_state: str) -> bool:
        row = self.store.get_v2_n(task_id, int(n))
        return bool(
            row
            and int(row.get("attempt", 0)) == int(attempt)
            and str(row.get("state")) == str(expected_state)
        )

    def _enqueue_solve(self, task_id: str, n: int, attempt: int) -> bool:
        return bool(self.store.enqueue_v2_job(
            V2Job(stage="solving", kind="solve", task_id=task_id, n=int(n), attempt=int(attempt)).to_dict()
        ))

    def schedule_ready_n(self, task_id: str) -> None:
        task = self.store.get_v2_task(task_id)
        if task is None:
            raise KeyError(task_id)
        max_n = int(task.get("max_useful_n") or 0)
        for row in task.get("solutions", []):
            if str(row.get("state")) != "pending":
                continue
            n = int(row["n"])
            attempt = int(row.get("attempt", 1))
            if n > max_n:
                self.store.set_v2_n_state(
                    task_id, n, "error", attempt=attempt,
                    error={
                        "stage": "preparing",
                        "type": "NAboveMaxUseful",
                        "message": f"N={n} exceeds max_useful_n={max_n}",
                        "max_useful_n": max_n,
                    },
                )
                continue
            self._enqueue_solve(task_id, n, attempt)

    def handle_prepare(self, job: V2Job) -> None:
        task = self.store.get_v2_task(job.task_id)
        if task is None:
            raise KeyError(job.task_id)
        self.store.set_v2_preparation_state(job.task_id, "preparing")
        logger.info("prepare_context task_id=%s", job.task_id)
        context = build_v2_field_context(self.store, task)
        self.store.save_v2_artifact(job.task_id, "field_context", "field_context", context)
        if not context["component"].get("polygons"):
            self.store.set_v2_preparation_state(job.task_id, "success", max_useful_n=0)
            refreshed = self.store.get_v2_task(job.task_id) or task
            for row in refreshed.get("solutions", []):
                if row.get("state") == "pending":
                    self.store.set_v2_n_state(
                        job.task_id, int(row["n"]), "error", attempt=int(row.get("attempt", 1)),
                        error={"stage": "preparing", "type": "NoAdditionalDemand", "message": "No additional reinforcement demand"},
                    )
            return

        matrix_problem = build_v2_matrix_problem(context, self.settings)
        self.store.save_v2_artifact(job.task_id, "matrix_problem", "matrix_problem", matrix_problem)
        max_result = estimate_max_useful_n(
            matrix_problem["work_matrix"],
            recipes=context["cfg"].get("recipes"),
            hard_cap=int(self.settings.effective_prepare_max_n()),
        )
        max_n = int(max_result.get("max_useful_n") or 0)
        logger.info("prepare_max_useful_n task_id=%s max_useful_n=%s feasible=%s", job.task_id, max_n, bool(max_result.get("feasible")))
        if not bool(max_result.get("feasible")) or max_n <= 0:
            raise RuntimeError(f"Unable to compute positive max_useful_n: {max_result}")

        prepared = build_v2_solver_problem(matrix_problem, context, self.settings, max_n=max_n)
        self.store.save_v2_artifact(job.task_id, "prepared_problem", "prepared_problem", prepared)
        self.store.save_v2_artifact(job.task_id, "max_n", "max_n", max_result)
        self.store.set_v2_preparation_state(job.task_id, "success", max_useful_n=max_n)
        self.schedule_ready_n(job.task_id)
        logger.info("prepare_done task_id=%s max_useful_n=%s", job.task_id, max_n)

    def handle_solve(self, job: V2Job) -> None:
        if job.n is None:
            raise ValueError("solve job requires n")
        n, attempt = int(job.n), int(job.attempt)
        row = self.store.get_v2_n(job.task_id, n)
        if row is None or int(row.get("attempt", 0)) != attempt:
            return
        if str(row.get("state")) == "cancelled":
            return
        self.store.set_v2_n_state(job.task_id, n, "solving", attempt=attempt)
        problem = self.store.load_v2_artifact(job.task_id, "prepared_problem")
        if problem is None:
            raise KeyError("prepared v2 problem missing")
        task = self.store.get_v2_task(job.task_id)
        if task is None:
            raise KeyError(job.task_id)
        config = dict(task.get("config", {}) or {})
        solver_cfg = dict(config.get("solver", {}) or {})
        requested_limit = solver_cfg.get("solver_time_limit")
        time_limit = self.settings.effective_solver_time_limit(requested_limit)
        context = self.store.load_v2_artifact(job.task_id, "field_context")
        if context is None:
            raise KeyError("field context missing")
        result = analytical_v2_minimum(problem, context, n)
        capture = None
        if result is None:
            from .solver_logs import SolverLogCapture
            capture = SolverLogCapture(self.settings, task_id=job.task_id, n=n, attempt=attempt)
            with capture:
                result = solve_v2_problem(
                    problem, n, threads=self.settings.effective_threads(None),
                    timeout=self.settings.solver_timeout, solver_time_limit=time_limit,
                    backend=self.settings.solver_backend, require_optimal=self.settings.require_optimal,
                    highs_options=capture.highs_options,
                )
        if capture is not None and capture.object_key and capture.upload_error is None:
            result["solver_log_key"] = capture.object_key
        if capture is not None and capture.upload_error:
            result["solver_log_upload_error"] = capture.upload_error
        logger.info(
            "solve_result task_id=%s n=%s attempt=%s state=%s feasible=%s optimal=%s",
            job.task_id, n, attempt, result.get("solve_state"), bool(result.get("is_feasible")), bool(result.get("is_optimal")),
        )
        if not self._n_job_is_current(job.task_id, n, attempt, "solving"):
            return
        key = f"solver:n:{n}:attempt:{attempt}"
        self.store.save_v2_artifact(job.task_id, key, "solver_result", result, n=n, attempt=attempt)
        state = str(result.get("solve_state") or "")
        if result.get("is_feasible"):
            self.store.enqueue_v2_job(V2Job(
                stage="fitting", kind="fit", task_id=job.task_id, n=n, attempt=attempt
            ).to_dict())
            return
        if state == "infeasible":
            self.store.set_v2_n_state(
                job.task_id, n, "success", attempt=attempt, status="infeasible",
                result={"solver_result": result},
            )
            return
        detail = str(result.get("error") or result.get("status") or state or "solver failed")
        raise RuntimeError(detail)

    def handle_fit(self, job: V2Job) -> None:
        if job.n is None:
            raise ValueError("fit job requires n")
        n, attempt = int(job.n), int(job.attempt)
        row = self.store.get_v2_n(job.task_id, n)
        if row is None or int(row.get("attempt", 0)) != attempt:
            return
        if str(row.get("state")) == "cancelled":
            return
        self.store.set_v2_n_state(job.task_id, n, "fitting", attempt=attempt)
        problem = self.store.load_v2_artifact(job.task_id, "prepared_problem")
        context = self.store.load_v2_artifact(job.task_id, "field_context")
        solved = self.store.load_v2_artifact(job.task_id, f"solver:n:{n}:attempt:{attempt}")
        if problem is None or context is None or solved is None:
            raise KeyError("v2 fit input artifact missing")
        fitted = dict(solved.get("analytical_fit") or {})
        if not fitted:
            fitted = fit_v2_problem(problem, solved, context, self.settings)
        if not fitted.get("is_feasible"):
            raise RuntimeError(str(
                fitted.get("fit_result", {}).get("status")
                or fitted.get("failure_stage") or "fit failed"
            ))
        zones = compact_v2_zones_from_fit(fitted, context)
        if not self._n_job_is_current(job.task_id, n, attempt, "fitting"):
            return
        artifact = {"n": n, "fit": persistable_v2_fit(fitted), "zones": zones}
        self.store.save_v2_artifact(
            job.task_id, f"fit:n:{n}:attempt:{attempt}", "fit_result", artifact, n=n, attempt=attempt
        )
        self.store.enqueue_v2_job(V2Job(
            stage="baring", kind="task_bars", task_id=job.task_id, n=n, attempt=attempt
        ).to_dict())
        logger.info("fit_done task_id=%s n=%s attempt=%s zones=%s", job.task_id, n, attempt, len(zones))

    def handle_task_bars(self, job: V2Job) -> None:
        if job.n is None:
            raise ValueError("task_bars job requires n")
        n, attempt = int(job.n), int(job.attempt)
        row = self.store.get_v2_n(job.task_id, n)
        if row is None or int(row.get("attempt", 0)) != attempt:
            return
        if str(row.get("state")) == "cancelled":
            return
        self.store.set_v2_n_state(job.task_id, n, "baring", attempt=attempt)
        task = self.store.get_v2_task(job.task_id)
        context = self.store.load_v2_artifact(job.task_id, "field_context")
        fitted = self.store.load_v2_artifact(job.task_id, f"fit:n:{n}:attempt:{attempt}")
        solved = self.store.load_v2_artifact(job.task_id, f"solver:n:{n}:attempt:{attempt}")
        if task is None or context is None or fitted is None or solved is None:
            raise KeyError("v2 baring input artifact missing")
        config = dict(task.get("config", {}) or {})
        density = float(config.get("steel_density_kg_m3") or self.settings.default_steel_density_kg_m3)
        zones = [dict(row) for row in fitted.get("zones", []) or []]
        bars = build_v2_bars_for_context(context, zones, self.settings, steel_density_kg_m3=density)
        if not self._n_job_is_current(job.task_id, n, attempt, "baring"):
            return
        final = {
            "bar_layout": bars["bar_layout"],
            "mass_metrics": bars["mass_metrics"],
            "zones": zones,
        }
        fit_row = dict(fitted.get("fit", {}) or {})
        optimal = bool(solved.get("is_optimal")) and bool(fit_row.get("is_optimal"))
        status = "optimal" if optimal else "feasible"
        fun = solved.get("total_cost")
        self.store.save_v2_artifact(
            job.task_id, f"bars:n:{n}:attempt:{attempt}", "bar_result", bars, n=n, attempt=attempt
        )
        self.store.set_v2_n_state(
            job.task_id, n, "success", attempt=attempt, status=status,
            fun=None if fun is None else float(fun), mass_kg=float(bars["mass_kg"]),
            mass_bg_kg=float(bars["mass_bg_kg"]), result=final,
        )
        logger.info(
            "baring_done task_id=%s n=%s attempt=%s status=%s mass_kg=%s mass_bg_kg=%s bars=%s",
            job.task_id, n, attempt, status, bars.get("mass_kg"), bars.get("mass_bg_kg"),
            len(dict(bars.get("bar_layout", {})).get("bars", []) or []),
        )

    def handle_bars_request(self, job: V2Job) -> None:
        if not job.request_id:
            raise ValueError("bars_request requires request_id")
        request = self.store.get_v2_request("bars", job.request_id)
        if request is None:
            raise KeyError(job.request_id)
        self.store.set_v2_request_state("bars", job.request_id, "baring")
        result = build_v2_bars_for_request(self.store, request, self.settings)
        # Standalone v2 bars contract exposes only the documented mass fields.
        public = {
            "bar_layout": result["bar_layout"],
            "mass_metrics": {
                kind: {
                    key: float(value)
                    for key, value in dict(result["mass_metrics"].get(kind, {})).items()
                    if key in {"with_anchorage_kg", "without_anchorage_kg"}
                }
                for kind in ("additional", "bg")
            },
        }
        self.store.set_v2_request_state("bars", job.request_id, "success", result=public)
        logger.info(
            "bars_request_done request_id=%s bars=%s",
            job.request_id, len(dict(public.get("bar_layout", {})).get("bars", []) or []),
        )

    def handle_verification(self, job: V2Job) -> None:
        if not job.request_id:
            raise ValueError("verification job requires request_id")
        request = self.store.get_v2_request("verification", job.request_id)
        if request is None:
            raise KeyError(job.request_id)
        self.store.set_v2_request_state("verification", job.request_id, "validation")
        # Deliberately execute the pure bar-layout calculation inside this worker;
        # validation never enqueues/calls the baring worker handler.
        bars = build_v2_bars_for_request(self.store, request, self.settings)
        result = verify_v2_request(self.store, request, bars)
        self.store.set_v2_request_state("verification", job.request_id, "success", result=result)
        logger.info("verification_done request_id=%s polygons=%s", job.request_id, len(result))
