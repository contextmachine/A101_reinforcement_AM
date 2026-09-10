from __future__ import annotations

from collections import defaultdict
from math import ceil
from typing import Any, Callable, Mapping, Sequence

import numpy as np
from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPolygon, Polygon, box, shape
from shapely.ops import unary_union
from shapely.strtree import STRtree

from .axis_orientation import (
    add_box_anchorage,
    class_holds,
    grid_rectangles_to_world,
    normalize_axis,
    orient_geometry,
)

_EPS = 1e-7
VERSION = "v10-robust-global-layout-2026-09-04"


class _DSU:
    def __init__(self, n: int):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: int, right: int) -> None:
        left, right = self.find(left), self.find(right)
        if left == right:
            return
        if self.rank[left] < self.rank[right]:
            left, right = right, left
        self.parent[right] = left
        if self.rank[left] == self.rank[right]:
            self.rank[left] += 1


def _polygon_parts(geometry: Any) -> list[Polygon]:
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    if isinstance(geometry, GeometryCollection):
        return [part for child in geometry.geoms for part in _polygon_parts(child)]
    return []


def _geom(value: Any):
    if hasattr(value, "geom_type"):
        return value
    if isinstance(value, Mapping):
        if value.get("geometry") is not None:
            return _geom(value["geometry"])
        if value.get("points") is not None:
            return Polygon(value["points"])
        if value.get("type") and value.get("coordinates") is not None:
            return shape(value)
    if isinstance(value, (list, tuple)):
        if len(value) == 2:
            try:
                return _geom(value[0])
            except Exception:
                pass
        return Polygon([tuple(map(float, point[:2])) for point in value])
    raise ValueError("Полигон должен быть Shapely-геометрией, записью с geometry/points или координатами")


def _clean_geometry(geometry: Any):
    if geometry is None:
        return geometry
    if not geometry.is_valid:
        geometry = geometry.buffer(0)
    return geometry


def _normalize_recipes(recipes: Mapping[Any, Sequence[Any]] | None) -> dict[int, tuple[int, ...]]:
    out: dict[int, tuple[int, ...]] = {}
    for raw_key, raw_value in dict(recipes or {}).items():
        values = (raw_value,) if np.isscalar(raw_value) else tuple(raw_value)
        out[int(raw_key)] = tuple(map(int, values))
    return out


def _normalize_class_floats(values: Mapping[Any, float] | None) -> dict[int, float]:
    return {int(key): float(value) for key, value in dict(values or {}).items()}


def _load_class(load2cls: Mapping[Any, Any], load: Any, atol: float = 1e-8):
    if load in load2cls:
        return int(load2cls[load])
    text = str(load)
    if text in load2cls:
        return int(load2cls[text])
    try:
        value = float(load)
    except (TypeError, ValueError) as exc:
        raise KeyError(load) from exc
    hits = [int(cls) for raw, cls in load2cls.items() if np.isclose(float(raw), value, rtol=0.0, atol=atol)]
    if len(hits) != 1:
        raise KeyError(load)
    return hits[0]


def _unique_sorted(values, eps=_EPS):
    out = []
    for value in sorted(map(float, values)):
        if not out or value - out[-1] > float(eps):
            out.append(value)
    return out


def _dense_grid_shape(grid, eps=_EPS):
    """Return (rows, columns, dense_cells) implied by rectangular cell edges."""
    bounds = [tuple(map(float, _geom(row).bounds)) for row in grid]
    if not bounds:
        return 0, 0, 0
    nx = max(0, len(_unique_sorted([x for b in bounds for x in (b[0], b[2])], eps)) - 1)
    ny = max(0, len(_unique_sorted([y for b in bounds for y in (b[1], b[3])], eps)) - 1)
    return ny, nx, ny * nx


def _dense_refinement_too_large(base_shape, refined_shape, max_dense_cells, max_refinement_factor):
    cells = int(refined_shape[2])
    if max_dense_cells is not None and cells > int(max_dense_cells):
        return True
    if max_refinement_factor is not None and int(base_shape[2]) > 0:
        return cells > float(max_refinement_factor) * int(base_shape[2])
    return False


def _grid_to_matrices(grid, load2cls, eps=_EPS):
    """Convert possibly locally refined rectangular cells to dense matrices."""

    rows = [dict(row) for row in grid]
    if not rows:
        raise ValueError("Пустая сетка")
    bounds = [tuple(map(float, _geom(row).bounds)) for row in rows]
    xs = np.asarray(_unique_sorted([x for b in bounds for x in (b[0], b[2])], eps), dtype=float)
    ys = np.asarray(_unique_sorted([y for b in bounds for y in (b[1], b[3])], eps), dtype=float)
    if len(xs) < 2 or len(ys) < 2:
        raise ValueError("Некорректные границы сетки")

    zero_load = next((raw for raw, cls in load2cls.items() if int(cls) == 0), 0.0)
    try:
        zero_load = float(zero_load)
    except (TypeError, ValueError):
        zero_load = 0.0
    load_matrix = np.full((len(ys) - 1, len(xs) - 1), zero_load, dtype=float)
    class_matrix = np.zeros((len(ys) - 1, len(xs) - 1), dtype=np.int32)
    written = np.zeros_like(class_matrix, dtype=bool)

    def index(values, value):
        i = int(np.searchsorted(values, float(value)))
        options = [j for j in (i - 1, i) if 0 <= j < len(values)]
        return min(options, key=lambda j: abs(values[j] - float(value)))

    for row, b in zip(rows, bounds):
        i0, i1 = index(xs, b[0]), index(xs, b[2])
        j0, j1 = index(ys, b[1]), index(ys, b[3])
        if i1 <= i0 or j1 <= j0:
            continue
        raw_load = row.get("load", row.get("value", row.get("class", zero_load)))
        cls = int(row.get("demand_class", _load_class(load2cls, raw_load)))
        try:
            numeric_load = float(raw_load)
        except (TypeError, ValueError):
            numeric_load = float(next(raw for raw, value in load2cls.items() if int(value) == cls))
        overlap = written[j0:j1, i0:i1] & (class_matrix[j0:j1, i0:i1] != cls)
        if np.any(overlap):
            raise ValueError("Локально уточнённые ячейки перекрываются с разными классами")
        load_matrix[j0:j1, i0:i1] = numeric_load
        class_matrix[j0:j1, i0:i1] = cls
        written[j0:j1, i0:i1] = True
    return xs, ys, load_matrix, class_matrix


def preserve_grid_demand_classes(
    grid: Sequence[Mapping[str, Any]],
    source_polygons: Sequence[Any],
    *,
    load2cls: Mapping[Any, Any],
    recipes: Mapping[Any, Sequence[Any]] | None = None,
    densities: Mapping[Any, float] | None = None,
    area_eps: float = 1e-6,
    strict: bool = True,
    refine_unrepresentable: bool = True,
    refine_mixed_cells: bool = False,
    max_subcells_per_cell: int = 2000,
    return_stats: bool = False,
):
    """Preserve demand by the highest intersecting ranked class.

    Classes are monotone: class ``c`` covers every class ``<= c``. Therefore a
    clustered cell intersecting classes ``2`` and ``3`` receives class ``3``;
    requirements are never summed and the cell is not split. ``recipes`` are
    intentionally irrelevant here and are retained only for API compatibility.
    """

    class_loads: dict[int, list[Any]] = defaultdict(list)
    for raw_load, cls in load2cls.items():
        class_loads[int(cls)].append(raw_load)

    def canonical_load(cls, preferred=None):
        cls = int(cls)
        if preferred is not None:
            try:
                if _load_class(load2cls, preferred) == cls:
                    return float(preferred)
            except Exception:
                pass
        if cls not in class_loads:
            return float(cls)
        raw = class_loads[cls][0]
        try:
            return float(raw)
        except (TypeError, ValueError):
            return raw

    sources = []
    for index, raw in enumerate(source_polygons):
        if isinstance(raw, Mapping):
            geometry = _clean_geometry(_geom(raw))
            load = raw.get("load", raw.get("value", raw.get("class")))
        elif isinstance(raw, (tuple, list)) and len(raw) == 2:
            geometry, load = _clean_geometry(_geom(raw[0])), raw[1]
        else:
            raise ValueError(f"source_polygons[{index}] должен содержать geometry и load")
        sources.append({"geometry": geometry, "load": load,
                        "class": int(_load_class(load2cls, load)), "index": index})

    tree = STRtree([row["geometry"] for row in sources]) if sources else None
    output = []
    promoted = combined = 0
    promoted_area = 0.0
    before, after = set(), set()

    for cell_index, raw_cell in enumerate(grid):
        cell = dict(raw_cell)
        geometry = _clean_geometry(_geom(cell))
        current_load = cell.get("load", cell.get("value", cell.get("class", 0)))
        try:
            current_class = int(_load_class(load2cls, current_load))
        except KeyError:
            current_class = 0
        before.add(current_class)

        ids = [] if tree is None else list(map(int, tree.query(geometry, predicate="intersects")))
        overlaps = []
        for i in ids:
            area = float(geometry.intersection(sources[i]["geometry"]).area)
            if area > float(area_eps):
                overlaps.append((sources[i], area))

        classes = sorted({int(row["class"]) for row, _ in overlaps}) or [current_class]
        chosen = max(classes)
        if len(classes) > 1:
            combined += 1

        same = max((item for item in overlaps if int(item[0]["class"]) == chosen),
                   key=lambda item: item[1], default=None)
        x0, y0, x1, y1 = map(float, geometry.bounds)
        result = dict(cell)
        result.update({
            "x": x0,
            "y": y0,
            "width": x1 - x0,
            "height": y1 - y0,
            "geometry": geometry,
            "load": canonical_load(chosen, current_load),
            "demand_class": chosen,
            "combined_source_classes": tuple(classes),
            "ranked_max_class": chosen,
        })
        if chosen != current_class:
            result["class_preserved"] = True
            result["preserved_source_index"] = None if same is None else int(same[0]["index"])
            promoted += 1
            promoted_area += float(geometry.area)
        output.append(result)
        after.add(chosen)

    # Check the actual ranked requirement, not only geometric grid coverage.
    output_geometries = [_geom(row) for row in output]
    output_tree = STRtree(output_geometries) if output_geometries else None
    missing = []
    missing_area = {}
    for source in sources:
        cls = int(source["class"])
        if cls <= 0:
            continue
        ids = [] if output_tree is None else list(map(int, output_tree.query(source["geometry"], predicate="intersects")))
        capable = [output_geometries[i] for i in ids if int(output[i]["demand_class"]) >= cls]
        covered = unary_union(capable) if capable else GeometryCollection()
        area = float(source["geometry"].difference(covered).area)
        if area > float(area_eps):
            missing.append(int(source["index"]))
            missing_area[int(source["index"])] = area

    if strict and missing:
        raise ValueError(
            "Кластеризованная сетка не сохраняет ранжированные классы demand-полигонов: "
            + ", ".join(f"{i} (area={missing_area[i]:.6g})" for i in missing[:20])
        )

    stats = {
        "grid_cells_before": len(grid),
        "grid_cells": len(output),
        "promoted_cells": promoted,
        "combined_cells": combined,
        "ranked_max_cells": combined,
        "refined_cells": 0,
        "refined_subcells": 0,
        "promoted_area": promoted_area,
        "classes_before": sorted(before),
        "classes_after": sorted(after),
        "missing_source_indices": missing,
        "missing_source_area": missing_area,
        "unrepresentable_cells": [],
        "refine_mixed_cells_ignored": bool(refine_mixed_cells),
    }
    return (output, stats) if return_stats else output

def _line_intervals(geometry: Any, x: float) -> list[tuple[float, float]]:
    if geometry.is_empty:
        return []
    _, ymin, _, ymax = geometry.bounds
    hit = geometry.intersection(LineString([(x, ymin - 1.0), (x, ymax + 1.0)]))
    intervals: list[tuple[float, float]] = []

    def add(item: Any) -> None:
        if item.is_empty:
            return
        if isinstance(item, LineString):
            ys = [float(p[1]) for p in item.coords]
            if max(ys) > min(ys) + _EPS:
                intervals.append((min(ys), max(ys)))
        elif isinstance(item, (MultiLineString, GeometryCollection)):
            for child in item.geoms:
                add(child)

    add(hit)
    return intervals


def _vertical_dilate_orthogonal(geometry: Any, distance: float):
    """Minkowski sum with a vertical segment for orthogonal polygons."""

    distance = float(distance)
    if distance <= 0 or geometry.is_empty:
        return geometry
    xs = sorted(
        {
            float(x)
            for polygon in _polygon_parts(geometry)
            for ring in (polygon.exterior, *polygon.interiors)
            for x, _ in ring.coords
        }
    )
    rectangles = []
    for left, right in zip(xs, xs[1:]):
        if right <= left + _EPS:
            continue
        mid = (left + right) / 2
        for low, high in _line_intervals(geometry, mid):
            rectangles.append(box(left, low - distance, right, high + distance))
    if not rectangles:
        return geometry
    return _clean_geometry(unary_union(rectangles))


def directional_expand_geometry(geometry: Any, distance: float, axis: str = "y"):
    """Expand only along the reinforcement axis.

    The input is expected to be orthogonal. ``axis='x'`` reuses the exact
    vertical implementation through the same involutive X/Y transform used by
    the bar-layout wrapper.
    """

    axis = normalize_axis(axis)
    geometry = _clean_geometry(_geom(geometry))
    if distance <= 0:
        return geometry
    if axis == "y":
        return _vertical_dilate_orthogonal(geometry, distance)
    work = orient_geometry(geometry, "x")
    return orient_geometry(_vertical_dilate_orthogonal(work, distance), "x")


def _resolve_priority_overlaps(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return non-overlapping expanded records, preserving the larger load."""

    covered = None
    out: list[dict[str, Any]] = []
    for record in sorted(records, key=lambda row: (float(row["load"]), int(row["class"])), reverse=True):
        raw = record["expanded_geometry"]
        visible = raw if covered is None else raw.difference(covered)
        for part in _polygon_parts(_clean_geometry(visible)):
            if part.area > _EPS:
                out.append({**dict(record), "geometry": part})
        covered = raw if covered is None else unary_union([covered, raw])
    return out


def split_reinforcement_components(
    ortho_polygons: Sequence[Any],
    *,
    load2cls: Mapping[Any, int],
    recipes: Mapping[Any, Sequence[Any]] | None,
    diameters: Mapping[Any, float],
    anchor_factor: float = 32.0,
    axis: str = "y",
    area_eps: float = 1e-6,
) -> dict[str, Any]:
    """Split non-background demand into anchorage-aware connected components."""

    axis = normalize_axis(axis)
    recipes = _normalize_recipes(recipes)
    diameters = _normalize_class_floats(diameters)
    _, all_holds, leaves = class_holds(diameters, recipes, anchor_factor)
    background_only: list[int] = []
    degenerate: list[int] = []
    active: list[dict[str, Any]] = []

    for index, raw in enumerate(ortho_polygons):
        if isinstance(raw, Mapping):
            load = raw.get("load", raw.get("value", raw.get("class")))
            geometry = _clean_geometry(_geom(raw))
            metadata = dict(raw)
        elif isinstance(raw, (tuple, list)) and len(raw) == 2:
            geometry, load = _clean_geometry(_geom(raw[0])), raw[1]
            metadata = {}
        else:
            raise ValueError(f"ortho_polygons[{index}] должен содержать geometry и load")
        if geometry.is_empty or geometry.area <= area_eps:
            degenerate.append(index)
            continue
        try:
            cls = _load_class(load2cls, load)
        except KeyError as exc:
            raise ValueError(f"Для load={load!r} нет класса в load2cls") from exc
        if cls == 0:
            background_only.append(index)
            continue
        class_leaves = leaves.get(cls, (cls,))
        hold = float(all_holds.get(cls, max(float(anchor_factor) * float(diameters[c]) for c in class_leaves)))
        expanded = directional_expand_geometry(geometry, hold, axis)
        active.append(
            {
                "source_index": index,
                "load": float(load),
                "class": int(cls),
                "leaf_classes": tuple(class_leaves),
                "hold": hold,
                "geometry": geometry,
                "expanded_geometry": expanded,
                "raw": metadata or raw,
            }
        )

    if not active:
        return {
            "axis": axis,
            "components": [],
            "active_indices": [],
            "background_only_indices": background_only,
            "degenerate_indices": degenerate,
            "resolved_expanded_polygons": [],
            "stats": {
                "input_polygons": len(ortho_polygons),
                "active_polygons": 0,
                "background_only_polygons": len(background_only),
                "degenerate_polygons": len(degenerate),
                "components": 0,
            },
        }

    tree = STRtree([row["expanded_geometry"] for row in active])
    dsu = _DSU(len(active))
    for i, row in enumerate(active):
        for j in map(int, tree.query(row["expanded_geometry"], predicate="intersects")):
            if j > i:
                dsu.union(i, j)

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(active)):
        groups[dsu.find(i)].append(i)

    components = []
    for component_id, members in enumerate(sorted(groups.values(), key=lambda ids: min(active[i]["source_index"] for i in ids))):
        rows = [active[i] for i in members]
        expanded = unary_union([row["expanded_geometry"] for row in rows])
        demand = unary_union([row["geometry"] for row in rows])
        source_indices = sorted(int(row["source_index"]) for row in rows)
        polygons = [
            {
                **({k: v for k, v in row["raw"].items() if k not in {"geometry", "points"}} if isinstance(row["raw"], Mapping) else {}),
                "geometry": row["geometry"],
                "load": row["load"],
                "class": row["class"],
                "source_index": row["source_index"],
            }
            for row in rows
        ]
        components.append(
            {
                "id": component_id,
                "axis": axis,
                "polygon_indices": source_indices,
                "polygons": polygons,
                "geometry": expanded,
                "demand_geometry": demand,
                "bounds": tuple(map(float, expanded.bounds)),
                "demand_bounds": tuple(map(float, demand.bounds)),
                "classes": sorted({int(row["class"]) for row in rows}),
                "loads": sorted({float(row["load"]) for row in rows}),
                "max_hold": max(float(row["hold"]) for row in rows),
                "expanded_polygons": [
                    {
                        "source_index": row["source_index"],
                        "load": row["load"],
                        "class": row["class"],
                        "hold": row["hold"],
                        "geometry": row["expanded_geometry"],
                    }
                    for row in rows
                ],
            }
        )

    resolved = _resolve_priority_overlaps(active)
    return {
        "axis": axis,
        "components": components,
        "active_indices": sorted(int(row["source_index"]) for row in active),
        "background_only_indices": background_only,
        "degenerate_indices": degenerate,
        "resolved_expanded_polygons": resolved,
        "stats": {
            "input_polygons": len(ortho_polygons),
            "active_polygons": len(active),
            "background_only_polygons": len(background_only),
            "degenerate_polygons": len(degenerate),
            "components": len(components),
        },
    }


def prepare_component_problem(
    component: Mapping[str, Any],
    *,
    load2cls: Mapping[Any, int],
    recipes: Mapping[Any, Sequence[Any]] | None,
    densities: Mapping[Any, float],
    diameters: Mapping[Any, float],
    anchor_factor: float = 32.0,
    axis: str = "y",
    min_width: float = 1000.0,
    grid_size: float = 300.0,
    fill_notches_threshold: float | None = 1000.0,
    short_edge_threshold: float | None = 300.0,
    simplify_steps_threshold: float | None = 1000.0,
    max_n: int | None = None,
    use_mosaic: bool = True,
    preserve_demand_classes: bool = True,
    preserve_area_eps: float = 1e-6,
    strict_grid_coverage: bool = True,
    refine_unrepresentable_cells: bool = True,
    refine_mixed_cells: bool = False,
    max_subcells_per_cell: int = 2000,
    max_dense_cells: int | None = 20000,
    max_refinement_factor: float | None = 3.0,
    fallback_to_composite_cells: bool = True,
    progress: bool = False,
) -> dict[str, Any]:
    """Build the existing prepared solver model for one demand component."""

    # Heavy solver modules stay lazy: decomposition/frontier tests need only Shapely.
    from .cells_merging import reduce_mosaic
    from .grid_work import clean_poly, resolve_overlaps
    from .linear_idea import generate_all_rectangles, relabel_rectangle_candidates
    from .poly_bbox import fill_notches, polygons_to_grid, simplify_short_edges, simplify_steps
    from .select_min_density_rectangles_recipes import prepare_rectangle_problem
    from .axis_orientation import orient_grid

    from time import perf_counter

    component_id = int(component.get("id", 0))
    stage_times: dict[str, float] = {}

    def mark(name, started, **meta):
        elapsed = perf_counter() - started
        stage_times[name] = float(elapsed)
        if progress:
            suffix = " ".join(f"{k}={v}" for k, v in meta.items())
            print(f"[prepare component {component_id}] {name}: {elapsed:.3f}s" + (f"; {suffix}" if suffix else ""), flush=True)

    axis = normalize_axis(axis)
    recipes = _normalize_recipes(recipes)
    densities = _normalize_class_floats(densities)
    diameters = _normalize_class_floats(diameters)
    polygons = [dict(row) for row in component.get("polygons", [])]
    if not polygons:
        raise ValueError("component не содержит polygons")

    started = perf_counter()
    working = [{**row, "geometry": _clean_geometry(row["geometry"])} for row in polygons]
    if fill_notches_threshold is not None and fill_notches_threshold > 0:
        working = clean_poly(resolve_overlaps(fill_notches(working, float(fill_notches_threshold))))
    if short_edge_threshold is not None and short_edge_threshold > 0:
        working = clean_poly(resolve_overlaps(simplify_short_edges(working, float(short_edge_threshold))))
    if simplify_steps_threshold is not None and simplify_steps_threshold > 0:
        working = clean_poly(resolve_overlaps(simplify_steps(working, float(simplify_steps_threshold))))
    mark("geometry_cleanup", started, polygons=len(working))

    started = perf_counter()
    base_grid = polygons_to_grid(working, float(grid_size))
    base_dense_shape = _dense_grid_shape(base_grid)
    mark("clustered_grid", started, cells=len(base_grid), dense_shape=base_dense_shape[:2])

    grid = base_grid
    preserve_stats = {}
    refinement_fallback = False
    discarded_refined_shape = None
    if preserve_demand_classes:
        started = perf_counter()
        grid, preserve_stats = preserve_grid_demand_classes(
            base_grid, polygons, load2cls=load2cls, recipes=recipes, densities=densities,
            area_eps=preserve_area_eps, strict=strict_grid_coverage,
            refine_unrepresentable=refine_unrepresentable_cells,
            refine_mixed_cells=refine_mixed_cells,
            max_subcells_per_cell=max_subcells_per_cell,
            return_stats=True,
        )
        refined_shape = _dense_grid_shape(grid)

        if (refine_mixed_cells and fallback_to_composite_cells and
                _dense_refinement_too_large(base_dense_shape, refined_shape,
                                            max_dense_cells, max_refinement_factor)):
            discarded_refined_shape = refined_shape
            grid, preserve_stats = preserve_grid_demand_classes(
                base_grid, polygons, load2cls=load2cls, recipes=recipes, densities=densities,
                area_eps=preserve_area_eps, strict=strict_grid_coverage,
                refine_unrepresentable=refine_unrepresentable_cells,
                refine_mixed_cells=False,
                max_subcells_per_cell=max_subcells_per_cell,
                return_stats=True,
            )
            refinement_fallback = True
            preserve_stats = dict(preserve_stats)
            preserve_stats.update({
                "refinement_fallback": True,
                "discarded_refined_shape": tuple(map(int, discarded_refined_shape)),
                "fallback_reason": "dense refinement guard",
            })

        dense_shape = _dense_grid_shape(grid)
        mark("class_preservation", started, cells=len(grid), dense_shape=dense_shape[:2],
             fallback=refinement_fallback)
    else:
        dense_shape = base_dense_shape

    if max_dense_cells is not None and int(dense_shape[2]) > int(max_dense_cells):
        raise ValueError(
            f"component {component_id}: dense grid {dense_shape[0]}x{dense_shape[1]}="
            f"{dense_shape[2]} cells exceeds max_dense_cells={int(max_dense_cells)}; "
            "set refine_mixed_cells=False, increase GRID_SIZE or raise the guard explicitly"
        )

    started = perf_counter()
    xs, ys, load_matrix, int_matrix = _grid_to_matrices(grid, load2cls, preserve_area_eps)
    work_matrix, work_x_edges, work_y_edges, work_x_steps, work_y_steps = orient_grid(int_matrix, xs, ys, axis)
    base_holds, holds, recipe_leaves = class_holds(diameters, recipes, anchor_factor)
    mark("dense_matrix", started, shape=tuple(map(int, work_matrix.shape)), nonzero=int(np.count_nonzero(work_matrix)))

    requested_min_width = max(0.0, float(min_width))
    cross_span = float(work_x_edges[-1] - work_x_edges[0]) if len(work_x_edges) >= 2 else 0.0
    if cross_span <= 0:
        raise ValueError(f"component {component_id}: нулевой поперечный размер")
    candidate_min_width = min(requested_min_width, cross_span)
    small_cross_component = cross_span < requested_min_width - 1e-9

    started = perf_counter()
    requirement_rectangles = generate_all_rectangles(
        int_matrix=work_matrix,
        x_steps=work_x_steps,
        y_steps=work_y_steps,
        xs=work_x_edges,
        min_w=candidate_min_width,
        holds=holds,
    )
    mark("generate_rectangles", started, rectangles=len(requirement_rectangles))

    started = perf_counter()
    selectable = relabel_rectangle_candidates(requirement_rectangles, dict(recipes or {}))
    mark("relabel_rectangles", started, selectable=len(selectable))
    if np.any(work_matrix != 0) and not selectable:
        raise ValueError(
            f"component {component_id}: ненулевое требование, но нет допустимых кандидатов; "
            f"cross_span={cross_span:.3f}, requested_min_width={requested_min_width:.3f}, "
            f"candidate_min_width={candidate_min_width:.3f}, shape={tuple(map(int, work_matrix.shape))}"
        )

    started = perf_counter()
    if use_mosaic:
        work_rectangles, mosaic, mosaic_stats = reduce_mosaic(
            work_matrix, selectable, target=np.inf, rect_target=np.inf,
            force_reduce=False, show=False,
        )
    else:
        work_rectangles, mosaic, mosaic_stats = selectable, None, None
    mark("reduce_mosaic", started, candidates=len(work_rectangles))
    if np.any(work_matrix != 0) and not work_rectangles:
        raise ValueError(f"component {component_id}: ненулевое требование, но после mosaic нет кандидатов")

    source_classes = {_load_class(load2cls, row["load"]) for row in polygons}
    required_leaf_classes = {leaf for cls in source_classes if cls > 0 for leaf in recipe_leaves.get(cls, (cls,))}
    candidate_classes = {
        int(row.get("class", row.get("load"))) if isinstance(row, Mapping) else int(tuple(row)[-1])
        for row in work_rectangles
    }
    missing_candidate_classes = sorted(
        threshold for threshold in required_leaf_classes
        if not any(candidate >= threshold for candidate in candidate_classes)
    )
    if missing_candidate_classes:
        raise ValueError(
            f"component {component_id}: после подготовки сетки отсутствует покрытие "
            f"порогов {missing_candidate_classes}; candidate classes={sorted(candidate_classes)}, "
            f"grid preservation={preserve_stats}"
        )

    started = perf_counter()
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
        max_n=max_n,
        build_pulp_template=False,
    )
    mark("prepare_solver_model", started, variables=len(prepared.get("variable_upper_bounds", [])),
         rows=len(prepared.get("row_needs", [])))

    started = perf_counter()
    n_bounds = component_n_bounds(prepared)
    mark("component_n_bounds", started, lower=n_bounds.get("lower_bound"), upper=n_bounds.get("nonredundant_upper_bound"))
    poly_mos = [(row["geometry"], _load_class(load2cls, row["load"])) for row in polygons]
    return {
        "component": dict(component),
        "component_id": int(component.get("id", 0)),
        "axis": axis,
        "source_polygons": polygons,
        "simple_polygons": working,
        "grid": grid,
        "xs": np.asarray(xs),
        "ys": np.asarray(ys),
        "load_matrix": load_matrix,
        "int_matrix": int_matrix,
        "work_matrix": work_matrix,
        "work_x_edges": np.asarray(work_x_edges),
        "work_y_edges": np.asarray(work_y_edges),
        "work_x_steps": np.asarray(work_x_steps),
        "work_y_steps": np.asarray(work_y_steps),
        "work_rectangles": work_rectangles,
        "mosaic": mosaic,
        "mosaic_stats": mosaic_stats,
        "prepared": prepared,
        "poly_mos": poly_mos,
        "base_holds": base_holds,
        "holds": holds,
        "stats": {
            "polygons": len(polygons),
            "grid_cells": int(np.prod(work_matrix.shape)),
            "matrix_shape": tuple(map(int, work_matrix.shape)),
            "base_dense_shape": tuple(map(int, base_dense_shape)),
            "dense_shape": tuple(map(int, dense_shape)),
            "refinement_fallback": bool(refinement_fallback),
            "discarded_refined_shape": None if discarded_refined_shape is None else tuple(map(int, discarded_refined_shape)),
            "prepare_times_s": stage_times,
            "candidate_rectangles": len(work_rectangles),
            "requested_min_width": requested_min_width,
            "candidate_min_width": candidate_min_width,
            "cross_span": cross_span,
            "small_cross_component": small_cross_component,
            "grid_class_preservation": preserve_stats,
            "promoted_cells": int(preserve_stats.get("promoted_cells", 0)),
            "combined_cells": int(preserve_stats.get("combined_cells", 0)),
            "refined_cells": int(preserve_stats.get("refined_cells", 0)),
            "refined_subcells": int(preserve_stats.get("refined_subcells", 0)),
            "classes_after": list(preserve_stats.get("classes_after", [])),
            "required_leaf_classes": sorted(required_leaf_classes),
            "candidate_classes": sorted(candidate_classes),
            **n_bounds,
        },
    }


def _greedy_feasible_count(prepared: Mapping[str, Any]) -> int | None:
    needs = np.asarray(prepared.get("row_needs", []), dtype=np.int32).copy()
    if needs.size == 0:
        return 0
    indptr = np.asarray(prepared.get("candidate_row_indptr", []), dtype=np.int64)
    rows = np.asarray(prepared.get("candidate_rows", []), dtype=np.int32)
    upper = np.asarray(prepared.get("variable_upper_bounds", []), dtype=np.int32).copy()
    costs = np.asarray(prepared.get("candidate_costs", np.ones(len(upper))), dtype=float)
    count = 0
    while np.any(needs > 0):
        best = None
        for candidate in np.flatnonzero(upper > 0):
            covered = rows[indptr[candidate] : indptr[candidate + 1]]
            gain = int(np.count_nonzero(needs[covered] > 0))
            if gain == 0:
                continue
            key = (-gain, float(costs[candidate]) / gain, int(candidate))
            if best is None or key < best[0]:
                best = key, int(candidate), covered
        if best is None:
            return None
        _, candidate, covered = best
        needs[covered] = np.maximum(0, needs[covered] - 1)
        upper[candidate] -= 1
        count += 1
    return count


def component_n_bounds(
    prepared: Mapping[str, Any],
    *,
    cap: int | None = None,
    list_limit: int = 1000,
) -> dict[str, Any]:
    """Cheap safe range for useful exact counts of one prepared component."""

    needs = np.asarray(prepared.get("row_needs", []), dtype=np.int64)
    upper = np.asarray(prepared.get("variable_upper_bounds", []), dtype=np.int64)
    indptr = np.asarray(prepared.get("candidate_row_indptr", []), dtype=np.int64)
    if needs.size == 0:
        return {
            "lower_bound": 0,
            "nonredundant_upper_bound": 0,
            "candidate_capacity": int(upper.sum()),
            "total_row_need": 0,
            "greedy_feasible_n": 0,
            "suggested_n_values": [0],
        }
    if len(indptr) != len(upper) + 1:
        raise ValueError("Некорректный candidate_row_indptr")
    total_need = int(needs.sum())
    max_cover = int(np.diff(indptr).max(initial=0))
    capacity = int(upper.sum())
    if capacity <= 0 or max_cover <= 0:
        raise ValueError(
            "Ненулевое требование, но нет допустимых кандидатов в prepared-модели"
        )
    lower = max(int(needs.max()), int(ceil(total_need / max_cover)))
    upper_bound = min(total_need, capacity)
    prepared_max = prepared.get("max_n")
    if prepared_max not in (None, 0):
        upper_bound = min(upper_bound, int(prepared_max))
    if cap is not None:
        upper_bound = min(upper_bound, int(cap))
    if upper_bound < lower:
        raise ValueError(
            f"Prepared-модель не имеет допустимого диапазона N: lower={lower}, upper={upper_bound}, "
            f"capacity={capacity}, total_need={total_need}"
        )
    greedy = _greedy_feasible_count(prepared)
    span = max(0, upper_bound - lower + 1)
    if span <= max(0, int(list_limit)):
        values = list(range(lower, upper_bound + 1))
        truncated = False
    else:
        values = sorted({lower, upper_bound, *(() if greedy is None else (int(greedy),))})
        truncated = True
    return {
        "lower_bound": lower,
        "nonredundant_upper_bound": upper_bound,
        "candidate_capacity": capacity,
        "total_row_need": total_need,
        "max_rows_per_candidate": max_cover,
        "greedy_feasible_n": greedy,
        "suggested_n_values": values,
        "suggested_n_values_truncated": truncated,
    }


def solver_result_state(result: Mapping[str, Any] | None) -> str:
    """Return feasible / timeout / infeasible / failed for a solver payload."""

    row = dict((result or {}).get("solver_result", result or {}))
    if row.get("is_feasible"):
        return "feasible"
    text = " ".join(str(row.get(key, "")) for key in
                    ("status", "error", "message", "termination", "termination_condition")).lower()
    if "hardtimeout" in text or "hard timeout" in text or "time limit" in text or "timeout" in text:
        return "timeout"
    if "infeasible" in text:
        return "infeasible"
    return "failed"


def solve_component_frontier(
    component_problem: Mapping[str, Any],
    n_values: Sequence[int],
    *,
    data: Mapping[str, Any] | None = None,
    timeout: float | None = None,
    solver_time_limit: float | None = None,
    threads: int = 1,
    backend: str = "highs",
    require_optimal: bool = False,
    return_best_on_timeout: bool = True,
    raise_errors: bool = False,
    stop_after_first_feasible: bool = False,
) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    """Solve requested N values in the supplied order and classify timeouts."""

    from .rectangle_solver_job import solve_rectangle_job

    prepared = component_problem.get("prepared", component_problem)
    state = dict(data or {})
    results: dict[int, dict[str, Any]] = {}
    order = list(dict.fromkeys(int(value) for value in n_values))
    for n in order:
        try:
            result, state = solve_rectangle_job(
                prepared=prepared,
                data=state,
                N=n,
                timeout=None if timeout is None else float(timeout),
                solver_time_limit=None if solver_time_limit is None else float(solver_time_limit),
                threads=int(threads),
                backend=backend,
                require_optimal=bool(require_optimal),
                return_best_on_timeout=bool(return_best_on_timeout),
                raise_worker_errors=bool(raise_errors),
            )
            row = dict(result or {"n": n, "is_feasible": False, "error": "solver returned None"})
        except Exception as exc:
            if raise_errors:
                raise
            row = {"n": n, "is_feasible": False, "error": f"{type(exc).__name__}: {exc}"}
        row.setdefault("n", n)
        row["solve_state"] = solver_result_state(row)
        row["is_timeout"] = row["solve_state"] == "timeout"
        results[n] = row
        if stop_after_first_feasible and row["solve_state"] == "feasible":
            break
    return results, state


def _anchored_proxy_mass(boxes: Sequence[Mapping[str, Any]], densities: Mapping[Any, float]) -> float:
    value = 0.0
    for row in boxes:
        density = densities.get(row.get("class"), densities.get(str(row.get("class")), 1.0))
        geometry = row.get("geometry")
        area = float(geometry.area) if geometry is not None else float(box(*row["bounds"]).area)
        value += area * float(density)
    return float(value)


def fit_component_frontier(
    component_problem: Mapping[str, Any],
    solver_results: Mapping[int, Mapping[str, Any]],
    *,
    recipes: Mapping[Any, Sequence[Any]] | None,
    densities: Mapping[Any, float],
    diameters: Mapping[Any, float],
    steps: Mapping[Any, float],
    anchor_factor: float = 32.0,
    axis: str = "y",
    field: Any = None,
    min_width: float | Mapping[Any, float] | None = None,
    time_limit: float | None = None,
    allow_class_upgrade: bool = True,
    fit_milp_backend: str = "auto",
    fit_threads: int = 1,
) -> dict[int, dict[str, Any]]:
    """Restore, fit and anchor each feasible local solver result."""

    from .fit_box_layout import fit_box_layout

    axis = normalize_axis(axis)
    recipes = _normalize_recipes(recipes)
    densities = _normalize_class_floats(densities)
    diameters = _normalize_class_floats(diameters)
    steps = _normalize_class_floats(steps)
    output: dict[int, dict[str, Any]] = {}
    component_id = int(component_problem.get("component_id", component_problem.get("component", {}).get("id", 0)))
    for n, raw in sorted(solver_results.items()):
        result = raw.get("solver_result", raw)
        if not result or not result.get("is_feasible") or not result.get("rectangles"):
            state = solver_result_state(result)
            output[int(n)] = {"n": int(n), "is_feasible": False, "solver_result": dict(result or {}),
                              "solve_state": state, "is_timeout": state == "timeout",
                              "failure_stage": "solver"}
            continue
        rectangles = grid_rectangles_to_world(
            result["rectangles"],
            component_problem["work_x_edges"],
            component_problem["work_y_edges"],
            axis,
        )
        fitted = fit_box_layout(
            component_problem["poly_mos"],
            rectangles,
            recipes=recipes,
            densities=densities,
            min_w=min_width,
            time_limit=time_limit,
            allow_class_upgrade=allow_class_upgrade,
            allowed_classes=set(densities) & set(diameters) & set(steps),
            milp_backend=fit_milp_backend,
            threads=fit_threads,
        )
        if not fitted.get("is_feasible") or not fitted.get("rectangles"):
            output[int(n)] = {
                "n": int(n),
                "is_feasible": False,
                "solver_result": dict(result),
                "rectangles": rectangles,
                "fit_result": fitted,
                "solve_state": "feasible",
                "is_timeout": False,
                "failure_stage": "fit_box_layout",
            }
            continue
        anchored = add_box_anchorage(
            fitted,
            recipes=recipes,
            diameters=diameters,
            steps=steps,
            anchor_factor=anchor_factor,
            axis=axis,
            field=field,
        )
        for row in anchored:
            row["component_id"] = component_id
        output[int(n)] = {
            "n": int(n),
            "component_id": component_id,
            "is_feasible": True,
            "is_optimal": bool(result.get("is_optimal")) and bool(fitted.get("is_optimal")),
            "solve_state": "feasible",
            "is_timeout": False,
            "solver_result": dict(result),
            "rectangles": rectangles,
            "fit_result": fitted,
            "class_changes": list(fitted.get("class_changes", [])),
            "anchored_boxes": anchored,
            "proxy_mass": _anchored_proxy_mass(anchored, densities),
        }
    return output


def _nested_value(value: Mapping[str, Any], path: str):
    current: Any = value
    for key in path.split("."):
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def _frontier_cost(result: Mapping[str, Any], cost: str | Callable[[Mapping[str, Any]], float] | None) -> float:
    if callable(cost):
        return float(cost(result))
    if isinstance(cost, str):
        value = _nested_value(result, cost)
        if value is None:
            raise ValueError(f"В результате нет cost path {cost!r}")
        return float(value)
    for path in ("proxy_mass", "mass", "summary.mass with anchorage", "fit_result.mass", "total_cost", "solver_result.total_cost"):
        value = _nested_value(result, path)
        if value is not None:
            return float(value)
    raise ValueError("Не удалось определить стоимость результата компоненты")


def _trim_frontier(rows: list[dict[str, Any]], top_k: int | None) -> list[dict[str, Any]]:
    unique: dict[tuple[tuple[int, int], ...], dict[str, Any]] = {}
    for row in rows:
        key = tuple(sorted((int(k), int(v)) for k, v in row["component_ns"].items()))
        old = unique.get(key)
        if old is None or row["proxy_mass"] < old["proxy_mass"]:
            unique[key] = row
    ordered = sorted(unique.values(), key=lambda row: (row["proxy_mass"], tuple(sorted(row["component_ns"].items()))))
    return ordered if top_k is None else ordered[:top_k]


def combine_component_frontiers(
    frontiers: Mapping[Any, Mapping[int, Mapping[str, Any]]],
    *,
    top_k: int | None = 1,
    cost: str | Callable[[Mapping[str, Any]], float] | None = None,
) -> dict[int, list[dict[str, Any]]]:
    """Min-plus dynamic programming over independent component frontiers."""

    if top_k is not None and int(top_k) < 1:
        raise ValueError("top_k должен быть >= 1 или None")
    dp: dict[int, list[dict[str, Any]]] = {
        0: [{"proxy_mass": 0.0, "component_ns": {}, "component_choices": {}, "anchored_boxes": [], "rectangles": []}]
    }
    for component_id in sorted(frontiers, key=lambda value: str(value)):
        options = [
            (int(n), result, _frontier_cost(result, cost))
            for n, result in frontiers[component_id].items()
            if result and result.get("is_feasible", True)
        ]
        if not options:
            return {}
        next_dp: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for total, combinations in dp.items():
            for combination in combinations:
                for n, result, value in options:
                    next_dp[total + n].append(
                        {
                            "proxy_mass": float(combination["proxy_mass"] + value),
                            "component_ns": {**combination["component_ns"], component_id: n},
                            "component_choices": {**combination["component_choices"], component_id: result},
                            "anchored_boxes": [*combination["anchored_boxes"], *list(result.get("anchored_boxes", []))],
                            "rectangles": [*combination["rectangles"], *list(result.get("rectangles", []))],
                        }
                    )
        dp = {total: _trim_frontier(rows, None if top_k is None else int(top_k)) for total, rows in next_dp.items()}
    return dict(sorted(dp.items()))


def select_frontier_combination(
    combined: Mapping[int, Sequence[Mapping[str, Any]]],
    total_n: int,
    rank: int = 0,
) -> dict[str, Any]:
    rows = list(combined.get(int(total_n), []))
    if not rows:
        raise KeyError(f"Для общего N={total_n} нет комбинации")
    if not 0 <= int(rank) < len(rows):
        raise IndexError("rank выходит за границы списка комбинаций")
    return dict(rows[int(rank)])


def merge_interacting_box_groups(
    boxes: Sequence[Any],
    *,
    touch_tolerance: float = 0.0,
) -> list[dict[str, Any]]:
    """Group anchored boxes that now intersect or touch each other."""

    rows = list(boxes or [])
    if not rows:
        return []
    geometries = []
    for i, row in enumerate(rows):
        if isinstance(row, Mapping):
            geometry = row.get("geometry")
            if geometry is None:
                bounds = row.get("bounds")
                if bounds is None:
                    raise ValueError(f"boxes[{i}] не содержит geometry/bounds")
                geometry = box(*map(float, bounds[:4]))
        else:
            geometry = box(*map(float, tuple(row)[:4]))
        geometries.append(_clean_geometry(geometry))

    query_geometries = [g.buffer(float(touch_tolerance)) if touch_tolerance > 0 else g for g in geometries]
    tree = STRtree(query_geometries)
    dsu = _DSU(len(rows))
    for i, geometry in enumerate(query_geometries):
        for j in map(int, tree.query(geometry, predicate="intersects")):
            if j > i:
                dsu.union(i, j)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(rows)):
        groups[dsu.find(i)].append(i)

    out = []
    for ids in sorted(groups.values(), key=min):
        union = unary_union([geometries[i] for i in ids])
        component_ids = sorted(
            {
                int(rows[i].get("component_id"))
                for i in ids
                if isinstance(rows[i], Mapping) and rows[i].get("component_id") is not None
            }
        )
        out.append(
            {
                "id": len(out),
                "box_indices": ids,
                "component_ids": component_ids,
                "boxes": [rows[i] for i in ids],
                "geometry": union,
                "bounds": tuple(map(float, union.bounds)),
            }
        )
    return out


def bar_mass_kg(bars: Sequence[Any], steel_density_kg_m3: float = 7850.0) -> float:
    """Mass of physical bar segments; all coordinates and diameters are mm."""

    from math import hypot, pi

    density = float(steel_density_kg_m3)
    if density <= 0:
        raise ValueError("steel_density_kg_m3 должен быть положительным")
    total = 0.0
    for i, raw in enumerate(bars or []):
        if isinstance(raw, Mapping):
            try:
                x0, y0, x1, y1 = (float(raw[k]) for k in ("x0", "y0", "x1", "y1"))
                diameter = float(raw["diameter"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"bars[{i}] должен содержать x0,y0,x1,y1,diameter") from exc
        else:
            q = tuple(raw)
            if len(q) < 5:
                raise ValueError(f"bars[{i}] должен иметь формат x0,y0,x1,y1,diameter")
            x0, y0, x1, y1, diameter = map(float, q[:5])
        if diameter <= 0:
            raise ValueError(f"bars[{i}]: diameter должен быть положительным")
        length_m = hypot(x1 - x0, y1 - y0) / 1000.0
        area_m2 = pi * (diameter / 1000.0) ** 2 / 4.0
        total += density * area_m2 * length_m
    return float(total)


def evaluate_combined_frontier(
    combined: Mapping[int, Sequence[Mapping[str, Any]]],
    *,
    polygons: Sequence[Any],
    background: tuple[float, float],
    axis: str,
    min_step: float = 100.0,
    steel_density_kg_m3: float = 7850.0,
    max_combinations_per_n: int | None = None,
) -> dict[int, list[dict[str, Any]]]:
    """Evaluate retained combinations by the actual global physical bar mass."""

    from .rebar_field_layout import layout_rebars

    out: dict[int, list[dict[str, Any]]] = {}
    for total_n, candidates in sorted(combined.items()):
        rows = []
        selected = list(candidates)
        if max_combinations_per_n is not None:
            selected = selected[: max(0, int(max_combinations_per_n))]
        for rank, candidate in enumerate(selected):
            layout = layout_rebars(
                polygons=polygons,
                boxes=candidate.get("anchored_boxes", []),
                background=background,
                axis=axis,
                min_step=min_step,
            )
            feasible = bool(layout.get("is_feasible"))
            mass = bar_mass_kg(layout.get("bars", []), steel_density_kg_m3) if feasible else float("inf")
            rows.append({
                **dict(candidate),
                "proxy_rank": rank,
                "actual_mass_kg": float(mass),
                "is_feasible": feasible,
                "bar_layout": layout,
            })
        rows.sort(key=lambda row: (not row["is_feasible"], row["actual_mass_kg"], row["proxy_mass"]))
        out[int(total_n)] = rows
    return out


# ---------------------------------------------------------------------------
# Global frontier selection / dynamic component merging
# ---------------------------------------------------------------------------

def _box_geometry(row: Any):
    """Return polygonal geometry for an anchored-box record without mutating it."""
    if isinstance(row, Mapping):
        geometry = row.get("geometry")
        if geometry is not None:
            return _clean_geometry(geometry)
        bounds = row.get("bounds")
        if bounds is None:
            raise ValueError("box record не содержит geometry/bounds")
        return box(*map(float, tuple(bounds)[:4]))
    values = tuple(row)
    if len(values) < 4:
        raise ValueError("box должен содержать минимум x0,y0,x1,y1")
    return box(*map(float, values[:4]))


def frontier_interaction_groups(
    frontiers: Mapping[Any, Mapping[int, Mapping[str, Any]]],
    *,
    touch_tolerance: float = 0.0,
) -> list[list[Any]]:
    """Conservative groups of components whose *possible* anchored boxes interact.

    All feasible frontier options of one component are unioned into one envelope.
    If envelopes of two components intersect/touch, those components belong to the
    same group.  This is intentionally conservative and is meant for deciding
    which components should be re-solved jointly.
    """

    component_ids = sorted(frontiers, key=lambda value: str(value))
    if not component_ids:
        return []

    envelopes = []
    for component_id in component_ids:
        geometries = []
        for result in frontiers.get(component_id, {}).values():
            if not result or not result.get("is_feasible", True):
                continue
            geometries.extend(_box_geometry(row) for row in result.get("anchored_boxes", []) or [])
        envelope = unary_union(geometries) if geometries else GeometryCollection()
        if touch_tolerance > 0 and not envelope.is_empty:
            envelope = envelope.buffer(float(touch_tolerance))
        envelopes.append(_clean_geometry(envelope))

    dsu = _DSU(len(component_ids))
    nonempty = [i for i, geometry in enumerate(envelopes) if not geometry.is_empty]
    if nonempty:
        tree = STRtree([envelopes[i] for i in nonempty])
        for local_i, i in enumerate(nonempty):
            for local_j in map(int, tree.query(envelopes[i], predicate="intersects")):
                j = nonempty[local_j]
                if j > i:
                    dsu.union(i, j)

    groups: dict[int, list[Any]] = defaultdict(list)
    for i, component_id in enumerate(component_ids):
        groups[dsu.find(i)].append(component_id)
    return [
        sorted(ids, key=lambda value: str(value))
        for ids in sorted(groups.values(), key=lambda ids: min(str(x) for x in ids))
    ]


def merge_reinforcement_components(
    components: Sequence[Mapping[str, Any]],
    component_groups: Sequence[Sequence[Any]],
) -> list[dict[str, Any]]:
    """Merge selected component ids into new component records for re-solving.

    ``component_groups`` may omit singleton components; they are preserved
    automatically.  New component ids are compact 0..N-1, while original ids are
    retained in ``source_component_ids``.
    """

    rows = [dict(component) for component in components]
    by_id = {component.get("id", i): component for i, component in enumerate(rows)}
    if len(by_id) != len(rows):
        raise ValueError("component ids должны быть уникальными")

    dsu = _DSU(len(rows))
    pos = {component.get("id", i): i for i, component in enumerate(rows)}
    for group in component_groups or []:
        ids = [value for value in group if value in pos]
        if not ids:
            continue
        first = pos[ids[0]]
        for value in ids[1:]:
            dsu.union(first, pos[value])

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for i, component in enumerate(rows):
        grouped[dsu.find(i)].append(component)

    def original_ids(component: Mapping[str, Any]) -> list[Any]:
        values = component.get("source_component_ids")
        return list(values) if values is not None else [component.get("id")]

    ordered = sorted(
        grouped.values(),
        key=lambda members: min(str(value) for member in members for value in original_ids(member)),
    )
    output = []
    for new_id, members in enumerate(ordered):
        axis_values = {str(member.get("axis", "y")) for member in members}
        if len(axis_values) > 1:
            raise ValueError(f"Нельзя объединить components с разными axis: {axis_values}")
        polygons = [dict(row) for member in members for row in member.get("polygons", [])]
        expanded_polygons = [dict(row) for member in members for row in member.get("expanded_polygons", [])]
        source_ids = sorted(
            {value for member in members for value in original_ids(member)},
            key=lambda value: str(value),
        )
        polygon_indices = sorted({int(x) for member in members for x in member.get("polygon_indices", [])})
        geometries = [member.get("geometry") for member in members if member.get("geometry") is not None]
        demand_geometries = [member.get("demand_geometry") for member in members if member.get("demand_geometry") is not None]
        geometry = _clean_geometry(unary_union(geometries)) if geometries else GeometryCollection()
        demand = _clean_geometry(unary_union(demand_geometries)) if demand_geometries else unary_union(
            [row["geometry"] for row in polygons if row.get("geometry") is not None]
        )
        output.append(
            {
                "id": new_id,
                "source_component_ids": source_ids,
                "axis": next(iter(axis_values), "y"),
                "polygon_indices": polygon_indices,
                "polygons": polygons,
                "geometry": geometry,
                "demand_geometry": demand,
                "bounds": tuple(map(float, geometry.bounds)) if not geometry.is_empty else (),
                "demand_bounds": tuple(map(float, demand.bounds)) if not demand.is_empty else (),
                "classes": sorted({int(x) for member in members for x in member.get("classes", [])}),
                "loads": sorted({float(x) for member in members for x in member.get("loads", [])}),
                "max_hold": max((float(member.get("max_hold", 0.0)) for member in members), default=0.0),
                "expanded_polygons": expanded_polygons,
            }
        )
    return output


def merge_frontier_interacting_components(
    components: Sequence[Mapping[str, Any]],
    frontiers: Mapping[Any, Mapping[int, Mapping[str, Any]]],
    *,
    touch_tolerance: float = 0.0,
) -> list[dict[str, Any]]:
    """Convenience wrapper: detect possible anchor interactions and merge them."""
    return merge_reinforcement_components(
        components,
        frontier_interaction_groups(frontiers, touch_tolerance=touch_tolerance),
    )



def frontier_combination_counts(
    frontiers: Mapping[Any, Mapping[int, Mapping[str, Any]]],
) -> dict[int, int]:
    """Exact number of local-option combinations for every total N, without enumeration."""

    counts: dict[int, int] = {0: 1}
    for component_id in sorted(frontiers, key=lambda value: str(value)):
        ns = [
            int(n)
            for n, result in frontiers[component_id].items()
            if result and result.get("is_feasible", True)
        ]
        if not ns:
            return {}
        nxt: dict[int, int] = defaultdict(int)
        for total, number in counts.items():
            for n in ns:
                nxt[total + n] += int(number)
        counts = dict(nxt)
    return dict(sorted(counts.items()))




def frontier_proxy_minima(
    frontiers: Mapping[Any, Mapping[int, Mapping[str, Any]]],
    *,
    cost: str | Callable[[Mapping[str, Any]], float] | None = None,
) -> dict[int, float]:
    """Minimum additive proxy cost for every total N, without combinations."""

    dp = {0: 0.0}
    for component_id in sorted(frontiers, key=lambda value: str(value)):
        best_by_n = {}
        for n, result in frontiers[component_id].items():
            if result and result.get("is_feasible", True):
                n, value = int(n), float(_frontier_cost(result, cost))
                best_by_n[n] = min(value, best_by_n.get(n, float("inf")))
        if not best_by_n:
            return {}
        nxt = {}
        for total, old in dp.items():
            for n, value in best_by_n.items():
                key = total + n
                nxt[key] = min(old + value, nxt.get(key, float("inf")))
        dp = nxt
    return dict(sorted(dp.items()))


def select_total_n_candidates(
    frontiers: Mapping[Any, Mapping[int, Mapping[str, Any]]],
    *,
    requested: Sequence[int] | None = None,
    top_k: int | None = None,
    cost: str | Callable[[Mapping[str, Any]], float] | None = None,
) -> list[int]:
    """Select promising total N values by their exact additive proxy lower bound."""

    minima = frontier_proxy_minima(frontiers, cost=cost)
    if requested is not None:
        allowed = set(map(int, requested))
        minima = {n: value for n, value in minima.items() if n in allowed}
    ordered = sorted(minima, key=lambda n: (minima[n], n))
    return ordered if top_k is None else ordered[:max(0, int(top_k))]


def component_frontier_diagnostics(
    frontiers: Mapping[Any, Mapping[int, Mapping[str, Any]]],
    *,
    expected_component_ids: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Explain empty proxy results and distinguish timeout from infeasible."""

    present = set(frontiers)
    expected = present if expected_component_ids is None else set(expected_component_ids)
    order = lambda values: sorted(values, key=lambda value: str(value))
    feasible_by_component, states_by_component = {}, {}
    empty, timeouts, fit_failed, true_infeasible, zero_boxes = [], [], [], [], []
    for component_id in order(present):
        options, states = [], {}
        for n, result in frontiers.get(component_id, {}).items():
            n = int(n)
            if result and result.get("is_feasible", True):
                options.append(n)
                states[n] = "feasible"
                if not list(result.get("anchored_boxes", []) or []):
                    zero_boxes.append((component_id, n))
            else:
                states[n] = (result or {}).get("solve_state") or solver_result_state((result or {}).get("solver_result", result))
                if (result or {}).get("failure_stage") == "fit_box_layout":
                    states[n] = "fit_failed"
        feasible_by_component[component_id] = sorted(options)
        states_by_component[component_id] = states
        if options:
            continue
        empty.append(component_id)
        values = set(states.values())
        if values and values <= {"timeout"}:
            timeouts.append(component_id)
        elif "fit_failed" in values:
            fit_failed.append(component_id)
        elif "infeasible" in values:
            true_infeasible.append(component_id)
    missing = order(expected - present)
    counts = frontier_combination_counts(frontiers) if not missing and not empty else {}
    return {
        "status": "ok" if not missing and not empty and not zero_boxes else ("empty" if not expected and not present else "incomplete"),
        "expected_components": len(expected), "present_components": len(present),
        "missing_components": missing, "empty_components": order(empty),
        "timeout_components": order(timeouts), "fit_failed_components": order(fit_failed),
        "infeasible_components": order(true_infeasible),
        "zero_box_options": sorted(zero_boxes, key=lambda item: (str(item[0]), item[1])),
        "feasible_by_component": feasible_by_component, "states_by_component": states_by_component,
        "total_n_values": len(counts), "combination_count": int(sum(counts.values())) if counts else 0,
        "combinations_by_n": counts,
    }


def _frontier_milp_data(
    frontiers: Mapping[Any, Mapping[int, Mapping[str, Any]]],
    cost: str | Callable[[Mapping[str, Any]], float] | None,
):
    component_ids = sorted(frontiers, key=lambda value: str(value))
    flat: list[dict[str, Any]] = []
    owner: dict[Any, list[int]] = {}
    for component_id in component_ids:
        owner[component_id] = []
        for n, result in sorted(frontiers[component_id].items(), key=lambda item: int(item[0])):
            if not result or not result.get("is_feasible", True):
                continue
            index = len(flat)
            flat.append(
                {
                    "index": index,
                    "component_id": component_id,
                    "n": int(n),
                    "result": result,
                    "cost": float(_frontier_cost(result, cost)),
                }
            )
            owner[component_id].append(index)
        if not owner[component_id]:
            return component_ids, flat, owner, False
    return component_ids, flat, owner, True


def _possible_total_ns(flat: Sequence[Mapping[str, Any]], owner: Mapping[Any, Sequence[int]]) -> list[int]:
    totals = {0}
    for ids in owner.values():
        ns = {int(flat[i]["n"]) for i in ids}
        totals = {a + b for a in totals for b in ns}
    return sorted(totals)


def _milp_rows(
    flat: Sequence[Mapping[str, Any]],
    owner: Mapping[Any, Sequence[int]],
    total_n: int,
    nogoods: Sequence[Sequence[int]],
):
    rows: list[tuple[list[int], float, float]] = [
        (list(ids), 1.0, 1.0) for ids in owner.values()
    ]
    rows.append((list(range(len(flat))), float(total_n), float(total_n)))
    for ids in nogoods:
        ids = list(map(int, ids))
        if ids:
            rows.append((ids, -float("inf"), float(len(ids) - 1)))
    return rows


def _solve_frontier_choice_scipy(flat, owner, total_n, nogoods, time_limit=None, output=False):
    try:
        from scipy.optimize import Bounds, LinearConstraint, milp
        from scipy.sparse import csr_matrix
    except ImportError as exc:
        raise ImportError("Нужен highspy или scipy.optimize.milp") from exc

    nvar = len(flat)
    rows = _milp_rows(flat, owner, total_n, nogoods)
    data, row_idx, col_idx = [], [], []
    lower, upper = [], []
    for r, (ids, lo, hi) in enumerate(rows):
        for i in ids:
            row_idx.append(r); col_idx.append(i); data.append(1.0 if r != len(owner) else float(flat[i]["n"]))
        lower.append(lo); upper.append(hi)
    # The total-N row is exactly the row after all owner equalities.
    total_row = len(owner)
    for k, r in enumerate(row_idx):
        if r == total_row:
            data[k] = float(flat[col_idx[k]]["n"])
    A = csr_matrix((data, (row_idx, col_idx)), shape=(len(rows), nvar))
    options = {"disp": bool(output)}
    if time_limit is not None:
        options["time_limit"] = float(time_limit)
    z = milp(
        c=np.asarray([row["cost"] for row in flat], dtype=float),
        integrality=np.ones(nvar, dtype=np.int8),
        bounds=Bounds(np.zeros(nvar), np.ones(nvar)),
        constraints=LinearConstraint(A, np.asarray(lower), np.asarray(upper)),
        options=options,
    )
    if z.x is None:
        return None, str(z.message)
    values = np.asarray(z.x, dtype=float)
    chosen = [max(ids, key=lambda i: values[i]) for ids in owner.values()]
    if not all(values[i] > 0.5 for i in chosen):
        return None, str(z.message)
    if sum(int(flat[i]["n"]) for i in chosen) != int(total_n):
        return None, str(z.message)
    return chosen, str(z.message)


def _solve_frontier_choice_highs(flat, owner, total_n, nogoods, threads=1, time_limit=None, output=False):
    import highspy

    nvar = len(flat)
    rows = _milp_rows(flat, owner, total_n, nogoods)
    h = highspy.Highs()
    h.setOptionValue("output_flag", bool(output))
    if threads is not None:
        h.setOptionValue("threads", max(1, int(threads)))
    if time_limit is not None:
        h.setOptionValue("time_limit", float(time_limit))
    h.addVars(nvar, np.zeros(nvar), np.ones(nvar))
    indices = np.arange(nvar, dtype=np.int32)
    h.changeColsCost(nvar, indices, np.asarray([row["cost"] for row in flat], dtype=float))
    h.changeColsIntegrality(
        nvar,
        indices,
        np.asarray([highspy.HighsVarType.kInteger] * nvar),
    )

    lengths = np.asarray([len(row[0]) for row in rows], dtype=np.int32)
    starts = np.r_[0, np.cumsum(lengths[:-1])].astype(np.int32) if len(rows) else np.asarray([], dtype=np.int32)
    col_indices = np.asarray([i for ids, _, _ in rows for i in ids], dtype=np.int32)
    values = []
    total_row = len(owner)
    for r, (ids, _, _) in enumerate(rows):
        values.extend(float(flat[i]["n"]) if r == total_row else 1.0 for i in ids)
    h.addRows(
        len(rows),
        np.asarray([row[1] if np.isfinite(row[1]) else -highspy.kHighsInf for row in rows], dtype=float),
        np.asarray([row[2] if np.isfinite(row[2]) else highspy.kHighsInf for row in rows], dtype=float),
        len(col_indices),
        starts,
        col_indices,
        np.asarray(values, dtype=float),
    )
    h.run()
    status = h.getModelStatus()
    solution = np.asarray(list(h.getSolution().col_value), dtype=float)
    if len(solution) != nvar:
        return None, h.modelStatusToString(status)
    chosen = [max(ids, key=lambda i: solution[i]) for ids in owner.values()]
    if not all(solution[i] > 0.5 for i in chosen):
        return None, h.modelStatusToString(status)
    if sum(int(flat[i]["n"]) for i in chosen) != int(total_n):
        return None, h.modelStatusToString(status)
    return chosen, h.modelStatusToString(status)


def _solve_frontier_choice_milp(
    flat,
    owner,
    total_n,
    nogoods,
    *,
    backend="auto",
    threads=1,
    time_limit=None,
    output=False,
):
    backend = str(backend).lower()
    if backend not in {"auto", "highs", "scipy"}:
        raise ValueError("milp_backend должен быть 'auto', 'highs' или 'scipy'")
    if backend in {"auto", "highs"}:
        try:
            import highspy  # noqa: F401
            return _solve_frontier_choice_highs(
                flat, owner, total_n, nogoods,
                threads=threads, time_limit=time_limit, output=output,
            )
        except Exception:
            if backend == "highs":
                raise
    return _solve_frontier_choice_scipy(
        flat, owner, total_n, nogoods,
        time_limit=time_limit, output=output,
    )


def _candidate_from_choice(flat, chosen):
    component_ns = {}
    component_choices = {}
    anchored_boxes = []
    rectangles = []
    proxy_mass = 0.0
    for index in chosen:
        row = flat[index]
        component_id, n, result = row["component_id"], int(row["n"]), row["result"]
        component_ns[component_id] = n
        component_choices[component_id] = result
        proxy_mass += float(row["cost"])
        for anchored in result.get("anchored_boxes", []) or []:
            if isinstance(anchored, Mapping):
                anchored_boxes.append({**dict(anchored), "component_id": anchored.get("component_id", component_id)})
            else:
                anchored_boxes.append(anchored)
        rectangles.extend(list(result.get("rectangles", []) or []))
    return {
        "proxy_mass": float(proxy_mass),
        "component_ns": component_ns,
        "component_choices": component_choices,
        "anchored_boxes": anchored_boxes,
        "rectangles": rectangles,
    }


def evaluate_component_frontiers_global(
    frontiers: Mapping[Any, Mapping[int, Mapping[str, Any]]],
    *,
    polygons: Sequence[Any],
    background: tuple[float, float],
    axis: str,
    total_ns: Sequence[int] | None = None,
    total_n_top_k: int | None = None,
    min_step: float = 100.0,
    steel_density_kg_m3: float = 7850.0,
    cost: str | Callable[[Mapping[str, Any]], float] | None = None,
    feasible_per_n: int = 1,
    max_evaluations_per_n: int | None = None,
    milp_backend: str = "auto",
    threads: int = 1,
    solver_time_limit: float | None = None,
    output: bool = False,
    interaction_tolerance: float = 0.0,
    layout_fn: Callable[..., Mapping[str, Any]] | None = None,
    seed_combined: Mapping[int, Sequence[Mapping[str, Any]]] | None = None,
    diagnostics: dict[int, dict[str, Any]] | None = None,
) -> dict[int, list[dict[str, Any]]]:
    """Lazy global search, seeded by the already computed proxy top-K candidates.

    Proxy seeds are checked first.  If one is feasible, no global-choice MILP is
    required.  Otherwise their exact choices become initial no-good cuts and the
    MILP continues from the next-cheapest combination.  ``diagnostics`` receives
    exact candidate counts and separate MILP/layout timings for each total N.
    """

    from time import perf_counter

    if int(feasible_per_n) < 1:
        raise ValueError("feasible_per_n должен быть >= 1")
    axis = normalize_axis(axis)
    if layout_fn is None:
        from .rebar_field_layout import layout_rebars
        layout_fn = layout_rebars

    component_ids, flat, owner, ok = _frontier_milp_data(frontiers, cost)
    if not ok:
        return {}
    counts = frontier_combination_counts(frontiers)
    targets = select_total_n_candidates(
        frontiers, requested=total_ns, top_k=total_n_top_k, cost=cost
    )
    lookup = {(row["component_id"], int(row["n"])): int(row["index"]) for row in flat}
    seed_combined = seed_combined or {}
    out: dict[int, list[dict[str, Any]]] = {}

    def choice_key(component_ns):
        return tuple(sorted(((cid, int(n)) for cid, n in component_ns.items()), key=lambda item: str(item[0])))

    def seed_indices(candidate):
        ns = candidate.get("component_ns", {})
        if set(ns) != set(owner):
            return None
        try:
            return tuple(lookup[(cid, int(ns[cid]))] for cid in component_ids)
        except (KeyError, TypeError, ValueError):
            return None

    for total_n in targets:
        rows: list[dict[str, Any]] = []
        nogoods: list[tuple[int, ...]] = []
        seen = set()
        feasible_count = seed_evaluations = milp_evaluations = milp_solves = 0
        layout_time = milp_time = 0.0
        rank = 0
        status = "exhausted"
        last_milp_status = None
        limit = None if max_evaluations_per_n is None else max(0, int(max_evaluations_per_n))

        def evaluate(candidate, chosen, source, solve_seconds=0.0, solver_status=None):
            nonlocal rank, feasible_count, seed_evaluations, milp_evaluations, layout_time, milp_time
            key = choice_key(candidate.get("component_ns", {}))
            if key in seen:
                return None
            seen.add(key)
            t = perf_counter()
            try:
                layout = dict(layout_fn(
                    polygons=polygons,
                    boxes=candidate.get("anchored_boxes", []),
                    background=background,
                    axis=axis,
                    min_step=min_step,
                ) or {})
                feasible, error = bool(layout.get("is_feasible")), None
            except Exception as exc:
                layout = {"is_feasible": False, "error": f"{type(exc).__name__}: {exc}"}
                feasible, error = False, layout["error"]
            dt = perf_counter() - t
            layout_time += dt
            milp_time += float(solve_seconds)
            mass = bar_mass_kg(layout.get("bars", []), steel_density_kg_m3) if feasible else float("inf")
            interaction_groups = [
                group["component_ids"]
                for group in merge_interacting_box_groups(
                    candidate.get("anchored_boxes", []),
                    touch_tolerance=float(interaction_tolerance),
                )
                if len(group.get("component_ids", [])) > 1
            ]
            row = {
                **dict(candidate),
                "actual_mass_kg": float(mass),
                "is_feasible": feasible,
                "bar_layout": layout,
                "global_search_rank": rank,
                "candidate_source": source,
                "milp_status": solver_status,
                "milp_time_s": float(solve_seconds),
                "layout_time_s": float(dt),
                "interaction_groups": interaction_groups,
                **({"error": error} if error else {}),
            }
            rows.append(row)
            rank += 1
            if source == "proxy_seed":
                seed_evaluations += 1
            else:
                milp_evaluations += 1
            if feasible:
                feasible_count += 1
            if chosen is not None:
                nogoods.append(tuple(sorted(map(int, chosen))))
            return row

        seeds = sorted(
            list(seed_combined.get(int(total_n), []) or []),
            key=lambda row: (float(row.get("proxy_mass", float("inf"))), choice_key(row.get("component_ns", {}))),
        )
        for candidate in seeds:
            if limit is not None and rank >= limit:
                status = "limit_reached"
                break
            chosen = seed_indices(candidate)
            if chosen is None or sum(int(flat[i]["n"]) for i in chosen) != int(total_n):
                continue
            row = evaluate(candidate, chosen, "proxy_seed")
            if row is not None and feasible_count >= int(feasible_per_n):
                status = "feasible"
                break

        while feasible_count < int(feasible_per_n) and status != "limit_reached":
            if limit is not None and rank >= limit:
                status = "limit_reached"
                break
            t = perf_counter()
            chosen, last_milp_status = _solve_frontier_choice_milp(
                flat, owner, int(total_n), nogoods,
                backend=milp_backend, threads=threads,
                time_limit=solver_time_limit, output=output,
            )
            solve_seconds = perf_counter() - t
            milp_solves += 1
            if chosen is None:
                milp_time += solve_seconds
                status = "exhausted"
                break
            candidate = _candidate_from_choice(flat, chosen)
            row = evaluate(candidate, chosen, "milp", solve_seconds, last_milp_status)
            if row is None:
                nogoods.append(tuple(sorted(map(int, chosen))))
                continue
            if feasible_count >= int(feasible_per_n):
                status = "feasible"
                break

        rows.sort(key=lambda row: (
            not row["is_feasible"], row["actual_mass_kg"], row["proxy_mass"], row["global_search_rank"]
        ))
        out[int(total_n)] = rows
        if diagnostics is not None:
            diagnostics[int(total_n)] = {
                "status": status,
                "candidate_space": int(counts.get(int(total_n), 0)),
                "evaluations": int(rank),
                "seed_evaluations": int(seed_evaluations),
                "milp_evaluations": int(milp_evaluations),
                "milp_solves": int(milp_solves),
                "feasible_found": int(feasible_count),
                "milp_time_s": float(milp_time),
                "layout_time_s": float(layout_time),
                "last_milp_status": last_milp_status,
            }
    return out

def failed_interaction_groups(
    evaluated: Mapping[int, Sequence[Mapping[str, Any]]],
) -> list[list[Any]]:
    """Union component interaction groups observed in failed global layouts."""

    all_ids = set()
    edges = []
    for rows in evaluated.values():
        for row in rows:
            if row.get("is_feasible"):
                continue
            for group in row.get("interaction_groups", []) or []:
                ids = list(group)
                all_ids.update(ids)
                if len(ids) > 1:
                    edges.append(ids)
    if not all_ids:
        return []
    ids = sorted(all_ids, key=lambda value: str(value))
    pos = {value: i for i, value in enumerate(ids)}
    dsu = _DSU(len(ids))
    for group in edges:
        first = pos[group[0]]
        for value in group[1:]:
            dsu.union(first, pos[value])
    groups: dict[int, list[Any]] = defaultdict(list)
    for value in ids:
        groups[dsu.find(pos[value])].append(value)
    return [
        sorted(group, key=lambda value: str(value))
        for group in groups.values()
        if len(group) > 1
    ]
