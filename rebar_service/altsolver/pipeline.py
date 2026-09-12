"""The alternative solver behind the v2 task protocol (same task/N rows, same job kinds).

``handle_prepare`` builds the experiments' engine to validate the scene and sizes N exactly
(``min_useful_n`` = smallest covering count, ``max_useful_n`` = box count of the unconstrained
minimum-mass cover: a larger N returns the same solution and is reported ``infeasable`` with that
reason, like the production pipeline does);
``handle_solve`` runs one N end to end: set-cover selection → wire zones → production bar
layout → gap filling → the N row (``mass_metrics`` and ``result`` exactly as the production
bars stage writes them). No artifacts are stored: the engine is rebuilt per job (seconds) and
cached in the process.
"""

from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger("rebar.altsolver")

from ..pipeline import analysis_variant
from ..v2 import bars as bars_module
from ..v2.pipeline import V2Pipeline
from ..v2.repair import fill_gaps
from .engine import SceneEngine, build_engine, cover_bounds, solve_n


class AltSolverPipeline(V2Pipeline):
    def __init__(self, store: Any, settings: Any) -> None:
        super().__init__(store, settings)
        self._engines: dict[str, SceneEngine] = {}

    def dispatch(self, job: Any, worker_id: str) -> None:
        """One log line per job (kind, task, N, outcome, duration): jobs are short-lived pods."""
        kind, task_id = str(job.get("kind", "")), str(job.get("task_id", ""))
        n = (job.get("payload") or {}).get("n")
        started = time.perf_counter()
        try:
            super().dispatch(job, worker_id)
        except Exception as exc:
            log.error("alt-solver %s task=%s n=%s FAILED in %.1fs: %s: %s", kind, task_id, n, time.perf_counter() - started, type(exc).__name__, exc)
            raise
        row = None if n is None else self.v2.get_n(task_id, int(n))
        outcome = "-" if row is None else f"{row.get('state')}/{row.get('status')}"
        log.info("alt-solver %s task=%s n=%s -> %s in %.1fs", kind, task_id, n, outcome, time.perf_counter() - started)

    # ------------------------------------------------------------- helpers
    def _rows(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        return list(self.store.resolved_scene_polygons(
            str(task["scene_id"]), variant=analysis_variant(bool(task.get("smooth"))),
            overlay_id=int(task.get("overlay_id") or 0),
        ))

    def _engine(self, task_id: str, task: dict[str, Any]) -> tuple[SceneEngine, list[dict[str, Any]]]:
        rows = self._rows(task)
        scene = self._engines.get(task_id)
        if scene is None:
            scene = build_engine(rows, config=dict(task.get("config") or {}), settings=self.settings)
            self._engines[task_id] = scene
        return scene, rows

    # ----------------------------------------------------------- preparing
    def handle_prepare(self, task_id: str, _n: int | None, worker_id: str) -> None:
        task = self.v2.get_task(task_id)
        if task is None or task.get("cancelled"):
            return
        if str(task.get("state")) == "ready":
            self.schedule_pending(task_id)
            return
        self.v2.set_task(task_id, state="preparing")
        for n in self.v2.ns_in_states(task_id, ["pending"]):
            self.v2.set_n(task_id, int(n), state="preparing")
        from A101.calculate_mass import ReinforcementCapacityError

        try:
            scene, _rows = self._engine(task_id, task)
        except ReinforcementCapacityError as exc:
            info = {
                "reason": "reinforcement_capacity", "load": float(exc.load),
                "max_supported_load": float(exc.max_supported_load), "max_layers": exc.max_layers,
                "back_grid": None if exc.back_grid is None else list(exc.back_grid), "solver": "canon_pool_cpsat",
            }
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
            bounds = cover_bounds(scene, settings=self.settings, time_limit=self._solver_time_limit(task))
        except Exception as exc:  # noqa: BLE001 - every N must learn about the failure
            message = f"{type(exc).__name__}: {exc}"
            self.v2.set_task(task_id, state="error", error=message)
            for n in self.v2.ns_in_states(task_id, ["pending", "preparing"]):
                self.v2.set_n(task_id, int(n), state="error", error=message)
            raise
        max_useful_n = bounds["max_useful_n"]
        if max_useful_n is None:  # the unconstrained solve hit its time limit: fall back to the demand size
            max_useful_n = min(int(self.settings.max_n), int(scene.info.get("demand_cells", 0)))
        max_useful_n = min(int(self.settings.max_n), int(max_useful_n))
        min_useful_n = bounds.get("min_useful_n")
        info = {"reason": bounds["reason"], "max_useful_n": max_useful_n, "min_useful_n": min_useful_n,
                "bounds": bounds.get("bounds"), **scene.info}
        self.v2.set_task(task_id, state="ready", max_useful_n=max_useful_n, min_useful_n=min_useful_n, prepare_info=info)
        self.schedule_pending(task_id)

    # ------------------------------------------------------------- solving
    def handle_solve(self, task_id: str, n: int | None, worker_id: str) -> None:
        assert n is not None
        if not self._stage_open(task_id, n, "solving"):
            return
        task = self.v2.get_task(task_id)
        if task is None:
            self.v2.set_n(task_id, n, state="error", error="task is missing")
            return
        config = dict(task.get("config") or {})
        scene, rows = self._engine(task_id, task)
        solution = solve_n(scene, int(n), settings=self.settings, time_limit=self._solver_time_limit(task))
        if self._cancelled(task_id, n):
            return
        if solution.boxes is None:
            status = solution.status.upper()
            if status in {"INFEASIBLE", "NO-SOLUTION"}:
                self.v2.set_n(task_id, n, state="success", status="infeasable", fun=None,
                              error=f"набор из {n} прямоугольников не покрывает требование ({status})")
            else:
                self.v2.set_n(task_id, n, state="error", error=f"solver status {status}")
            return
        polygons = bars_module.physical_polygons(rows)
        min_step = config.get("min_bar_gap_mm")
        out = bars_module.layout_zones(
            polygons, solution.zones, axis=scene.axis, anchor_factor=float(config.get("anchor_factor", 40.0)),
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
        if bool(config.get("fill_gaps", True)):
            out = fill_gaps(
                rows, out, axis=scene.axis, anchor_factor=float(config.get("anchor_factor", 40.0)),
                cover_mm=float(config.get("cover_mm", 30.0)),
                steel_density_kg_m3=float(config.get("steel_density_kg_m3", 7850.0)),
            )
            if self._cancelled(task_id, n):
                return
        status = "optimal" if solution.status.upper() == "OPTIMAL" else "feasable"
        self.v2.set_n(
            task_id, n, state="success", status=status, error=None,
            fun=None if solution.mass_kg is None else float(solution.mass_kg),
            mass_metrics=out["mass_metrics"],
            result={"bars": out["bars"], "zones": out["zones"], "repair": out.get("repair"), "solver": solution.info},
        )

    # the fit and bars stages are folded into handle_solve; a stray job of those kinds is a no-op
    def handle_fit(self, task_id: str, n: int | None, worker_id: str) -> None:
        return

    def handle_bars(self, task_id: str, n: int | None, worker_id: str) -> None:
        return
