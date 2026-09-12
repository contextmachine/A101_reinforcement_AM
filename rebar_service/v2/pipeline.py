"""Whole-field /v2 task pipeline: preparing -> solving -> fitting -> bars.

One task = (scene_id, overlay_id, smooth, config, requested N list). Every N is an
independent solver run over the whole field; there are no components. Zones and bars are
stored without anchorage; anchorage lives only in the solver objective, in the mass
metrics and as numbers on the wire.

Job kinds on the main worker queue: ``v2_prepare`` (per task), ``v2_solve``, ``v2_fit``
and ``v2_bars`` (per N). Per-N states: pending -> preparing -> solving -> fitting -> bars ->
success | error | cancelled; ``status`` (optimal / feasable / infeasable) is the solver
outcome and is only meaningful once the N is no longer pending/preparing.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from shapely.geometry import Polygon
from shapely.ops import unary_union

from ..config import Settings
from ..pipeline import PipelineJob, analysis_variant, overlay_polygon_sets, polygons_from_input
from ..solver_logs import highs_log_options, solver_log_file
from . import bars as bars_module

KINDS = ("v2_prepare", "v2_solve", "v2_fit", "v2_bars")
LIVE_STATES = ("pending", "preparing", "solving", "fitting", "bars")


class V2Pipeline:
    def __init__(self, store: Any, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    @property
    def v2(self):
        return self.store.v2

    # ------------------------------------------------------------------ queue
    def enqueue(self, kind: str, task_id: str, n: int | None = None) -> bool:
        payload: dict[str, Any] = {} if n is None else {"n": int(n)}
        job = PipelineJob(
            kind, task_id, payload, generation=0,
            dedupe_key=f"v2:{kind}:{task_id}:{'-' if n is None else int(n)}",
        )
        return bool(self.store.enqueue_pipeline_job(job.to_dict()))

    def dispatch(self, job: Mapping[str, Any], worker_id: str) -> None:
        kind = str(job.get("kind", ""))
        task_id = str(job.get("task_id", ""))
        payload = dict(job.get("payload") or {})
        n = payload.get("n")
        handlers: dict[str, Callable[..., None]] = {
            "v2_prepare": self.handle_prepare,
            "v2_solve": self.handle_solve,
            "v2_fit": self.handle_fit,
            "v2_bars": self.handle_bars,
        }
        handler = handlers.get(kind)
        if handler is None:
            raise ValueError(f"Неизвестный v2 job kind: {kind}")
        try:
            handler(task_id, None if n is None else int(n), str(worker_id))
        except Exception as exc:  # noqa: BLE001 - the N must end in a terminal state
            if n is not None:
                try:
                    self.v2.set_n(task_id, int(n), state="error", error=f"{type(exc).__name__}: {exc}")
                except Exception:  # noqa: BLE001 - never mask the original failure
                    pass
            raise

    def _stage_open(self, task_id: str, n: int, *stages: str) -> bool:
        """True when the N is still waiting for one of ``stages`` (re-delivered jobs are skipped)."""

        if self._cancelled(task_id, n):
            return False
        row = self.v2.get_n(task_id, n)
        return row is not None and str(row.get("state")) in stages

    # -------------------------------------------------------------- API side
    def create_task(
        self, *, scene_id: str, overlay_id: int, smooth: bool, config: Mapping[str, Any], ns: Sequence[int],
    ) -> str:
        task_id = uuid4().hex
        self.v2.create_task(
            task_id, scene_id=scene_id, overlay_id=int(overlay_id), smooth=bool(smooth),
            config=dict(config), ns=[int(n) for n in ns],
        )
        self.enqueue("v2_prepare", task_id)
        return task_id

    def add_ns(self, task_id: str, ns: Sequence[int]) -> list[int]:
        added = self.v2.add_ns(task_id, [int(n) for n in ns])
        task = self.v2.get_task(task_id)
        if task is not None and added:
            if task.get("cancelled"):
                # New N revive a task whose previous N were all cancelled.
                self.v2.set_task(task_id, cancelled=False)
            if str(task.get("state")) != "ready":
                # Preparation never finished (cancelled early or failed): run it again.
                self.enqueue("v2_prepare", task_id)
        self.schedule_pending(task_id)
        return added

    def cancel(self, task_id: str, ns: Sequence[int] | None) -> list[int]:
        return self.v2.cancel(task_id, None if ns is None else [int(n) for n in ns])

    # ------------------------------------------------------------ scheduling
    def schedule_pending(self, task_id: str) -> list[int]:
        """Move every pending N of a prepared task to its first stage.

        Runs under the task row lock so the API (PUT .../n) and the preparing job can never
        schedule the same N twice; enqueue dedupe keys make a repeat harmless anyway.
        """
        scheduled: list[int] = []
        with self.v2.schedule_lock(task_id) as conn:
            task = self.v2.get_task(task_id, conn=conn)
            if task is None or task.get("cancelled") or str(task.get("state")) != "ready":
                return scheduled
            for n in self.v2.ns_in_states(task_id, ["pending", "preparing"], conn):
                self._schedule_one(task, int(n), conn)
                scheduled.append(int(n))
        return scheduled

    def _schedule_one(self, task: Mapping[str, Any], n: int, conn: Any) -> None:
        task_id = str(task["task_id"])
        max_useful_n = task.get("max_useful_n")
        min_useful_n = task.get("min_useful_n")
        info = dict(task.get("prepare_info") or {})
        if max_useful_n is not None and n > int(max_useful_n):
            reason = f"N={n} превышает max_useful_n={int(max_useful_n)}"
            if info.get("reason") and info["reason"] != "prepared":
                reason += f" ({info['reason']})"
            self.v2.set_n(task_id, n, state="success", status="infeasable", error=reason, conn=conn)
            return
        if min_useful_n is not None and n < int(min_useful_n):
            self.v2.set_n(
                task_id, n, state="success", status="infeasable",
                error=f"N={n} меньше аналитического минимума {int(min_useful_n)}", conn=conn,
            )
            return
        self.v2.set_n(task_id, n, state="solving", conn=conn)
        self.enqueue("v2_solve", task_id, n)

    # ----------------------------------------------------------- preparing
    def handle_prepare(self, task_id: str, _n: int | None, worker_id: str) -> None:
        task = self.v2.get_task(task_id)
        if task is None or task.get("cancelled"):
            return
        if str(task.get("state")) == "ready":
            # Re-delivered job after a worker restart: preparation already finished.
            self.schedule_pending(task_id)
            return
        self.v2.set_task(task_id, state="preparing")
        for n in self.v2.ns_in_states(task_id, ["pending"]):
            self.v2.set_n(task_id, int(n), state="preparing")
        from A101.calculate_mass import ReinforcementCapacityError
        from A101.reinforcement_components import CandidateCoverInfeasible

        try:
            field, problem, info = self._prepare(task)
        except ReinforcementCapacityError as exc:
            info = {
                "reason": "reinforcement_capacity", "load": float(exc.load),
                "max_supported_load": float(exc.max_supported_load), "max_layers": exc.max_layers,
                "back_grid": None if exc.back_grid is None else list(exc.back_grid),
            }
            self.v2.set_task(task_id, state="ready", max_useful_n=0, min_useful_n=None, prepare_info=info)
            self.schedule_pending(task_id)
            return
        except CandidateCoverInfeasible as exc:
            info = {"reason": "candidate_cover", "detail": str(exc)}
            self.v2.set_task(task_id, state="ready", max_useful_n=0, min_useful_n=None, prepare_info=info)
            self.schedule_pending(task_id)
            return
        except Exception as exc:  # noqa: BLE001 - every N must learn about the failure
            message = f"{type(exc).__name__}: {exc}"
            self.v2.set_task(task_id, state="error", error=message)
            for n in self.v2.ns_in_states(task_id, ["pending", "preparing"]):
                self.v2.set_n(task_id, int(n), state="error", error=message)
            raise

        try:
            self.v2.save_artifact(task_id, "field", field)
            if problem is not None:
                self.v2.save_artifact(task_id, "problem", problem)
            self.v2.set_task(
                task_id, state="ready", max_useful_n=int(info["max_useful_n"]),
                min_useful_n=info.get("min_useful_n"), prepare_info=info,
            )
        except Exception as exc:  # noqa: BLE001 - persistence failures must not park the N
            message = f"{type(exc).__name__}: {exc}"
            self.v2.set_task(task_id, state="error", error=message)
            for n in self.v2.ns_in_states(task_id, ["pending", "preparing"]):
                self.v2.set_n(task_id, int(n), state="error", error=message)
            raise
        self.schedule_pending(task_id)

    def _prepare(self, task: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
        from A101.axis_orientation import class_holds, normalize_axis
        from A101.calculate_mass import resolve_rebar_config
        from A101.grid_work import clean_poly
        from A101.max_n_milp import estimate_max_useful_n
        from A101.poly_bbox import rect_polygons
        from A101.reinforcement_components import (
            CandidateCoverInfeasible, component_n_bounds, prepare_component_problem,
        )

        settings = self.settings
        config = dict(task.get("config") or {})
        scene_id = str(task["scene_id"])
        overlay_id = int(task.get("overlay_id") or 0)
        variant = analysis_variant(bool(task.get("smooth")))
        resolved = list(self.store.resolved_scene_polygons(scene_id, variant=variant, overlay_id=overlay_id))
        all_rows = [{"points": row["points"], "load": row["load"]} for row in resolved]
        all_polygons = polygons_from_input({"kind": "polygons", "units": "mm", "polygons": all_rows})

        back_grid = config.get("back_grid")
        stock = config.get("stock")
        cfg = resolve_rebar_config(
            all_polygons,
            back_grid=None if back_grid is None else (back_grid["d"], back_grid["step"]),
            stock=None if stock is None else [(row["d"], row["step"]) for row in stock],
            max_layers=config.get("max_layers"),
        )
        axis = normalize_axis(str(config.get("axis", "y")))
        cfg["axis"] = axis
        anchor_factor = float(config.get("anchor_factor", 40.0))
        base_holds, holds, leaves = class_holds(cfg["diameters"], cfg.get("recipes"), anchor_factor)
        cfg.update(base_holds=base_holds, holds=holds, recipe_leaves=leaves)

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
            target.update(source_index=int(source["source_index"]), overlay_state=str(source["overlay_state"]))
        geometries = [
            p["geometry"] for p in physical_polygons if p.get("geometry") is not None and not p["geometry"].is_empty
        ]
        field_geometry = unary_union(geometries) if geometries else Polygon()
        ortho = clean_poly(rect_polygons(demand_polygons)) if demand_polygons else []

        rows = []
        for index, raw in enumerate(ortho):
            load = raw.get("load", raw.get("value", raw.get("class"))) if isinstance(raw, Mapping) else raw[1]
            geometry = raw.get("geometry") if isinstance(raw, Mapping) else raw[0]
            cls = cfg["load2cls"].get(load, cfg["load2cls"].get(str(load)))
            if cls is None or int(cls) == 0:
                continue
            rows.append({"geometry": geometry, "load": float(load), "class": int(cls), "source_index": index})

        field = {
            "scene_id": scene_id, "overlay_id": overlay_id, "smooth": bool(task.get("smooth")),
            "cfg": cfg, "axis": axis, "anchor_factor": anchor_factor,
            "physical_polygons": [
                {
                    "geometry": p["geometry"], "source_index": p.get("source_index"),
                    "overlay_state": p.get("overlay_state"),
                }
                for p in physical_polygons
            ],
            "field_geometry": field_geometry,
            "demand_polygons": len(rows),
            "created_at": time.time(),
        }
        if not rows:
            return field, None, {"reason": "no_positive_n_required", "max_useful_n": 0, "min_useful_n": None}

        demand = unary_union([r["geometry"] for r in rows])
        component = {
            "id": 0, "axis": axis, "polygon_indices": [r["source_index"] for r in rows], "polygons": rows,
            "geometry": demand, "demand_geometry": demand, "bounds": tuple(map(float, demand.bounds)),
            "demand_bounds": tuple(map(float, demand.bounds)),
            "classes": sorted({r["class"] for r in rows}), "loads": sorted({r["load"] for r in rows}),
            "max_hold": max(map(float, holds.values()), default=0.0), "expanded_polygons": [],
        }
        estimate: dict[str, Any] = {}

        def resolver(work_matrix, _context) -> int | None:
            # The exact max-N MILP runs first; its value bounds candidate generation.
            result = estimate_max_useful_n(work_matrix, recipes=cfg.get("recipes"), hard_cap=int(settings.max_n))
            estimate.update(result)
            if not result.get("feasible") or not result.get("max_useful_n"):
                raise CandidateCoverInfeasible("точный max-N MILP не нашёл допустимого покрытия прямоугольниками")
            return int(result["max_useful_n"])

        problem = prepare_component_problem(
            component,
            load2cls=cfg["load2cls"], recipes=cfg.get("recipes"), densities=cfg["densities"],
            diameters=cfg["diameters"], anchor_factor=anchor_factor, axis=axis,
            min_width=float(config.get("min_width_mm", 1000.0)),
            grid_size=float(settings.grid_size), fill_notches_threshold=float(settings.fill_notches),
            short_edge_threshold=float(settings.short_edge), simplify_steps_threshold=float(settings.simplify_step),
            max_n=None, max_n_resolver=resolver, use_mosaic=bool(settings.use_mosaic),
            preserve_demand_classes=True, strict_grid_coverage=True,
            refine_unrepresentable_cells=False, refine_mixed_cells=False,
            physical_geometry=field_geometry,
        )
        max_useful_n = int(estimate["max_useful_n"])
        bounds = component_n_bounds(problem["prepared"], cap=max_useful_n)
        info = {
            "reason": "prepared", "max_useful_n": max_useful_n,
            "matrix_max_useful_n": estimate.get("matrix_max_useful_n"), "capped": bool(estimate.get("capped")),
            "min_useful_n": int(bounds.get("lower_bound") or 1),
            "nonredundant_upper_bound": bounds.get("nonredundant_upper_bound"),
            "candidate_rectangles": problem.get("stats", {}).get("candidate_rectangles"),
            "matrix_shape": problem.get("stats", {}).get("matrix_shape"),
        }
        return field, problem, info

    # ------------------------------------------------------------- solving
    def _cancelled(self, task_id: str, n: int) -> bool:
        if self.v2.is_cancelled(task_id, n):
            self.v2.set_n(task_id, n, state="cancelled")
            return True
        return False

    def _solver_time_limit(self, task: Mapping[str, Any]) -> float | None:
        solver_cfg = dict((task.get("config") or {}).get("solver") or {})
        value = solver_cfg.get("solver_time_limit")
        return self.settings.solver_time_limit if value is None else float(value)

    def handle_solve(self, task_id: str, n: int | None, worker_id: str) -> None:
        from A101.reinforcement_components import solve_component_frontier, solver_result_state

        assert n is not None
        if not self._stage_open(task_id, n, "solving"):
            return
        task = self.v2.get_task(task_id)
        problem = self.v2.load_artifact(task_id, "problem")
        if task is None or problem is None:
            self.v2.set_n(task_id, n, state="error", error="prepared problem is missing")
            return
        log_file = solver_log_file(self.settings, task_id, n, worker_id)
        results, _ = solve_component_frontier(
            problem, [n], data={}, timeout=self.settings.solver_timeout,
            solver_time_limit=self._solver_time_limit(task),
            threads=int(self.settings.solver_threads), backend=str(self.settings.solver_backend),
            require_optimal=bool(self.settings.require_optimal), return_best_on_timeout=True,
            raise_errors=False, highs_options=highs_log_options(log_file),
        )
        row = dict(results.get(n) or {"n": n, "is_feasible": False, "error": "solver returned nothing"})
        if self._cancelled(task_id, n):
            return
        fun = row.get("total_cost")
        state = solver_result_state(row)
        if row.get("is_feasible"):
            self.v2.save_artifact(task_id, f"solver:{n}", row)
            status = "optimal" if row.get("is_optimal") else "feasable"
            self.v2.set_n(task_id, n, state="fitting", status=status, fun=None if fun is None else float(fun))
            self.enqueue("v2_fit", task_id, n)
        elif state == "infeasible" or row.get("infeasibility_proved"):
            self.v2.set_n(
                task_id, n, state="success", status="infeasable", fun=None,
                error=str(row.get("reason") or row.get("status") or "infeasible"),
            )
        else:
            self.v2.set_n(task_id, n, state="error", error=str(row.get("error") or row.get("status") or state))

    # -------------------------------------------------------------- fitting
    def handle_fit(self, task_id: str, n: int | None, worker_id: str) -> None:
        from A101.reinforcement_components import fit_component_frontier

        assert n is not None
        if not self._stage_open(task_id, n, "fitting"):
            return
        task = self.v2.get_task(task_id)
        field = self.v2.load_artifact(task_id, "field")
        problem = self.v2.load_artifact(task_id, "problem")
        solved = self.v2.load_artifact(task_id, f"solver:{n}")
        if task is None or field is None or problem is None or solved is None:
            self.v2.set_n(task_id, n, state="error", error="solver result or prepared problem is missing")
            return
        cfg = field["cfg"]
        config = dict(task.get("config") or {})
        anchor_factor = float(config.get("anchor_factor", 40.0))
        log_file = solver_log_file(self.settings, task_id, n, f"{worker_id}.fit")
        max_snap = config.get("max_snap_mm")
        frontier = fit_component_frontier(
            problem, {n: solved}, recipes=cfg.get("recipes"), densities=cfg["densities"],
            diameters=cfg["diameters"], steps=cfg["steps"], anchor_factor=anchor_factor,
            axis=cfg["axis"], field=field["field_geometry"], min_width=float(config.get("min_width_mm", 1000.0)),
            time_limit=self.settings.fit_time_limit, allow_class_upgrade=True,
            fit_milp_backend=str(self.settings.fit_milp_backend), fit_threads=int(self.settings.fit_threads),
            max_distance=None if max_snap is None else float(max_snap),
            highs_options=highs_log_options(log_file),
        )
        result = dict(frontier.get(n) or {"n": n, "is_feasible": False})
        if self._cancelled(task_id, n):
            return
        if not result.get("is_feasible"):
            self.v2.set_n(
                task_id, n, state="success", status="infeasable",
                error=f"fit infeasible ({result.get('failure_stage', 'fit')})",
            )
            self.v2.delete_artifact(task_id, f"solver:{n}")
            return
        bg = dict(config.get("back_grid") or {})
        if not bg:
            d, step = cfg["back_grid"]
            bg = {"d": float(d), "step": float(step)}
        zones = bars_module.fitted_boxes_to_zones(
            result["anchored_boxes"], axis=cfg["axis"], anchor_factor=anchor_factor, bg=bg,
        )
        is_optimal = bool(solved.get("is_optimal")) and bool(result.get("is_optimal"))
        self.v2.save_artifact(task_id, f"fit:{n}", {
            "zones": zones, "is_optimal": is_optimal, "proxy_mass": result.get("proxy_mass"),
            "class_changes": result.get("class_changes", []),
        })
        self.v2.set_n(task_id, n, state="bars", status="optimal" if is_optimal else "feasable")
        self.v2.delete_artifact(task_id, f"solver:{n}")
        self.enqueue("v2_bars", task_id, n)

    # ----------------------------------------------------------------- bars
    def handle_bars(self, task_id: str, n: int | None, worker_id: str) -> None:
        assert n is not None
        if not self._stage_open(task_id, n, "bars"):
            return
        task = self.v2.get_task(task_id)
        field = self.v2.load_artifact(task_id, "field")
        fit = self.v2.load_artifact(task_id, f"fit:{n}")
        if task is None or field is None or fit is None:
            self.v2.set_n(task_id, n, state="error", error="fit result or field is missing")
            return
        config = dict(task.get("config") or {})
        polygons = [row["geometry"] for row in field["physical_polygons"]]
        min_step = config.get("min_bar_gap_mm")
        out = bars_module.layout_zones(
            polygons, fit["zones"], axis=field["axis"], anchor_factor=float(config.get("anchor_factor", 40.0)),
            steel_density_kg_m3=float(config.get("steel_density_kg_m3", 7850.0)),
            min_step=float(min_step) if min_step else float(self.settings.min_internal_step),
        )
        if self._cancelled(task_id, n):
            return
        if not out.get("is_feasible"):
            self.v2.set_n(
                task_id, n, state="success", status="infeasable",
                error=f"bar layout {out.get('status', 'infeasible')}: {out.get('warnings') or out.get('errors')}",
            )
            return
        row = self.v2.get_n(task_id, n) or {}
        status = row.get("status") or ("optimal" if fit.get("is_optimal") else "feasable")
        self.v2.set_n(
            task_id, n, state="success", status=status, error=None,
            mass_metrics=out["mass_metrics"], result={"bars": out["bars"], "zones": out["zones"]},
        )
