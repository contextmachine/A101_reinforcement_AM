from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Mapping, Sequence

import numpy as np


def normalize_axis(axis: str) -> str:
    axis = str(axis).lower()
    if axis not in {"x", "y"}:
        raise ValueError("axis должен быть 'x' или 'y'")
    return axis


def orient_grid(matrix, xs, ys, axis):
    """Normalize a ``matrix[y, x]`` grid so the working bar axis is ``y``."""

    axis = normalize_axis(axis)
    matrix = np.asarray(matrix)
    xs = np.asarray(xs, dtype=float)
    ys = np.asarray(ys, dtype=float)

    if matrix.ndim != 2:
        raise ValueError("matrix должна быть двумерной")
    if len(xs) != matrix.shape[1] + 1 or len(ys) != matrix.shape[0] + 1:
        raise ValueError("Размеры xs/ys не соответствуют matrix[y, x]")
    if np.any(np.diff(xs) <= 0) or np.any(np.diff(ys) <= 0):
        raise ValueError("Координаты xs/ys должны строго возрастать")

    if axis == "y":
        return matrix, xs, ys, np.diff(xs), np.diff(ys)
    return matrix.T, ys, xs, np.diff(ys), np.diff(xs)


def _swap_bounds(bounds: Sequence[float]) -> tuple:
    if len(bounds) < 4:
        raise ValueError("Границы должны содержать минимум четыре значения")
    x0, y0, x1, y1 = map(float, bounds[:4])
    return (y0, x0, y1, x1, *tuple(bounds[4:]))


def orient_geometry(geometry: Any, axis: str):
    """Return geometry in the native vertical-bar work coordinate system.

    For ``axis='x'`` this swaps X and Y. The operation is its own inverse.
    """

    axis = normalize_axis(axis)
    if axis == "y" or geometry is None:
        return geometry
    from shapely.ops import transform

    def swap(x, y, z=None):
        return (y, x) if z is None else (y, x, z)

    return transform(swap, geometry)


def _geometry_from_item(item: Any):
    if hasattr(item, "geom_type"):
        return item
    if isinstance(item, Mapping):
        if item.get("geometry") is not None:
            return _geometry_from_item(item["geometry"])
        if item.get("points") is not None:
            from shapely.geometry import Polygon

            return Polygon(item["points"])
    if isinstance(item, (tuple, list)) and len(item) == 2:
        try:
            return _geometry_from_item(item[0])
        except Exception:
            pass
    raise ValueError("Не удалось получить Shapely-геометрию")


def orient_polygon_items(items: Sequence[Any], axis: str) -> list[Any]:
    """Swap axes of polygon records without losing their metadata."""

    axis = normalize_axis(axis)
    if axis == "y":
        return list(items)
    out = []
    for item in items:
        if hasattr(item, "geom_type"):
            out.append(orient_geometry(item, axis))
        elif isinstance(item, Mapping):
            q = dict(item)
            if q.get("geometry") is not None:
                q["geometry"] = orient_geometry(q["geometry"], axis)
            if q.get("points") is not None:
                q["points"] = [[float(y), float(x)] for x, y, *_ in q["points"]]
            if q.get("bounds") is not None:
                q["bounds"] = _swap_bounds(q["bounds"])
            out.append(q)
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            out.append((orient_geometry(_geometry_from_item(item[0]), axis), item[1]))
        else:
            out.append([[float(y), float(x)] for x, y, *_ in item])
    return out


def _swap_bar_dict(bar: Mapping[str, Any]) -> dict[str, Any]:
    q = dict(bar)
    if all(k in q for k in ("x0", "y0", "x1", "y1")):
        q["x0"], q["y0"], q["x1"], q["y1"] = (
            float(q["y0"]),
            float(q["x0"]),
            float(q["y1"]),
            float(q["x1"]),
        )
    if "guide_x" in q and "guide_y" in q:
        q["guide_x"], q["guide_y"] = q["guide_y"], q["guide_x"]
    elif "guide_x" in q:
        q["guide_y"] = q.pop("guide_x")
    elif "guide_y" in q:
        q["guide_x"] = q.pop("guide_y")
    if "allowed_x" in q and "allowed_y" in q:
        q["allowed_x"], q["allowed_y"] = q["allowed_y"], q["allowed_x"]
    elif "allowed_x" in q:
        q["allowed_y"] = q.pop("allowed_x")
    elif "allowed_y" in q:
        q["allowed_x"] = q.pop("allowed_y")
    if "x" in q and "y" in q:
        q["x"], q["y"] = q["y"], q["x"]
    elif "x" in q:
        q["y"] = q.pop("x")
    elif "y" in q:
        q["x"] = q.pop("y")
    return q


def orient_bars(bars: Sequence[Any], axis: str) -> list[Any]:
    axis = normalize_axis(axis)
    if axis == "y":
        return [dict(b) if isinstance(b, Mapping) else tuple(b) for b in bars]
    out = []
    for bar in bars:
        if isinstance(bar, Mapping):
            out.append(_swap_bar_dict(bar))
        else:
            q = tuple(bar)
            if len(q) < 4:
                raise ValueError("Стержень должен содержать x0,y0,x1,y1")
            out.append(_swap_bounds(q))
    return out


def _orient_rectangle_item(item: Any, axis: str):
    if axis == "y":
        return dict(item) if isinstance(item, Mapping) else tuple(item)
    if isinstance(item, Mapping):
        q = dict(item)
        for key in (
            "bounds",
            "original_bounds",
            "fitted_bounds",
            "anchored_bounds",
            "primary_bounds",
            "structural_bounds",
            "final rectangle",
            "final_rectangle",
        ):
            if q.get(key) is not None:
                q[key] = _swap_bounds(q[key])
        if q.get("geometry") is not None:
            q["geometry"] = orient_geometry(q["geometry"], axis)
        if q.get("bars") is not None:
            q["bars"] = orient_bars(q["bars"], axis)
        return q
    q = tuple(item)
    if len(q) == 2 and hasattr(q[0], "geom_type"):
        return orient_geometry(q[0], axis), q[1]
    return _swap_bounds(q)


def orient_rectangles(rectangles: Any, axis: str):
    """Swap rectangle/zone axes, including common fit-result wrappers."""

    axis = normalize_axis(axis)
    if isinstance(rectangles, Mapping):
        q = dict(rectangles)
        for key in ("rectangles", "zones", "candidate_rectangles", "structural_rectangles"):
            if key in q and q[key] is not None:
                q[key] = [_orient_rectangle_item(x, axis) for x in q[key]]
        if q.get("bars") is not None:
            q["bars"] = orient_bars(q["bars"], axis)
        q["axis"] = "y" if axis == "x" else q.get("axis", "y")
        return q
    return [_orient_rectangle_item(x, axis) for x in rectangles]


def restore_rectangles(rectangles, axis):
    """Restore work-coordinate rectangles to source world ``(x, y)`` axes."""

    return orient_rectangles(rectangles, axis)


def _swap_parts(parts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for part in parts or []:
        out.append(
            {
                **{k: v for k, v in part.items() if k not in {"exterior", "holes"}},
                "exterior": [[float(y), float(x)] for x, y, *_ in part.get("exterior", [])],
                "holes": [
                    [[float(y), float(x)] for x, y, *_ in ring]
                    for ring in part.get("holes", [])
                ],
            }
        )
    return out


def restore_bar_layout(layout: Mapping[str, Any], axis: str) -> dict[str, Any]:
    """Restore the public result of ``layout_rebars_y`` to world axes."""

    axis = normalize_axis(axis)
    if axis == "y":
        q = deepcopy(dict(layout))
        q["axis"] = "y"
        return q

    q = deepcopy(dict(layout))
    q["axis"] = "x"
    q["bars"] = orient_bars(q.get("bars", []), "x")

    for zone in q.get("zones", []):
        for key in ("bounds", "primary_bounds", "original_bounds", "fitted_bounds", "anchored_bounds"):
            if zone.get(key) is not None:
                zone[key] = _swap_bounds(zone[key])
        if zone.get("bars") is not None:
            zone["bars"] = orient_bars(zone["bars"], "x")
        if zone.get("parts") is not None:
            zone["parts"] = _swap_parts(zone["parts"])

    for component in q.get("components", []):
        if component.get("bounds") is not None:
            component["bounds"] = _swap_bounds(component["bounds"])
        if "background_positions" in component:
            component["background_coordinates"] = list(component["background_positions"])
        component["cross_axis"] = "y"

    for track in q.get("tracks", []):
        if "guide_x" in track:
            track["guide_y"] = track.pop("guide_x")
        if "x" in track:
            track["y"] = track.pop("x")
        if "allowed_x" in track:
            track["allowed_y"] = track.pop("allowed_x")
        if "intervals" in track:
            track["longitudinal_intervals"] = list(track["intervals"])

    for guide in q.get("guides", []):
        if "x" in guide:
            guide["y"] = guide.pop("x")
        if "y0" in guide:
            guide["x0"] = guide.pop("y0")
        if "y1" in guide:
            guide["x1"] = guide.pop("y1")
        guide["cross_axis"] = "y"

    return q


def grid_rectangles_to_world(
    rectangles: Iterable[Sequence[int]],
    work_x_edges: Sequence[float],
    work_y_edges: Sequence[float],
    axis: str,
):
    """Convert inclusive work-grid indices to source physical coordinates."""

    xe = np.asarray(work_x_edges, dtype=float)
    ye = np.asarray(work_y_edges, dtype=float)
    if xe.ndim != 1 or ye.ndim != 1 or len(xe) < 2 or len(ye) < 2:
        raise ValueError("work_x_edges/work_y_edges должны быть одномерными границами")
    if np.any(np.diff(xe) <= 0) or np.any(np.diff(ye) <= 0):
        raise ValueError("Границы сетки должны строго возрастать")

    world = []
    for i, raw in enumerate(rectangles):
        if len(raw) != 5:
            raise ValueError(f"rectangles[{i}] должен иметь формат (x0,y0,x1,y1,class)")
        x0, y0, x1, y1, cls = raw
        if any(isinstance(v, bool) or int(v) != v for v in (x0, y0, x1, y1)):
            raise ValueError(f"Индексы rectangles[{i}] должны быть целыми")
        x0, y0, x1, y1 = map(int, (x0, y0, x1, y1))
        if not (0 <= x0 <= x1 < len(xe) - 1 and 0 <= y0 <= y1 < len(ye) - 1):
            raise ValueError(f"rectangles[{i}] выходит за границы рабочей сетки")
        world.append((float(xe[x0]), float(ye[y0]), float(xe[x1 + 1]), float(ye[y1 + 1]), int(cls)))

    return restore_rectangles(world, axis)


def _lookup(mapping: Mapping[Any, Any], key: Any):
    if key in mapping:
        return mapping[key]
    text = str(key)
    if text in mapping:
        return mapping[text]
    try:
        number = int(key)
    except (TypeError, ValueError):
        return None
    return mapping.get(number)


def class_holds(
    diameters: Mapping[Any, float],
    recipes: Mapping[Any, Sequence[Any]] | None = None,
    anchor_factor: float = 32.0,
) -> tuple[dict[Any, float], dict[Any, float], dict[Any, tuple[Any, ...]]]:
    """Return base holds, holds for every recipe class and recursive leaves."""

    factor = float(anchor_factor)
    if factor < 0:
        raise ValueError("anchor_factor не может быть отрицательным")
    recipes = dict(recipes or {})
    base = {k: factor * float(v) for k, v in diameters.items()}
    leaves: dict[Any, tuple[Any, ...]] = {}

    def expand(cls: Any, trail: tuple[Any, ...] = ()) -> tuple[Any, ...]:
        if cls in leaves:
            return leaves[cls]
        recipe = _lookup(recipes, cls)
        if recipe is None:
            if _lookup(base, cls) is None:
                raise ValueError(f"Нет diameter и recipe для class={cls}")
            leaves[cls] = (cls,)
            return leaves[cls]
        if cls in trail:
            raise ValueError(f"Циклический recipe: {' -> '.join(map(str, (*trail, cls)))}")
        if isinstance(recipe, (str, bytes)) or not recipe:
            raise ValueError(f"Некорректный recipe для class={cls}")
        leaves[cls] = tuple(x for part in recipe for x in expand(part, (*trail, cls)))
        return leaves[cls]

    keys = list(diameters) + list(recipes)
    for cls in keys:
        expand(cls)
    all_holds = {
        cls: max(float(_lookup(base, leaf)) for leaf in cls_leaves)
        for cls, cls_leaves in leaves.items()
    }
    return base, all_holds, leaves


def _field_geometry(field: Any):
    if field is None:
        return None
    if hasattr(field, "geom_type"):
        return field
    from shapely.ops import unary_union

    geoms = []
    for item in field:
        try:
            geoms.append(_geometry_from_item(item))
        except Exception:
            continue
    return unary_union(geoms) if geoms else None


def _polygon_parts(geometry: Any) -> list[Any]:
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    if hasattr(geometry, "geoms"):
        return [p for g in geometry.geoms for p in _polygon_parts(g)]
    return []


def _box_source_items(boxes: Any) -> list[Any]:
    if isinstance(boxes, Mapping):
        if boxes.get("rectangles") is not None:
            return list(boxes["rectangles"])
        if boxes.get("zones") is not None:
            return list(boxes["zones"])
        raise ValueError("boxes-словарь должен содержать rectangles или zones")
    return list(boxes or [])


def _parse_box(item: Any, index: int) -> tuple[tuple[float, float, float, float], Any, dict[str, Any]]:
    if isinstance(item, Mapping):
        bounds = item.get("bounds") or item.get("final rectangle") or item.get("final_rectangle")
        if bounds is None and item.get("geometry") is not None:
            bounds = item["geometry"].bounds
        cls = item.get("class")
        meta = dict(item)
    else:
        q = tuple(item)
        if len(q) < 5:
            raise ValueError(f"boxes[{index}] должен содержать x0,y0,x1,y1,class")
        bounds, cls, meta = q[:4], q[4], {}
    if bounds is None:
        raise ValueError(f"boxes[{index}]: отсутствуют bounds")
    x0, y0, x1, y1 = map(float, bounds[:4])
    if not (x0 < x1 and y0 < y1):
        raise ValueError(f"boxes[{index}]: некорректные bounds")
    return (x0, y0, x1, y1), cls, meta


def add_box_anchorage(
    boxes: Any,
    *,
    recipes: Mapping[Any, Sequence[Any]] | None,
    diameters: Mapping[Any, float],
    steps: Mapping[Any, float],
    anchor_factor: float = 32.0,
    axis: str = "y",
    field: Any = None,
) -> list[dict[str, Any]]:
    """Expand fitted boxes into physical recipe layers and add anchorage.

    Expansion is only along the world reinforcement axis. Rectangle bounds are
    clamped to the connected field polygon bounds; ``geometry`` is the exact
    intersection with that polygon and is used later for interaction checks.
    """

    from shapely.geometry import box

    axis = normalize_axis(axis)
    recipes = dict(recipes or {})
    _, _, leaves = class_holds(diameters, recipes, anchor_factor)
    field_geom = _field_geometry(field)
    field_parts = _polygon_parts(field_geom)
    out: list[dict[str, Any]] = []

    for source_pos, item in enumerate(_box_source_items(boxes)):
        fitted, parent_cls, meta = _parse_box(item, source_pos)
        explicit = meta.get("diameter") is not None and meta.get("step") is not None
        physical = (parent_cls,) if explicit else leaves.get(parent_cls, (parent_cls,))
        original_id = meta.get("id", source_pos)
        source_index = meta.get("source_index", source_pos)

        original_geom = box(*fitted)
        component = None
        if field_parts:
            ranked = sorted(
                field_parts,
                key=lambda g: (g.intersection(original_geom).area, -g.distance(original_geom)),
                reverse=True,
            )
            component = ranked[0]

        for layer_index, cls in enumerate(physical):
            diameter = float(meta.get("diameter") if explicit else _lookup(diameters, cls))
            step = float(meta.get("step") if explicit else _lookup(steps, cls))
            hold = float(anchor_factor) * diameter
            x0, y0, x1, y1 = fitted
            anchored = (x0, y0 - hold, x1, y1 + hold) if axis == "y" else (x0 - hold, y0, x1 + hold, y1)
            if component is not None:
                fx0, fy0, fx1, fy1 = map(float, component.bounds)
                anchored = (
                    max(fx0, anchored[0]),
                    max(fy0, anchored[1]),
                    min(fx1, anchored[2]),
                    min(fy1, anchored[3]),
                )
            geometry = box(*anchored) if component is None else component.intersection(box(*anchored))
            q = {
                **{k: v for k, v in meta.items() if k not in {"bounds", "geometry", "bars", "class", "diameter", "step"}},
                "id": f"{original_id}:{layer_index}" if len(physical) > 1 else original_id,
                "source_index": source_index,
                "component_id": meta.get("component_id"),
                "class": cls,
                "parent_class": parent_cls,
                "layer_index": layer_index,
                "diameter": diameter,
                "step": step,
                "hold": hold,
                "original_bounds": tuple(map(float, meta.get("original_bounds", fitted))),
                "fitted_bounds": fitted,
                "bounds": tuple(map(float, anchored)),
                "geometry": geometry,
                "assigned_polygons": list(meta.get("assigned_polygons", ()) or ()),
            }
            out.append(q)
    return out
