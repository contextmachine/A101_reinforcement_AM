from __future__ import annotations

from collections import defaultdict
from math import hypot
from typing import Any, Mapping, Sequence

_EPS = 1e-6


def _bar_tuple(value: Any) -> tuple[float, float, float, float]:
    if isinstance(value, Mapping):
        return tuple(float(value[k]) for k in ("x0", "y0", "x1", "y1"))
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        return tuple(map(float, value[:4]))
    raise ValueError("Некорректное представление стержня")


def _canonical_bar(bar: Sequence[float]) -> dict[str, Any]:
    x0, y0, x1, y1 = map(float, bar[:4])
    vx, vy = x1 - x0, y1 - y0
    length = hypot(vx, vy)
    if length <= _EPS:
        raise ValueError("Нулевая длина стержня")
    bx, by = vx / length, vy / length
    # direction is the layout direction, therefore bar_dir = rot90ccw(direction)
    dx, dy = by, -bx
    # Canonicalize direction sign so axis-aligned current layouts remain stable:
    # vertical bars -> +X layout, horizontal bars -> -Y layout.
    if abs(bx) < abs(by):
        if dx < 0:
            dx, dy, bx, by = -dx, -dy, -bx, -by
            x0, y0, x1, y1 = x1, y1, x0, y0
    else:
        if dy > 0:
            dx, dy, bx, by = -dx, -dy, -bx, -by
            x0, y0, x1, y1 = x1, y1, x0, y0
    cross = x0 * dx + y0 * dy
    longitudinal = x0 * bx + y0 * by
    return {
        "origin": (x0, y0),
        "end": (x1, y1),
        "direction": (dx, dy),
        "bar_direction": (bx, by),
        "length": length,
        "cross": cross,
        "longitudinal": longitudinal,
    }


def _near(a: float, b: float, tol: float = _EPS) -> bool:
    return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(a)), abs(float(b)))


def _regular_runs(rows: list[dict[str, Any]], step: float) -> list[list[dict[str, Any]]]:
    """Partition bars into exact arithmetic runs without dropping duplicate tracks."""
    remaining = list(sorted(rows, key=lambda row: (row["cross"], row["longitudinal"], row["length"])))
    runs: list[list[dict[str, Any]]] = []
    tol = max(_EPS, abs(step) * 1e-6)
    while remaining:
        seed = remaining.pop(0)
        run = [seed]
        target = seed["cross"] + step
        while True:
            hit = None
            for index, row in enumerate(remaining):
                if abs(row["cross"] - target) > tol:
                    continue
                if not _near(row["longitudinal"], seed["longitudinal"], tol):
                    continue
                if not _near(row["length"], seed["length"], tol):
                    continue
                dx, dy = row["direction"]
                sdx, sdy = seed["direction"]
                if not (_near(dx, sdx, tol) and _near(dy, sdy, tol)):
                    continue
                hit = index
                break
            if hit is None:
                break
            row = remaining.pop(hit)
            run.append(row)
            target += step
        runs.append(run)
    return runs


def compact_zones_from_layout(layout: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Serialize final supplemental unclipped anchored tracks into compact regular groups.

    Background zones are deliberately excluded: callers can transmit ``back_grid``
    separately.  If a caller wants background among zones, the same compact schema
    can be produced explicitly from background geometry upstream.
    """
    result: list[dict[str, Any]] = []
    for zone in list(layout.get("zones", []) or []):
        if zone.get("background"):
            continue
        diameter = float(zone.get("diameter") or 0)
        step = float(zone.get("step") or 0)
        if diameter <= 0 or step <= 0:
            continue
        bars = zone.get("bars_with_anchorage_unclipped")
        if bars is None:
            raise ValueError("unclipped anchored tracks are missing; relayout this saved solution first")
        normalized = [_canonical_bar(_bar_tuple(bar)) for bar in bars]
        if not normalized:
            continue
        # Bars with different longitudinal spans/orientations cannot be one compact zone.
        buckets: dict[tuple[int, ...], list[dict[str, Any]]] = defaultdict(list)
        tol = max(_EPS, step * 1e-6)
        for row in normalized:
            key = (
                round(row["direction"][0] / tol),
                round(row["direction"][1] / tol),
                round(row["longitudinal"] / tol),
                round(row["length"] / tol),
            )
            buckets[key].append(row)
        for rows in buckets.values():
            for run in _regular_runs(rows, step):
                base = run[0]
                result.append(
                    {
                        "origin": [float(base["origin"][0]), float(base["origin"][1])],
                        "direction": [float(base["direction"][0]), float(base["direction"][1])],
                        "length": float(base["length"]),
                        "step": float(step),
                        "right": max(0, len(run) - 1),
                        "left": 0,
                        "d": float(diameter),
                    }
                )
    return result


def compact_zones_from_anchored_boxes(
    boxes: Sequence[Mapping[str, Any]], *, axis: str = "y"
) -> list[dict[str, Any]]:
    """Best-effort compact representation for a component frontier before field layout."""
    result: list[dict[str, Any]] = []
    for raw in boxes:
        row = dict(raw)
        bounds = row.get("anchored_bounds_unclipped") or row.get("bounds")
        if not bounds or len(bounds) < 4:
            continue
        x0, y0, x1, y1 = map(float, bounds[:4])
        step = float(row.get("step") or 0)
        diameter = float(row.get("diameter") or 0)
        if step <= 0 or diameter <= 0:
            continue
        if str(axis) == "x":
            # Horizontal bars; +direction points down so CCW rotation points +X.
            direction = (0.0, -1.0)
            length = max(0.0, x1 - x0)
            count = max(1, int(round((y1 - y0) / step)) + 1)
            origin = (x0, y1)
        else:
            direction = (1.0, 0.0)
            length = max(0.0, y1 - y0)
            count = max(1, int(round((x1 - x0) / step)) + 1)
            origin = (x0, y0)
        result.append(
            {
                "origin": [origin[0], origin[1]],
                "direction": [direction[0], direction[1]],
                "length": length,
                "step": step,
                "right": count - 1,
                "left": 0,
                "d": diameter,
            }
        )
    return result
