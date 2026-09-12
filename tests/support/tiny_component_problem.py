"""A tiny real prepared component problem for solver-level tests (no IO)."""

from __future__ import annotations

from shapely.geometry import Polygon
from shapely.ops import unary_union

from A101.calculate_mass import resolve_rebar_config
from A101.reinforcement_components import prepare_component_problem, split_reinforcement_components

# A checkerboard of three demand classes: work matrix [[1, 2, 3], [2, 1, 3]] with
# min N = 2, so every N >= 2 goes through the real MILP (no n=1 fast path).
ROWS = [
    {"points": [[0, 0], [1200, 0], [1200, 1200], [0, 1200]], "load": 12.0},
    {"points": [[1200, 0], [2400, 0], [2400, 1200], [1200, 1200]], "load": 19.0},
    {"points": [[0, 1200], [1200, 1200], [1200, 2400], [0, 2400]], "load": 19.0},
    {"points": [[1200, 1200], [2400, 1200], [2400, 2400], [1200, 2400]], "load": 12.0},
    {"points": [[2400, 0], [3600, 0], [3600, 2400], [2400, 2400]], "load": 25.0},
]
BACK_GRID = (16, 300)
STOCK = [(16, 300), (20, 150), (20, 100)]


def tiny_polygons():
    return [{"geometry": Polygon(row["points"]), "load": float(row["load"])} for row in ROWS]


def tiny_cfg(polygons=None):
    polygons = tiny_polygons() if polygons is None else polygons
    cfg = resolve_rebar_config(polygons, back_grid=BACK_GRID, stock=STOCK, max_layers=2)
    cfg["axis"] = "y"
    return cfg


def tiny_component(polygons=None, cfg=None, axis="y"):
    polygons = tiny_polygons() if polygons is None else polygons
    cfg = tiny_cfg(polygons) if cfg is None else cfg
    split = split_reinforcement_components(
        polygons,
        load2cls=cfg["load2cls"],
        recipes=cfg.get("recipes"),
        diameters=cfg["diameters"],
        anchor_factor=40.0,
        axis=axis,
    )
    assert split["components"], "tiny scene must produce at least one demand component"
    return split["components"][0]


def tiny_problem(*, max_n=4, max_n_resolver=None, axis="y", polygons=None, cfg=None):
    polygons = tiny_polygons() if polygons is None else polygons
    cfg = tiny_cfg(polygons) if cfg is None else cfg
    component = tiny_component(polygons, cfg, axis=axis)
    field_geometry = unary_union([row["geometry"] for row in polygons])
    problem = prepare_component_problem(
        component,
        load2cls=cfg["load2cls"],
        recipes=cfg.get("recipes"),
        densities=cfg["densities"],
        diameters=cfg["diameters"],
        anchor_factor=40.0,
        axis=axis,
        min_width=300.0,
        grid_size=300.0,
        fill_notches_threshold=0,
        short_edge_threshold=0,
        simplify_steps_threshold=0,
        max_n=max_n,
        max_n_resolver=max_n_resolver,
        use_mosaic=False,
        preserve_demand_classes=True,
        strict_grid_coverage=True,
        refine_unrepresentable_cells=False,
        refine_mixed_cells=False,
        physical_geometry=field_geometry,
    )
    return problem, cfg, field_geometry
