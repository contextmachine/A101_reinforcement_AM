#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pickle
from pathlib import Path
from typing import Any, Dict, Iterable, List


def run_api(args) -> None:
    import uvicorn
    uvicorn.run("rebar_service.run3_app:app", host=args.host, port=args.port, workers=args.workers, reload=args.reload)


def run_worker(args) -> None:
    from rebar_service.universal_worker import main
    main()


def _axis_from_filename(path: Path) -> str:
    from rebar_service.component_source import axis_from_filename
    return axis_from_filename(path.name)


def run_local(args) -> None:
    """Notebook-equivalent component pipeline without Redis/API."""
    from shapely.ops import unary_union

    from A101.axis_orientation import class_holds, normalize_axis
    from A101.calculate_mass import select_rebar_config
    from A101.grid_work import clean_poly
    from A101.poly_bbox import rect_polygons
    from A101.read_dxf import extract_polygons
    from A101.reinforcement_components import (
        combine_component_frontiers,
        component_n_bounds,
        fit_component_frontier,
        prepare_component_problem,
        solve_component_frontier,
        split_reinforcement_components,
    )
    from A101.rebar_field_layout import layout_rebars
    from A101.reinforcement_components import bar_mass_kg
    from rebar_service.component_workflow import choose_component_ns

    path = Path(args.dxf)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    requested = list(dict.fromkeys(args.n or [1]))

    polygons = extract_polygons(path)
    cfg = select_rebar_config(polygons, max_lay=args.max_lay, strategy=args.strategy)
    cfg["axis"] = normalize_axis(args.axis or _axis_from_filename(path))
    base, holds, leaves = class_holds(cfg["diameters"], cfg.get("recipes"), args.anchor_factor)
    cfg.update(base_holds=base, holds=holds, recipe_leaves=leaves)
    ortho = clean_poly(rect_polygons(polygons))
    split = split_reinforcement_components(
        ortho,
        load2cls=cfg["load2cls"],
        recipes=cfg.get("recipes"),
        diameters=cfg["diameters"],
        anchor_factor=args.anchor_factor,
        axis=cfg["axis"],
    )
    field = unary_union([p["geometry"] for p in polygons if not p["geometry"].is_empty])

    frontiers: Dict[int, Dict[int, Dict[str, Any]]] = {}
    component_report = []
    for component in split["components"]:
        cid = int(component["id"])
        problem = prepare_component_problem(
            component,
            load2cls=cfg["load2cls"],
            recipes=cfg.get("recipes"),
            densities=cfg["densities"],
            diameters=cfg["diameters"],
            anchor_factor=args.anchor_factor,
            axis=cfg["axis"],
            min_width=args.min_width,
            grid_size=args.grid_size,
            fill_notches_threshold=args.fill_notches,
            short_edge_threshold=args.short_edge,
            simplify_steps_threshold=args.simplify_step,
            max_n=args.max_n,
            use_mosaic=not args.no_mosaic,
            preserve_demand_classes=True,
            strict_grid_coverage=True,
            refine_unrepresentable_cells=False,
            refine_mixed_cells=False,
        )
        bounds = component_n_bounds(problem["prepared"], cap=args.max_n)
        ns, fallback = choose_component_ns(requested, bounds["nonredundant_upper_bound"], args.hard_scan)
        solved, _ = solve_component_frontier(
            problem,
            ns,
            data={},
            timeout=args.timeout,
            solver_time_limit=args.time_limit,
            threads=args.threads,
            backend=args.backend,
            require_optimal=args.require_optimal,
            return_best_on_timeout=True,
            raise_errors=False,
        )
        fitted = fit_component_frontier(
            problem,
            solved,
            recipes=cfg.get("recipes"),
            densities=cfg["densities"],
            diameters=cfg["diameters"],
            steps=cfg["steps"],
            anchor_factor=args.anchor_factor,
            axis=cfg["axis"],
            field=field,
            min_width=args.min_width,
            time_limit=args.fit_time_limit,
            allow_class_upgrade=True,
            fit_milp_backend=args.fit_backend,
            fit_threads=args.threads,
        )
        frontiers[cid] = fitted
        component_report.append({
            "id": cid,
            "polygon_indices": component.get("polygon_indices", []),
            "max_useful_n": bounds["nonredundant_upper_bound"],
            "planned_n": ns,
            "fallback_requested": fallback,
            "feasible_n": [int(n) for n, r in fitted.items() if r.get("is_feasible")],
        })

    combined = combine_component_frontiers(frontiers, top_k=args.top_k)
    solutions = {}
    for total_n, candidates in combined.items():
        rows = []
        for rank, candidate in enumerate(candidates):
            layout = layout_rebars(
                polygons=[p["geometry"] for p in polygons],
                boxes=candidate.get("anchored_boxes", []),
                background=cfg["back_grid"],
                axis=cfg["axis"],
                min_step=args.min_step,
            )
            feasible = bool(layout.get("is_feasible"))
            rows.append({
                **candidate,
                "source": "components",
                "total_N": int(total_n),
                "rank": rank,
                "is_feasible": feasible,
                "actual_mass_kg": bar_mass_kg(layout.get("bars", []), args.steel_density) if feasible else float("inf"),
                "bar_layout": layout,
            })
        rows.sort(key=lambda r: (not r["is_feasible"], r["actual_mass_kg"], r["proxy_mass"]))
        solutions[int(total_n)] = rows

    whole_solutions: Dict[int, List[Dict[str, Any]]] = {}
    if args.whole:
        whole_rows = []
        for index, raw in enumerate(ortho):
            load = raw.get("load", raw.get("value", raw.get("class"))) if isinstance(raw, dict) else raw[1]
            geometry = raw.get("geometry") if isinstance(raw, dict) else raw[0]
            cls = cfg["load2cls"].get(load, cfg["load2cls"].get(str(load)))
            if cls is None or int(cls) == 0:
                continue
            whole_rows.append({"geometry": geometry, "load": float(load), "class": int(cls), "source_index": index})
        demand = unary_union([row["geometry"] for row in whole_rows])
        whole_component = {
            "id": -1,
            "axis": cfg["axis"],
            "polygon_indices": [row["source_index"] for row in whole_rows],
            "polygons": whole_rows,
            "geometry": demand,
            "demand_geometry": demand,
            "bounds": tuple(map(float, demand.bounds)),
            "demand_bounds": tuple(map(float, demand.bounds)),
            "classes": sorted({row["class"] for row in whole_rows}),
            "loads": sorted({row["load"] for row in whole_rows}),
            "max_hold": max(map(float, cfg.get("holds", {0: 0}).values()), default=0.0),
            "expanded_polygons": [],
        }
        whole_problem = prepare_component_problem(
            whole_component,
            load2cls=cfg["load2cls"], recipes=cfg.get("recipes"), densities=cfg["densities"],
            diameters=cfg["diameters"], anchor_factor=args.anchor_factor, axis=cfg["axis"],
            min_width=args.min_width, grid_size=args.grid_size, fill_notches_threshold=args.fill_notches,
            short_edge_threshold=args.short_edge, simplify_steps_threshold=args.simplify_step,
            max_n=args.max_n, use_mosaic=not args.no_mosaic, preserve_demand_classes=True,
            strict_grid_coverage=True, refine_unrepresentable_cells=False, refine_mixed_cells=False,
        )
        whole_bounds = component_n_bounds(whole_problem["prepared"], cap=args.max_n)
        whole_ns, _ = choose_component_ns(requested, whole_bounds["nonredundant_upper_bound"], args.hard_scan)
        whole_solved, _ = solve_component_frontier(
            whole_problem, whole_ns, data={}, timeout=args.timeout, solver_time_limit=args.time_limit,
            threads=args.threads, backend=args.backend, require_optimal=args.require_optimal,
            return_best_on_timeout=True, raise_errors=False,
        )
        whole_fitted = fit_component_frontier(
            whole_problem, whole_solved, recipes=cfg.get("recipes"), densities=cfg["densities"],
            diameters=cfg["diameters"], steps=cfg["steps"], anchor_factor=args.anchor_factor,
            axis=cfg["axis"], field=field, min_width=args.min_width, time_limit=args.fit_time_limit,
            allow_class_upgrade=True, fit_milp_backend=args.fit_backend, fit_threads=args.threads,
        )
        for n, candidate in whole_fitted.items():
            if not candidate.get("is_feasible"):
                whole_solutions[int(n)] = [{**candidate, "source": "whole", "total_N": int(n), "is_feasible": False}]
                continue
            layout = layout_rebars(
                polygons=[row["geometry"] for row in polygons], boxes=candidate.get("anchored_boxes", []),
                background=cfg["back_grid"], axis=cfg["axis"], min_step=args.min_step,
            )
            feasible = bool(layout.get("is_feasible"))
            whole_solutions[int(n)] = [{
                **candidate, "source": "whole", "total_N": int(n), "component_ns": {"whole": int(n)},
                "is_feasible": feasible,
                "actual_mass_kg": bar_mass_kg(layout.get("bars", []), args.steel_density) if feasible else float("inf"),
                "bar_layout": layout,
            }]

    bundle = {
        "format": "A101-run3-local-v1",
        "input": str(path),
        "axis": cfg["axis"],
        "config": cfg,
        "components": component_report,
        "decomposition": split,
        "frontiers": frontiers,
        "combined": combined,
        "solutions": solutions,
        "whole_solutions": whole_solutions,
    }
    with (output / "run3_result.pkl").open("wb") as fh:
        pickle.dump(bundle, fh, protocol=pickle.HIGHEST_PROTOCOL)
    summary = {
        "input": str(path),
        "axis": cfg["axis"],
        "component_count": len(component_report),
        "components": component_report,
        "solutions": [
            {
                "total_N": n,
                "is_feasible": bool(rows and rows[0]["is_feasible"]),
                "actual_mass_kg": None if not rows or not rows[0]["is_feasible"] else rows[0]["actual_mass_kg"],
                "component_N": None if not rows else rows[0].get("component_ns"),
            }
            for n, rows in sorted(solutions.items())
        ],
        "whole_solutions": [
            {
                "total_N": n,
                "is_feasible": bool(rows and rows[0].get("is_feasible")),
                "actual_mass_kg": None if not rows or not rows[0].get("is_feasible") else rows[0].get("actual_mass_kg"),
            }
            for n, rows in sorted(whole_solutions.items())
        ],
    }
    (output / "run3_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def inspect_task(args) -> None:
    from rebar_service.component_store import ComponentStore
    from rebar_service.universal_worker import redis_client
    store = ComponentStore(redis_client())
    value = {
        "meta": store.task_meta(args.task_id),
        "components": store.components(args.task_id),
        "solutions": store.solutions(args.task_id),
        "events": store.events(args.task_id),
    }
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="A101 component-aware reinforcement service/pipeline")
    sub = p.add_subparsers(dest="command", required=True)

    api = sub.add_parser("api", help="Run backward-compatible FastAPI application")
    api.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    api.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    api.add_argument("--workers", type=int, default=int(os.getenv("WEB_CONCURRENCY", "1")))
    api.add_argument("--reload", action="store_true")
    api.set_defaults(func=run_api)

    worker = sub.add_parser("worker", help="Run universal legacy + component worker")
    worker.set_defaults(func=run_worker)

    inspect = sub.add_parser("inspect", help="Print component state for a task")
    inspect.add_argument("task_id")
    inspect.set_defaults(func=inspect_task)

    local = sub.add_parser("local", help="Run the full notebook-equivalent pipeline for one DXF")
    local.add_argument("dxf")
    local.add_argument("--output", default="run3_output")
    local.add_argument("-n", nargs="+", type=int, default=[1, 2, 3])
    local.add_argument("--hard-scan", action="store_true")
    local.add_argument("--whole", action="store_true", help="Reserved for parity with API; component branch is always calculated")
    local.add_argument("--axis", choices=["x", "y"])
    local.add_argument("--max-lay", type=int, default=2)
    local.add_argument("--strategy", default="min_proxy")
    local.add_argument("--anchor-factor", type=float, default=32)
    local.add_argument("--min-width", type=float, default=1000)
    local.add_argument("--grid-size", type=float, default=300)
    local.add_argument("--fill-notches", type=float, default=1000)
    local.add_argument("--short-edge", type=float, default=300)
    local.add_argument("--simplify-step", type=float, default=1000)
    local.add_argument("--max-n", type=int, default=100)
    local.add_argument("--no-mosaic", action="store_true")
    local.add_argument("--timeout", type=float, default=900)
    local.add_argument("--time-limit", type=float, default=840)
    local.add_argument("--threads", type=int, default=1)
    local.add_argument("--backend", default="highs")
    local.add_argument("--require-optimal", action="store_true")
    local.add_argument("--fit-time-limit", type=float, default=100)
    local.add_argument("--fit-backend", default="auto")
    local.add_argument("--top-k", type=int, default=5)
    local.add_argument("--min-step", type=float, default=100)
    local.add_argument("--steel-density", type=float, default=7850)
    local.set_defaults(func=run_local)

    return p


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
