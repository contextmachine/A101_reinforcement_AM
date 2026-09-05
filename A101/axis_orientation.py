from __future__ import annotations

from typing import Iterable, Sequence

import numpy as np


def normalize_axis(axis: str) -> str:
    axis = str(axis).lower()
    if axis not in {"x", "y"}:
        raise ValueError("axis должен быть 'x' или 'y'")
    return axis


def orient_grid(matrix, xs, ys, axis):
    """Normalize the grid so the working reinforcement axis is always ``y``.

    The source matrix uses ``matrix[y, x]``. For source ``axis='x'`` the
    matrix and edge arrays are swapped; all grid-based algorithms may then
    keep using their native ``axis='y'`` convention.
    """

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


def restore_rectangles(rectangles, axis):
    """Restore work-coordinate rectangles to source world ``(x, y)`` axes."""

    axis = normalize_axis(axis)
    rects = [tuple(r) for r in rectangles]
    if axis == "y":
        return rects
    return [(y0, x0, y1, x1, c) for x0, y0, x1, y1, c in rects]


def grid_rectangles_to_world(
    rectangles: Iterable[Sequence[int]],
    work_x_edges: Sequence[float],
    work_y_edges: Sequence[float],
    axis: str,
):
    """Convert inclusive work-grid indices to source physical coordinates.

    Unlike conversion through cumulative cell widths, this preserves a
    non-zero world origin. Polygons stay in source coordinates; only the
    solver rectangles are restored after the normalized grid solve.
    """

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
