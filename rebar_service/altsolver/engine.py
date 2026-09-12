"""Scene rows → experiments ``Engine`` → K boxes → wire zones."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

import numpy as np

_VENDOR = Path(__file__).resolve().parents[2] / "vendor"
for _p in (str(_VENDOR), str(_VENDOR / "experiments")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agglo  # noqa: E402  (vendor/experiments)
import ilp  # noqa: E402
from a101_reinforcement.catalog import (  # noqa: E402
    Background, BandMapping, RebarOption, area_cm2_per_m, ladder_options,
)
from a101_reinforcement.dxf_io import Mosaic  # noqa: E402
from a101_reinforcement.grid import build_grid, denoise_bands  # noqa: E402

from ..v2.bars import _bar_direction, _canonical_direction, _frame  # noqa: E402
from ..v2.verification import wire_overlay_state  # noqa: E402


@dataclass
class SceneEngine:
    engine: Any                      # agglo.Engine
    background: tuple[float, float]  # (d, step) of the background grid
    options: list[str]               # ladder labels (engine.labels[1:])
    axis: str
    info: dict[str, Any] = field(default_factory=dict)


@dataclass
class AltSolution:
    status: str                      # OPTIMAL | FEASIBLE | GAP | INFEASIBLE | ...
    boxes: np.ndarray | None
    zones: list[dict[str, Any]]      # bg zone + additional zones (wire format)
    mass_kg: float | None            # the experiment's own mass model
    bound_kg: float | None
    info: dict[str, Any]


# ---------------------------------------------------------------- scene -> mosaic


def _quad(points: Sequence[Sequence[float]]) -> np.ndarray:
    pts = [(float(x), float(y)) for x, y in points]
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) == 3:
        pts.append(pts[-1])
    if len(pts) != 4:
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        pts = [(min(xs), min(ys)), (max(xs), min(ys)), (max(xs), max(ys)), (min(xs), max(ys))]
    return np.array(pts, dtype=float)


def mosaic_from_rows(rows: Sequence[Mapping[str, Any]], *, axis: str, background_area: float) -> Mosaic:
    """Build the experiments' ``Mosaic`` from resolved scene rows.

    The production ``load`` of a DXF scene is the legend bound minus 1 (see A101.read_dxf), so
    the band upper bound is ``load + 1``; background-only polygons demand the background area.
    Removed polygons are dropped.
    """
    polygons, required = [], []
    for row in rows:
        state = wire_overlay_state(row.get("overlay_state"))
        if state == "empty":
            continue
        load = float(row.get("load", 0.0) or 0.0) if state == "active" else 0.0
        polygons.append(_quad(row["points"]))
        required.append(load + 1.0 if load > 0 else float(background_area))
    if not polygons:
        raise ValueError("сцена не содержит полигонов")
    req = np.array(required, dtype=float)
    levels = sorted({float(v) for v in req})
    scale = []
    lower = 0.0
    for i, upper in enumerate(levels):
        scale.append((i, lower, upper))
        lower = upper
    band_of = {upper: i for i, (_, _, upper) in enumerate(scale)}
    bands = np.array([band_of[float(v)] for v in req], dtype=int)
    return Mosaic(
        path=Path("scene.dxf"), polygons=np.array(polygons), colors=bands.copy(), bands=bands,
        required=req, scale=scale, direction="X" if str(axis).lower() == "x" else "Y", face="bottom",
    )


def _map_bands(scale, background: Background, options: list[RebarOption], policy: str) -> list[BandMapping]:
    """``a101_reinforcement.catalog.map_bands`` with an explicit option ladder."""
    tol = max(0.05, 0.02 * background.as_cm2_m)
    mapped = []
    for i, (color, lower, upper) in enumerate(scale):
        if upper <= background.as_cm2_m + tol:
            mapped.append(BandMapping(i, color, lower, upper, None, background.as_cm2_m, 0.0))
            continue
        need = upper - background.as_cm2_m
        if policy == "ceil":
            covering = [o for o in options if o.as_cm2_m >= need - tol]
            option = covering[0] if covering else options[-1]
        else:
            option = min(options, key=lambda o: (abs(o.as_cm2_m - need), o.as_cm2_m))
        total = background.as_cm2_m + option.as_cm2_m
        mapped.append(BandMapping(i, color, lower, upper, option, total, (upper - total) / upper))
    return mapped


def build_engine(rows: Sequence[Mapping[str, Any]], *, config: Mapping[str, Any], settings: Any) -> SceneEngine:
    """Resolve background/ladder like the production pipeline, rasterise the scene, build the Engine."""
    from A101.calculate_mass import resolve_rebar_config

    axis = str(config.get("axis", "y")).lower()
    active = [{"load": float(r.get("load", 0.0) or 0.0)}
              for r in rows if wire_overlay_state(r.get("overlay_state")) == "active"]
    back_grid = config.get("back_grid")
    stock = config.get("stock")
    cfg = resolve_rebar_config(
        active or [{"load": 0.0}],
        back_grid=None if back_grid is None else (back_grid["d"], back_grid["step"]),
        stock=None if stock is None else [(row["d"], row["step"]) for row in stock],
        max_layers=config.get("max_layers"),
    )
    bg_d, bg_step = (float(v) for v in cfg["back_grid"])
    background = Background(int(bg_d), int(bg_step), area_cm2_per_m(bg_d, bg_step))
    if stock is not None:
        options = sorted({RebarOption.make(int(d), int(step)) for d, step in cfg["stock"]})
    else:
        options = ladder_options(background)
    mosaic = mosaic_from_rows(rows, axis=axis, background_area=background.as_cm2_m)
    mapping = _map_bands(mosaic.scale, background, options, str(settings.alt_solver_band_policy))
    bands = denoise_bands(mosaic, int(settings.alt_solver_isolated_fe))
    grid = build_grid(mosaic, bands, mapping, cell_mm=float(settings.alt_solver_cell_mm))
    min_width_mm = float(config.get("min_width_mm", 1000.0) or 0.0)
    engine = agglo.Engine(grid, min_width_fe=min_width_mm / float(grid.fe_v_mm), anch_d=float(config.get("anchor_factor", 40.0)))
    info = {
        "solver": "canon_pool_cpsat", "grid": [int(v) for v in grid.need.shape], "cell_mm": float(grid.cell_mm),
        "demand_cells": int((grid.need > 0).sum()), "background": [bg_d, bg_step],
        "options": engine.labels[1:], "band_policy": str(settings.alt_solver_band_policy),
        "bands": [{"upper": float(m.upper), "option": None if m.option is None else m.option.label} for m in mapping],
    }
    return SceneEngine(engine=engine, background=(bg_d, bg_step), options=engine.labels[1:], axis=axis, info=info)


# ------------------------------------------------------------------- solve


def _args(settings: Any, time_limit: float | None) -> SimpleNamespace:
    return SimpleNamespace(
        cell=float(settings.alt_solver_cell_mm), isolated_fe=int(settings.alt_solver_isolated_fe), min_width_fe=2,
        anch_d=agglo.ANCH_D, lattice=0, cap=int(settings.alt_solver_cap), solver="cpsat",
        time_limit=float(time_limit if time_limit else settings.alt_solver_time_limit), mip_gap=0.002,
        max_nnz=20_000_000, workers=int(settings.alt_solver_workers), verbose=False, polish_time=20.0, harvest_all=False,
    )


def boxes_to_zones(scene: SceneEngine, boxes: np.ndarray, *, next_id: int = 1) -> list[dict[str, Any]]:
    """Experiment boxes (cell indices) → wire zones: one ``bg`` zone plus one zone per box (per layer)."""
    eng, axis = scene.engine, scene.axis
    bg_d, bg_step = scene.background
    zones: list[dict[str, Any]] = [{"id": 0, "kind": "bg", "arm": {"d": bg_d, "step": bg_step}, "anchorage": None}]
    if boxes is None or len(boxes) == 0:
        return zones
    rows = eng.zone_rows(boxes)
    _, rk = eng.cost(boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3])
    direction = _canonical_direction(axis)
    cross, long = _frame(axis)
    sign_cross = direction[cross]
    sign_long = _bar_direction(direction)[long]
    for row, r in zip(rows, rk):
        d, step, layers = float(eng.diam[r]), float(eng.step[r]), int(eng.layers[r])
        n_bars = max(2, int(round(row["width_mm"] / step)) + 1)
        lo = (row["x0"], row["y0"]); hi = (row["x1"], row["y1"])
        c_center = (lo[cross] + hi[cross]) / 2.0
        c_first, c_last = c_center - (n_bars - 1) * step / 2.0, c_center + (n_bars - 1) * step / 2.0
        origin = [0.0, 0.0]
        origin[cross] = c_first if sign_cross > 0 else c_last
        origin[long] = lo[long] if sign_long > 0 else hi[long]
        length = float(hi[long] - lo[long])
        for _ in range(layers):
            zones.append({"id": next_id, "kind": "additional", "arm": {"d": d, "step": step}, "left": 0,
                          "right": n_bars - 1, "length": length, "anchorage": None,
                          "origin": [float(origin[0]), float(origin[1])],
                          "direction": [float(direction[0]), float(direction[1])]})
            next_id += 1
    return zones


def solve_n(scene: SceneEngine, k: int, *, settings: Any, time_limit: float | None = None) -> AltSolution:
    """Select at most ``k`` boxes covering every deficit cell at minimum mass (nested-lattice warm start)."""
    eng = scene.engine
    if not (eng.need > 0).any():
        return AltSolution("OPTIMAL", np.empty((0, 4), np.int64), boxes_to_zones(scene, None), 0.0, 0.0, {"pool": 0})
    args = _args(settings, time_limit)
    t0 = time.perf_counter()
    pool, lattice = ilp.canon_pool_auto(eng, 0, args.cap)
    hint = None
    for coarse in (4 * lattice, 2 * lattice):
        cp = ilp.canon_pool(eng, coarse, args.cap)
        rr = ilp.solve(eng, cp, k, args, hint, time_limit=args.time_limit * 0.25)
        if rr["boxes"] is not None:
            hint = rr["boxes"]
    cand = pool if hint is None else np.unique(np.concatenate([pool, hint]), axis=0)
    r = ilp.solve(eng, cand, k, args, hint)
    info = {"pool": int(len(pool)), "lattice": int(lattice), "candidates": int(len(cand)), "status": r["status"],
            "rows": r.get("rows"), "t_solve": round(float(r.get("t_solve", 0.0)), 1),
            "t_total": round(time.perf_counter() - t0, 1)}
    boxes = None if r["boxes"] is None else np.asarray(r["boxes"], np.int64)
    zones = boxes_to_zones(scene, boxes)
    return AltSolution(str(r["status"]), boxes, zones, r.get("mass"), r.get("bound"), info)
