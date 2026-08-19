from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np
from shapely.geometry import Polygon

from A101.axis_orientation import grid_rectangles_to_world, normalize_axis, orient_grid
from A101.calculate_mass import loads_to_classes, make_rebar_classes, rebar_summary
from A101.cells_merging import reduce_mosaic
from A101.fit_rebar_layout import fit_rebar_layout
from A101.grid_quantizer import quantize_rectilinear_loads
from A101.grid_work import clean_poly, get_grid_matrix, resolve_overlaps
from A101.linear_idea import generate_all_rectangles, relabel_rectangle_candidates
from A101.poly_bbox import rect_polygons
from A101.read_dxf import extract_polygons
from A101.select_min_density_rectangles_recipes import prepare_rectangle_problem

Progress = Callable[[str, Mapping[str, Any]], None]


def _emit(progress: Progress | None, phase: str, **payload: Any) -> None:
    if progress:
        progress(phase, payload)



def normalize_input_payload(payload: Any) -> dict[str, Any]:
    """Accept the API schema and the legacy ``[[points, load], ...]`` JSON."""

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

def _polygon_input(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    payload = normalize_input_payload(payload)
    kind = payload.get("kind")
    if kind == "dxf":
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
        geom = Polygon(points)
        if not geom.is_valid:
            geom = geom.buffer(0)
        if geom.is_empty or geom.area <= 0:
            raise ValueError(f"Некорректный полигон #{i}")
        out.append({"points": points, "geometry": geom, "load": float(item["load"])})
    if not out:
        raise ValueError("Не переданы полигоны")
    return out


def prepare_pipeline(input_payload: Mapping[str, Any], params: Mapping[str, Any], progress: Progress | None = None):
    axis = normalize_axis(params.get("axis", "y"))
    _emit(progress, "input")
    start_polygons = _polygon_input(input_payload)

    _emit(progress, "rectilinearize", polygons=len(start_polygons))
    ortho_polygons = rect_polygons(start_polygons)
    q = dict(params.get("quantizer", {}))
    main_grid = quantize_rectilinear_loads(
        ortho_polygons,
        target_cells_x=q.get("target_cells_x"),
        target_cells_y=q.get("target_cells_y"),
        method=q.get("method", "exact"),
        preserve_holes=bool(q.get("preserve_holes", True)),
        max_shift_fraction=float(q.get("max_shift_fraction", 0.02)),
        shrink_penalty=float(q.get("shrink_penalty", 30.0)),
        expand_penalty=float(q.get("expand_penalty", 1.0)),
        load_gamma=float(q.get("load_gamma", 2.5)),
        min_shrink_tol_ratio=float(q.get("min_shrink_tol_ratio", 0.10)),
        min_expand_tol_ratio=float(q.get("min_expand_tol_ratio", 0.50)),
        coord_eps=float(q.get("coord_eps", 1e-6)),
    )
    quantized = clean_poly(resolve_overlaps(main_grid["snapped"]))
    xs, ys, load_matrix = get_grid_matrix(quantized)

    _emit(progress, "classes")
    loads = sorted({p["load"] for p in start_polygons})
    cfg = make_rebar_classes(
        loads,
        tuple(params.get("back_grid", (18, 300))),
        [tuple(x) for x in params.get("stock", [])],
        max_lay=int(params.get("max_layers", 2)),
    )
    load2cls, recipes = cfg["load2cls"], cfg["recipes"]
    densities, diameters, steps = cfg["densities"], cfg["diameters"], cfg["steps"]
    anchor_factor = float(params.get("anchor_factor", 32.0))
    base_holds = {cls: anchor_factor * diameter for cls, diameter in diameters.items()}
    holds = dict(base_holds)
    for cls, layers in recipes.items():
        holds[cls] = max(base_holds[layer] for layer in layers)

    int_matrix = loads_to_classes(load_matrix, load2cls)
    work_matrix, work_x_edges, work_y_edges, work_x_steps, work_y_steps = orient_grid(int_matrix, xs, ys, axis)

    _emit(progress, "candidate_rectangles", shape=list(work_matrix.shape))
    requirement_rectangles = generate_all_rectangles(
        int_matrix=work_matrix,
        x_steps=work_x_steps,
        y_steps=work_y_steps,
        xs=work_x_edges,
        min_w=float(params.get("min_width_mm", 1000.0)),
        holds=holds,
    )
    selectable_rectangles = relabel_rectangle_candidates(requirement_rectangles, recipes)

    _emit(progress, "mosaic", candidates=len(selectable_rectangles))
    work_rectangles, mosaic, _ = reduce_mosaic(
        work_matrix,
        selectable_rectangles,
        target=np.inf,
        rect_target=np.inf,
        force_reduce=False,
        show=False,
    )

    solver = dict(params.get("solver", {}))
    prepared_max_n = solver.get("prepared_max_n")
    if prepared_max_n is None:
        prepared_max_n = int(params.get("initial_max_n", 0))
    _emit(progress, "prepare_model", candidates=len(work_rectangles), max_n=prepared_max_n)
    prepared = prepare_rectangle_problem(
        value_matrix=work_matrix,
        xs=work_x_steps,
        ys=work_y_steps,
        rectangles=work_rectangles,
        densities=densities,
        recipes=recipes,
        holds=base_holds,
        axis="y",
        mosaic=mosaic,
        max_n=int(prepared_max_n),
        build_pulp_template=bool(solver.get("build_pulp_template", False)),
    )

    poly_mos = [(p["geometry"], load2cls[p["load"]]) for p in start_polygons]
    context = {
        "axis": axis,
        "work_x_edges": np.asarray(work_x_edges),
        "work_y_edges": np.asarray(work_y_edges),
        "poly_mos": poly_mos,
        "recipes": recipes,
        "densities": densities,
        "diameters": diameters,
        "steps": steps,
        "base_holds": base_holds,
        "min_width_mm": float(params.get("min_width_mm", 1000.0)),
        "steel_density_kg_m3": float(params.get("steel_density_kg_m3", 7850.0)),
        "max_snap_mm": float(params.get("max_snap_mm", 600.0)),
        "min_bar_gap_mm": float(params.get("min_bar_gap_mm", 50.0)),
    }
    public = {
        "problem_id": prepared["problem_id"],
        "polygons": len(start_polygons),
        "matrix_shape": list(work_matrix.shape),
        "candidate_rectangles": len(work_rectangles),
        "recipes": recipes,
        "densities": densities,
        "diameters": diameters,
        "steps": steps,
        "prepared_stats": prepared.get("stats", {}),
    }
    _emit(progress, "prepared", **public)
    return prepared, context, public


def world_rectangles(solver_result: Mapping[str, Any], context: Mapping[str, Any]) -> list[tuple]:
    return grid_rectangles_to_world(
        solver_result["rectangles"],
        context["work_x_edges"],
        context["work_y_edges"],
        context["axis"],
    )


def finalize_solution(solver_result: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    if not solver_result or not solver_result.get("is_feasible") or not solver_result.get("rectangles"):
        return {"solver_result": dict(solver_result or {}), "is_feasible": False, "is_optimal": False}
    rec_opt = world_rectangles(solver_result, context)
    fit_result = fit_rebar_layout(
        polygons=context["poly_mos"],
        rectangles=rec_opt,
        recipes=context["recipes"],
        divisors=context["steps"],
        densities=context["densities"],
        min_width={k: context["min_width_mm"] for k in context["steps"]},
        axis=context["axis"],
        field=None,
        max_snap=context["max_snap_mm"],
        min_bar_gap=context["min_bar_gap_mm"],
    )
    summary = rebar_summary(
        rec_opt=rec_opt,
        fit_result=fit_result,
        divisors=context["steps"],
        diameters=context["diameters"],
        density=context["steel_density_kg_m3"],
        axis=context["axis"],
        holds=context["base_holds"],
        polygons=context["poly_mos"],
    )
    solver_is_optimal = bool(solver_result.get("is_optimal"))
    postprocess_is_optimal = bool(fit_result.get("is_optimal"))
    return {
        "is_feasible": bool(fit_result.get("is_feasible")),
        # Обратная совместимость: верхний флаг означает, что сеточный MILP
        # доказан оптимальным, а его постобработка дала допустимую геометрию.
        "is_optimal": solver_is_optimal and bool(fit_result.get("is_feasible")),
        "solver_is_optimal": solver_is_optimal,
        "postprocess_is_optimal": postprocess_is_optimal,
        "optimality_scope": "prepared_grid" if solver_is_optimal else None,
        "total_cost": solver_result.get("total_cost"),
        "solver_result": dict(solver_result),
        "primary_rectangles": rec_opt,
        "fit_result": fit_result,
        "summary": summary,
    }
