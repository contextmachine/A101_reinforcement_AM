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
from ..v2.verification import reinforcement_rows
from .engine import SceneEngine, build_engine, cover_bounds, solve_n


def _residual(rows: Any, bars: Any, cover_mm: float, tol: float = 0.5) -> tuple[int, float]:
    """(short polygons, worst shortfall) of ``bars`` on ``rows`` by the verification model."""
    verified = reinforcement_rows(rows, bars, steel_density_kg_m3=7850.0, t_mm=1000.0, cover_mm=cover_mm)
    gaps = [v["need_load_sm2/m"] - v["fact_load_sm2/m"] for v in verified
            if v.get("need_load_sm2/m") is not None and v.get("fact_load_sm2/m") is not None]
    return (sum(1 for g in gaps if g > tol), max(gaps, default=0.0))


class AltSolverPipeline(V2Pipeline):
    def __init__(self, store: Any, settings: Any) -> None:
        super().__init__(store, settings)
        self._engines: dict[tuple[str, str], SceneEngine] = {}

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

    def _engine(self, task_id: str, task: dict[str, Any], policy: str | None = None) -> tuple[SceneEngine, list[dict[str, Any]]]:
        rows = self._rows(task)
        policy = str(policy or self.settings.alt_solver_band_policy)
        scene = self._engines.get((task_id, policy))
        if scene is None:
            scene = build_engine(rows, config=dict(task.get("config") or {}), settings=self.settings, band_policy=policy)
            self._engines[(task_id, policy)] = scene
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
        """One N: solve with the configured band policy; if elements are still short after the layout
        and the gap filling, redo the N with the fallback policy (``ceil``) and keep the better attempt."""
        assert n is not None
        if not self._stage_open(task_id, n, "solving"):
            return
        task = self.v2.get_task(task_id)
        if task is None:
            self.v2.set_n(task_id, n, state="error", error="task is missing")
            return
        primary = str(self.settings.alt_solver_band_policy)
        fallback = str(self.settings.alt_solver_fallback_policy or "").strip()
        policies = [primary] + ([fallback] if fallback and fallback != primary else [])
        attempts: list[dict[str, Any]] = []
        best: dict[str, Any] | None = None
        for policy in policies:
            attempt = self._attempt(task_id, task, int(n), policy)
            if attempt is None:  # cancelled or a terminal state already written
                return
            attempts.append(attempt["summary"])
            if best is None or attempt["residual"] < best["residual"]:
                best = attempt
            if attempt["residual"][0] == 0:
                break
        assert best is not None
        out, solution = best["out"], best["solution"]
        info = {**solution.info, "band_policy": best["policy"], "attempts": attempts}
        status = "optimal" if solution.status.upper() == "OPTIMAL" else "feasable"
        self.v2.set_n(
            task_id, n, state="success", status=status, error=None,
            fun=None if solution.mass_kg is None else float(solution.mass_kg),
            mass_metrics=out["mass_metrics"],
            result={"bars": out["bars"], "zones": out["zones"], "repair": out.get("repair"), "solver": info},
        )

    def _attempt(self, task_id: str, task: dict[str, Any], n: int, policy: str) -> dict[str, Any] | None:
        """Solve + layout + fill for one band policy. Returns ``None`` after writing a terminal
        state (infeasible cover, infeasible layout, solver failure) or on cancellation."""
        config = dict(task.get("config") or {})
        scene, rows = self._engine(task_id, task, policy)
        solution = solve_n(scene, n, settings=self.settings, time_limit=self._solver_time_limit(task))
        if self._cancelled(task_id, n):
            return None
        if solution.boxes is None:
            status = solution.status.upper()
            if status in {"INFEASIBLE", "NO-SOLUTION"}:
                self.v2.set_n(task_id, n, state="success", status="infeasable", fun=None,
                              error=f"набор из {n} прямоугольников не покрывает требование ({status}, {policy})")
            else:
                self.v2.set_n(task_id, n, state="error", error=f"solver status {status} ({policy})")
            return None
        polygons = bars_module.physical_polygons(rows)
        min_step = config.get("min_bar_gap_mm")
        out = bars_module.layout_zones(
            polygons, solution.zones, axis=scene.axis, anchor_factor=float(config.get("anchor_factor", 40.0)),
            steel_density_kg_m3=float(config.get("steel_density_kg_m3", 7850.0)),
            min_step=float(min_step) if min_step else float(self.settings.min_internal_step),
        )
        if self._cancelled(task_id, n):
            return None
        if not out.get("is_feasible"):
            self.v2.set_n(
                task_id, n, state="success", status="infeasable",
                error=f"bar layout {out.get('status', 'infeasible')} ({policy}): {out.get('warnings') or out.get('errors')}",
            )
            return None
        cover = float(config.get("cover_mm", 30.0))
        if bool(config.get("fill_gaps", True)):
            out = fill_gaps(
                rows, out, axis=scene.axis, anchor_factor=float(config.get("anchor_factor", 40.0)),
                cover_mm=cover, steel_density_kg_m3=float(config.get("steel_density_kg_m3", 7850.0)),
            )
            if self._cancelled(task_id, n):
                return None
            short = out["repair"]["short_after"]
            residual = (int(short["polygons"]), float(short["worst_cm2_m"]))
        else:
            residual = _residual(rows, out["bars"], cover)
        mass = float(out["mass_metrics"]["additional"]["with_anchorage_kg"])
        summary = {"band_policy": policy, "short_polygons": residual[0], "worst_cm2_m": round(residual[1], 2),
                   "additional_with_anchorage_kg": round(mass, 1), "solver_status": solution.status}
        log.info("alt-solver task=%s n=%s policy=%s -> short %s (worst %.2f), %.0f kg", task_id, n, policy, residual[0], residual[1], mass)
        return {"policy": policy, "out": out, "solution": solution, "residual": residual, "summary": summary}

    # the fit and bars stages are folded into handle_solve; a stray job of those kinds is a no-op
    def handle_fit(self, task_id: str, n: int | None, worker_id: str) -> None:
        return

    def handle_bars(self, task_id: str, n: int | None, worker_id: str) -> None:
        return
