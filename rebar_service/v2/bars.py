"""Zones <-> layout boxes, ``layout_rebars`` call, Bar/Zone serialisation and v2 mass metrics.

Pure functions on plain dicts shaped like ``rebar_service/v2/models.py`` (no pydantic, store or
settings imports) so the task ``bars`` stage and both isolated workers share one implementation.

Geometry conventions (``rebar_service/compact_zones.py``): ``direction`` is the transverse unit
vector and the bar axis is ``direction`` rotated 90 degrees CCW.  Vertical bars (axis ``y``) use
``direction=(1, 0)``, horizontal bars (axis ``x``) use ``direction=(0, -1)``.  ``origin`` is the
start of the base bar; the zone holds ``left + right + 1`` bars at ``origin + i*step*direction``
for ``i in [-left, right]``.  All zone and bar geometry is stored WITHOUT anchorage: anchorage is
the number pair ``{start, end}`` attached to every zone and bar and only enters the mass metrics.

Layout boxes handed to ``layout_rebars`` are the *fitted* (unanchored) rectangles: the cross
extent is the tributary width of the bar positions (``count * step``, centred on them, so that
the layout's ``ceil(width / step)`` demand equals the bar count and a single-bar zone is still a
proper box) and the longitudinal extent is ``length``.
"""
from __future__ import annotations

from collections import defaultdict
from math import ceil, fsum, hypot, isfinite, pi
from typing import Any, Mapping, Sequence

from shapely.geometry import LineString, MultiPolygon, Polygon

MASS_KEYS = (
    "with_anchorage_kg",
    "without_anchorage_kg",
    "with_anchorage_unclipped_kg",
    "without_anchorage_unclipped_kg",
)
MASS_GROUPS = ("additional", "bg")
_PHYSICAL_STATES = frozenset({"active", "background_only"})
#: Largest admissible off-axis component of an axis-aligned unit ``direction``.
_ALIGN_TOL = 1e-4
#: Two tracks of one zone belong to the same arithmetic run when their spacing equals the zone
#: step within this many millimetres (guide slots are exact up to floating-point noise).
_RUN_TOL = 1e-3


# --------------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------------


def _axis(axis: Any) -> str:
    value = str(axis).lower()
    if value not in {"x", "y"}:
        raise ValueError("axis должен быть 'x' или 'y'")
    return value


def _frame(axis: str) -> tuple[int, int]:
    """Return ``(cross_index, long_index)`` into an ``(x, y)`` pair for the given bar axis."""
    return (0, 1) if axis == "y" else (1, 0)


def _canonical_direction(axis: str) -> tuple[float, float]:
    return (1.0, 0.0) if axis == "y" else (0.0, -1.0)


def _bar_direction(direction: Sequence[float]) -> tuple[float, float]:
    """Bar axis = transverse ``direction`` rotated 90 degrees CCW."""
    return (-float(direction[1]), float(direction[0]))


def _aligned_direction(direction: Any, axis: str) -> tuple[float, float]:
    """Snap an axis-aligned unit ``direction`` to an exact ``(+-1, 0)`` / ``(0, +-1)`` vector."""
    try:
        dx, dy = float(direction[0]), float(direction[1])
    except (TypeError, ValueError, IndexError, KeyError) as exc:
        raise ValueError(f"direction должен быть парой чисел, получено {direction!r}") from exc
    norm = hypot(dx, dy)
    if not isfinite(norm) or norm <= 0:
        raise ValueError(f"direction должен быть ненулевым вектором, получено {direction!r}")
    unit = (dx / norm, dy / norm)
    cross, _ = _frame(axis)
    if abs(unit[1 - cross]) > _ALIGN_TOL:
        raise ValueError(
            f"direction={direction!r} не соответствует оси axis={axis!r}: "
            "стержни должны быть параллельны оси"
        )
    sign = 1.0 if unit[cross] > 0 else -1.0
    return (sign, 0.0) if cross == 0 else (0.0, sign)


def _arm(zone: Mapping[str, Any]) -> tuple[float, float]:
    arm = zone.get("arm")
    if not isinstance(arm, Mapping):
        raise ValueError(f"zone={zone.get('id')!r}: отсутствует arm {{d, step}}")
    try:
        d, step = float(arm["d"]), float(arm["step"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"zone={zone.get('id')!r}: arm должен содержать числа d и step") from exc
    if not (isfinite(d) and isfinite(step) and d > 0 and step > 0):
        raise ValueError(f"zone={zone.get('id')!r}: d и step должны быть положительными")
    return d, step


def _zone_id(zone: Mapping[str, Any]) -> int:
    try:
        return int(zone["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"zone: некорректный id {zone.get('id')!r}") from exc


def _split_zones(zones: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return ``(bg_zone, additional_zones)``; exactly one bg zone and unique ids are required."""
    bg: list[dict[str, Any]] = []
    additional: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw in zones:
        zone = dict(raw)
        zone_id = _zone_id(zone)
        if zone_id in seen:
            raise ValueError(f"Повторяющийся id зоны: {zone_id}")
        seen.add(zone_id)
        kind = str(zone.get("kind", "additional"))
        if kind == "bg":
            bg.append(zone)
        elif kind == "additional":
            additional.append(zone)
        else:
            raise ValueError(f"zone={zone_id}: неизвестный kind {kind!r}")
    if len(bg) != 1:
        raise ValueError(f"Требуется ровно одна зона kind='bg', получено {len(bg)}")
    return bg[0], additional


def _anchorage_pair(value: Any, label: str) -> tuple[float, float]:
    try:
        start, end = float(value["start"]), float(value["end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{label}: anchorage должен содержать числа start и end") from exc
    if not (isfinite(start) and isfinite(end) and start >= 0 and end >= 0):
        raise ValueError(f"{label}: anchorage должен быть конечным и неотрицательным")
    return start, end


def _unit_mass_kg_per_mm(diameter: float, density: float) -> float:
    """Mass of one millimetre of a round bar: pi*(d/1000)^2/4 * rho / 1000."""
    return pi * (diameter / 1000.0) ** 2 / 4.0 * density / 1000.0


def _zero_mass() -> dict[str, dict[str, float]]:
    return {group: {key: 0.0 for key in MASS_KEYS} for group in MASS_GROUPS}


def _track_cross(track: Mapping[str, Any], axis: str) -> float | None:
    value = track.get("x" if axis == "y" else "y")
    return None if value is None else float(value)


def _component_extent(layout: Mapping[str, Any], component_id: Any, axis: str) -> float:
    """Extent of a field component's bbox along the bar axis (world coordinates)."""
    components = list(layout.get("components", []) or [])
    row = None
    for candidate in components:
        if candidate.get("id") == component_id:
            row = candidate
            break
    if row is None:
        try:
            row = components[int(component_id)]
        except (TypeError, ValueError, IndexError) as exc:
            raise ValueError(f"layout: компонент {component_id!r} не найден") from exc
    x0, y0, x1, y1 = map(float, row["bounds"][:4])
    return (y1 - y0) if axis == "y" else (x1 - x0)


def _bg_unclipped(layout: Mapping[str, Any], track: Mapping[str, Any], axis: str) -> float:
    """Full background rod length: the component outline at the track, holes ignored.

    The background zone of the component (``layout['zones']`` entries with ``background``)
    carries the outline as ``parts`` (exterior + holes); the rod spans the filled outline at
    the track's cross coordinate. Falls back to the bbox extent when parts are unavailable.
    """
    component_id = track.get("component_id")
    fallback = _component_extent(layout, component_id, axis)
    cross_value = _track_cross(track, axis)
    zone = None
    for candidate in layout.get("zones", []) or []:
        if candidate.get("background") and candidate.get("component_id") == component_id:
            zone = candidate
            break
    if zone is None or cross_value is None:
        return fallback
    try:
        filled = [Polygon(part["exterior"]) for part in zone.get("parts", []) or [] if part.get("exterior")]
    except (KeyError, TypeError, ValueError):
        return fallback
    filled = [poly.buffer(0) for poly in filled if not poly.is_empty]
    if not filled:
        return fallback
    x0, y0, x1, y1 = map(float, zone.get("bounds", layout["components"][int(component_id)]["bounds"])[:4])
    pad = 1.0
    if axis == "y":
        line = LineString([(cross_value, y0 - pad), (cross_value, y1 + pad)])
    else:
        line = LineString([(x0 - pad, cross_value), (x1 + pad, cross_value)])
    length = float(sum(line.intersection(poly).length for poly in filled))
    return length if length > 0 else fallback


# --------------------------------------------------------------------------------------------
# public conversions
# --------------------------------------------------------------------------------------------


def physical_polygons(resolved_rows: Sequence[Any]) -> list[Polygon]:
    """Shapely polygons of the rows whose overlay state is ``active`` or ``background_only``.

    ``removed`` (``real: false``) polygons are dropped, so the union of the returned polygons has
    holes where openings are and the layout clips bars around them and at the field boundary.
    Rows may carry a Shapely ``geometry`` or a ``points`` ring.
    """
    out: list[Polygon] = []
    for row in resolved_rows:
        if hasattr(row, "geom_type"):
            geometry, state = row, "active"
        elif isinstance(row, Mapping):
            state = str(row.get("overlay_state", "active"))
            geometry = row.get("geometry")
            if geometry is None:
                points = row.get("points")
                if points is None:
                    raise ValueError("Полигон должен содержать geometry или points")
                geometry = Polygon([(float(p[0]), float(p[1])) for p in points])
        else:
            raise ValueError("Полигон должен быть словарём или Shapely-геометрией")
        if state not in _PHYSICAL_STATES:
            continue
        if not geometry.is_valid:
            geometry = geometry.buffer(0)
        if geometry.is_empty or geometry.area <= 0:
            continue
        if isinstance(geometry, MultiPolygon):
            out.extend(part for part in geometry.geoms if not part.is_empty and part.area > 0)
        else:
            out.append(geometry)
    return out


def resolve_anchorage(zone: Mapping[str, Any], anchor_factor: float) -> tuple[float, float]:
    """``(start, end)`` anchorage of a zone: its explicit ``anchorage`` or ``anchor_factor*d``."""
    factor = float(anchor_factor)
    if not isfinite(factor) or factor < 0:
        raise ValueError("anchor_factor должен быть конечным и неотрицательным")
    d, _ = _arm(zone)
    explicit = zone.get("anchorage")
    if explicit is None:
        hold = factor * d
        return hold, hold
    return _anchorage_pair(explicit, f"zone={zone.get('id')!r}")


def zone_to_box(zone: Mapping[str, Any], *, axis: str) -> dict[str, Any]:
    """Convert a ``ZoneAdditional`` into a fitted (unanchored) ``layout_rebars`` box.

    Returns ``{"id", "bounds": (x0, y0, x1, y1), "diameter", "step"}``.  The cross extent is the
    tributary width of the ``left + right + 1`` bar positions (``step / 2`` beyond the outermost
    bars) and the longitudinal extent is ``length`` measured from ``origin`` along the bar axis.
    """
    axis = _axis(axis)
    if str(zone.get("kind", "additional")) != "additional":
        raise ValueError(f"zone={zone.get('id')!r}: в layout-box преобразуются только additional-зоны")
    zone_id = _zone_id(zone)
    d, step = _arm(zone)
    try:
        left, right = int(zone.get("left", 0) or 0), int(zone.get("right", 0) or 0)
        length = float(zone["length"])
        origin = (float(zone["origin"][0]), float(zone["origin"][1]))
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise ValueError(f"zone={zone_id}: требуются left, right, length, origin, direction") from exc
    if left < 0 or right < 0:
        raise ValueError(f"zone={zone_id}: left и right должны быть неотрицательными")
    if not isfinite(length) or length <= 0:
        raise ValueError(f"zone={zone_id}: length должен быть положительным")
    if not all(map(isfinite, origin)):
        raise ValueError(f"zone={zone_id}: origin должен быть конечным")
    direction = _aligned_direction(zone.get("direction"), axis)
    cross, long = _frame(axis)
    sign_cross = direction[cross]
    sign_long = _bar_direction(direction)[long]

    c_first = origin[cross] - left * step * sign_cross
    c_last = origin[cross] + right * step * sign_cross
    c_lo, c_hi = min(c_first, c_last) - step / 2.0, max(c_first, c_last) + step / 2.0
    l_a, l_b = origin[long], origin[long] + length * sign_long
    l_lo, l_hi = min(l_a, l_b), max(l_a, l_b)
    bounds = (c_lo, l_lo, c_hi, l_hi) if axis == "y" else (l_lo, c_lo, l_hi, c_hi)
    return {"id": zone_id, "bounds": tuple(map(float, bounds)), "diameter": d, "step": step}


def fitted_boxes_to_zones(
    boxes: Sequence[Mapping[str, Any]],
    *,
    axis: str,
    anchor_factor: float,
    bg: Mapping[str, Any],
    bg_anchorage: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Wire zones for fitted rectangles (``add_box_anchorage`` rows or plain fit output).

    The bg zone gets ``id=0``; fitted boxes become ``additional`` zones with ids ``1..n`` in input
    order.  A box uses ``fitted_bounds`` (falling back to ``bounds``), ``diameter``/``d`` and
    ``step``; it holds ``ceil(width / step)`` bars centred in its cross extent (so
    ``zone_to_box`` reproduces a ``count * step`` wide box exactly).  Anchorage comes from
    ``hold_start``/``hold_end`` or ``hold`` when present, else ``anchor_factor * d``.
    """
    axis = _axis(axis)
    factor = float(anchor_factor)
    if not isfinite(factor) or factor < 0:
        raise ValueError("anchor_factor должен быть конечным и неотрицательным")
    bg_zone: dict[str, Any] = {"id": 0, "kind": "bg", "arm": {"d": bg["d"], "step": bg["step"]}}
    bg_d, bg_step = _arm(bg_zone)
    if bg_anchorage is not None:
        bg_start, bg_end = _anchorage_pair(bg_anchorage, "bg")
    else:
        bg_start = bg_end = factor * bg_d
    bg_zone["arm"] = {"d": bg_d, "step": bg_step}
    bg_zone["anchorage"] = {"start": bg_start, "end": bg_end}
    out: list[dict[str, Any]] = [bg_zone]

    cross, long = _frame(axis)
    direction = _canonical_direction(axis)
    sign_cross = direction[cross]
    for index, raw in enumerate(boxes):
        row = dict(raw)
        bounds = row.get("fitted_bounds")
        if bounds is None:
            bounds = row.get("bounds")
        if bounds is None and row.get("geometry") is not None:
            bounds = row["geometry"].bounds
        if bounds is None:
            raise ValueError(f"boxes[{index}]: отсутствуют fitted_bounds/bounds")
        try:
            b = tuple(map(float, bounds[:4]))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"boxes[{index}]: bounds должны содержать четыре числа") from exc
        if len(b) != 4 or not all(map(isfinite, b)) or not (b[0] < b[2] and b[1] < b[3]):
            raise ValueError(f"boxes[{index}]: некорректные bounds {bounds!r}")
        d = row.get("diameter", row.get("d"))
        step = row.get("step")
        if d is None or step is None:
            raise ValueError(f"boxes[{index}]: отсутствуют diameter/step")
        d, step = float(d), float(step)
        if not (isfinite(d) and isfinite(step) and d > 0 and step > 0):
            raise ValueError(f"boxes[{index}]: diameter и step должны быть положительными")

        c_lo, c_hi = b[cross], b[cross + 2]
        l_lo, l_hi = b[long], b[long + 2]
        width = c_hi - c_lo
        count = max(1, int(ceil(width / step - 1e-9)))
        margin = (width - (count - 1) * step) / 2.0
        first = c_lo + margin if sign_cross > 0 else c_hi - margin
        origin = [first, l_lo] if cross == 0 else [l_lo, first]

        default_hold = factor * d
        hold = row.get("hold")
        hold_start = row.get("hold_start", hold if hold is not None else default_hold)
        hold_end = row.get("hold_end", hold if hold is not None else default_hold)
        start, end = _anchorage_pair({"start": hold_start, "end": hold_end}, f"boxes[{index}]")
        out.append({
            "id": index + 1,
            "kind": "additional",
            "arm": {"d": d, "step": step},
            "left": 0,
            "right": count - 1,
            "length": float(l_hi - l_lo),
            "anchorage": {"start": start, "end": end},
            "origin": [float(origin[0]), float(origin[1])],
            "direction": [float(direction[0]), float(direction[1])],
        })
    return out


# --------------------------------------------------------------------------------------------
# layout -> zones / bars / mass
# --------------------------------------------------------------------------------------------


def _runs(rows: Sequence[Mapping[str, Any]], axis: str, step: float) -> list[list[Mapping[str, Any]]]:
    """Split one zone's tracks (sorted by cross coordinate) into exact arithmetic runs of ``step``."""
    ordered = sorted(rows, key=lambda t: (_track_cross(t, axis), int(t["id"])))
    runs: list[list[Mapping[str, Any]]] = []
    for track in ordered:
        coordinate = _track_cross(track, axis)
        if runs and abs(coordinate - _track_cross(runs[-1][-1], axis) - step) <= _RUN_TOL:
            runs[-1].append(track)
        else:
            runs.append([track])
    return runs


def _run_zone(
    zone: Mapping[str, Any],
    run: Sequence[Mapping[str, Any]],
    *,
    zone_id: int,
    axis: str,
    d: float,
    step: float,
    direction: tuple[float, float],
    anchorage: tuple[float, float],
    keep_split: bool,
) -> dict[str, Any]:
    cross, long = _frame(axis)
    coordinates = sorted(_track_cross(t, axis) for t in run)
    if direction[cross] < 0:
        coordinates.reverse()
    count = len(coordinates)
    left, right = int(zone.get("left", 0) or 0), int(zone.get("right", 0) or 0)
    if not keep_split or left + right + 1 != count:
        left, right = 0, count - 1
    base_cross = coordinates[left]
    origin_long = float(zone["origin"][long])
    origin = [base_cross, origin_long] if cross == 0 else [origin_long, base_cross]
    return {
        "id": int(zone_id),
        "kind": "additional",
        "arm": {"d": float(d), "step": float(step)},
        "left": left,
        "right": right,
        "length": float(zone["length"]),
        "anchorage": {"start": float(anchorage[0]), "end": float(anchorage[1])},
        "origin": [float(origin[0]), float(origin[1])],
        "direction": [float(direction[0]), float(direction[1])],
    }


def _derive(
    layout: Mapping[str, Any],
    zones: Sequence[Mapping[str, Any]],
    *,
    axis: str,
    anchor_factor: float,
) -> dict[str, Any]:
    """Re-derive wire zones from the laid-out tracks and describe every track.

    Returns ``{"zones": [...], "track_zone": {track_id: zone_id}, "track_info": {track_id: {...}}}``
    where ``track_info`` holds ``group`` (``bg``/``additional``), ``d``, ``anchorage`` oriented to
    the bar's (lower, upper) ends and ``unclipped`` (full rod length without anchorage).
    """
    axis = _axis(axis)
    cross, long = _frame(axis)
    bg, additional = _split_zones(zones)
    bg_id = _zone_id(bg)
    bg_d, bg_step = _arm(bg)
    bg_anchorage = resolve_anchorage(bg, anchor_factor)

    out_zones: list[dict[str, Any]] = [{
        "id": bg_id,
        "kind": "bg",
        "arm": {"d": bg_d, "step": bg_step},
        "anchorage": {"start": bg_anchorage[0], "end": bg_anchorage[1]},
    }]
    track_zone: dict[int, int] = {}
    track_info: dict[int, dict[str, Any]] = {}
    by_zone: dict[int, list[Mapping[str, Any]]] = defaultdict(list)

    for track in layout.get("tracks", []) or []:
        if _track_cross(track, axis) is None:
            continue
        tid = int(track["id"])
        if track.get("background"):
            track_zone[tid] = bg_id
            track_info[tid] = {
                "group": "bg",
                "d": bg_d,
                "anchorage": bg_anchorage,
                "unclipped": _bg_unclipped(layout, track, axis),
            }
        else:
            by_zone[int(track["input_zone_index"])].append(track)

    next_id = max((_zone_id(z) for z in zones), default=0) + 1
    for index, zone in enumerate(additional):
        zone_id = _zone_id(zone)
        d, step = _arm(zone)
        anchorage = resolve_anchorage(zone, anchor_factor)
        direction = _aligned_direction(zone.get("direction"), axis)
        # Bars are reported from their lower to their upper coordinate; when the zone's bar
        # axis points the other way its ``start`` anchorage sits at the bar's upper end.
        bar_anchorage = anchorage if _bar_direction(direction)[long] > 0 else (anchorage[1], anchorage[0])
        rows = by_zone.get(index, [])
        # A hole can split one rod into several layout tracks at the same cross coordinate;
        # they form ONE zone member and ONE unclipped rod (the segments stay separate bars).
        representatives: list[Mapping[str, Any]] = []
        siblings: dict[int, list[int]] = {}
        for track in sorted(rows, key=lambda t: (_track_cross(t, axis), int(t["id"]))):
            coordinate = _track_cross(track, axis)
            if representatives and abs(coordinate - _track_cross(representatives[-1], axis)) <= _RUN_TOL:
                siblings[int(representatives[-1]["id"])].append(int(track["id"]))
            else:
                representatives.append(track)
                siblings[int(track["id"])] = []
        for track in rows:
            track_info[int(track["id"])] = {
                "group": "additional",
                "d": d,
                "anchorage": bar_anchorage,
                "unclipped": float(zone["length"]),
                "rod": True,
            }
        for tid, extra in siblings.items():
            for other in extra:
                track_info[other]["rod"] = False
        for k, run in enumerate(_runs(representatives, axis, step)):
            if k == 0:
                run_id = zone_id
            else:
                run_id, next_id = next_id, next_id + 1
            out_zones.append(_run_zone(
                zone, run, zone_id=run_id, axis=axis, d=d, step=step, direction=direction,
                anchorage=anchorage, keep_split=(k == 0),
            ))
            for track in run:
                track_zone[int(track["id"])] = run_id
                for other in siblings.get(int(track["id"]), []):
                    track_zone[other] = run_id
    return {"zones": out_zones, "track_zone": track_zone, "track_info": track_info}


def _bars(layout: Mapping[str, Any], derived: Mapping[str, Any], *, axis: str) -> list[dict[str, Any]]:
    cross, long = _frame(axis)
    track_zone, track_info = derived["track_zone"], derived["track_info"]
    out: list[dict[str, Any]] = []
    for row in layout.get("bars", []) or []:
        tid = int(row["track_id"])
        if tid not in track_zone:
            continue
        info = track_info[tid]
        p0 = [float(row["x0"]), float(row["y0"])]
        p1 = [float(row["x1"]), float(row["y1"])]
        if p0[long] > p1[long]:
            p0, p1 = p1, p0
        p1[cross] = p0[cross]
        out.append({
            "zone_id": int(track_zone[tid]),
            "start": p0,
            "end": p1,
            "d": float(info["d"]),
            "anchorage": {"start": float(info["anchorage"][0]), "end": float(info["anchorage"][1])},
        })
    return out


def _mass_metrics(
    layout: Mapping[str, Any], derived: Mapping[str, Any], *, density: float
) -> dict[str, dict[str, float]]:
    """Per-group masses (kg) of design section 5.

    * ``without_anchorage_kg``: visible clipped segments.
    * ``with_anchorage_kg``: every visible segment plus its start and end anchorage (a segment
      clipped by the field boundary or a hole still anchors invisibly beyond the clipped end).
    * ``without_anchorage_unclipped_kg``: one full unclipped rod per track (holes ignored).
    * ``with_anchorage_unclipped_kg``: the full rod plus one start and one end anchorage.
    """
    visible: dict[int, list[float]] = defaultdict(list)
    for row in layout.get("bars", []) or []:
        length = hypot(float(row["x1"]) - float(row["x0"]), float(row["y1"]) - float(row["y0"]))
        visible[int(row["track_id"])].append(length)
    parts = {group: {key: [] for key in MASS_KEYS} for group in MASS_GROUPS}
    for tid, info in derived["track_info"].items():
        unit = _unit_mass_kg_per_mm(float(info["d"]), density)
        anchorage = float(info["anchorage"][0]) + float(info["anchorage"][1])
        segments = visible.get(tid, [])
        total_visible = fsum(segments)
        unclipped = float(info["unclipped"]) if info.get("rod", True) else 0.0
        target = parts[info["group"]]
        target["without_anchorage_kg"].append(unit * total_visible)
        target["with_anchorage_kg"].append(unit * (total_visible + anchorage * len(segments)))
        if info.get("rod", True):
            target["without_anchorage_unclipped_kg"].append(unit * unclipped)
            target["with_anchorage_unclipped_kg"].append(unit * (unclipped + anchorage))
    return {group: {key: fsum(values) for key, values in rows.items()} for group, rows in parts.items()}


def zones_from_layout(
    layout: Mapping[str, Any], zones: Sequence[Mapping[str, Any]], *, axis: str, anchor_factor: float
) -> list[dict[str, Any]]:
    """Wire zones re-derived from ``layout['tracks']`` (input ids preserved, extra runs get new ids)."""
    return _derive(layout, zones, axis=axis, anchor_factor=anchor_factor)["zones"]


def bars_from_layout(
    layout: Mapping[str, Any], zones: Sequence[Mapping[str, Any]], *, axis: str, anchor_factor: float
) -> list[dict[str, Any]]:
    """Wire bars of a feasible ``layout_rebars`` result (see :func:`layout_zones`)."""
    axis = _axis(axis)
    derived = _derive(layout, zones, axis=axis, anchor_factor=anchor_factor)
    return _bars(layout, derived, axis=axis)


def mass_metrics_from_layout(
    layout: Mapping[str, Any],
    zones: Sequence[Mapping[str, Any]],
    *,
    axis: str,
    anchor_factor: float,
    steel_density_kg_m3: float,
) -> dict[str, dict[str, float]]:
    """``MassMetrics`` dict of a feasible ``layout_rebars`` result (see :func:`layout_zones`)."""
    axis = _axis(axis)
    density = float(steel_density_kg_m3)
    if not isfinite(density) or density <= 0:
        raise ValueError("steel_density_kg_m3 должен быть положительным")
    derived = _derive(layout, zones, axis=axis, anchor_factor=anchor_factor)
    return _mass_metrics(layout, derived, density=density)


def layout_zones(
    polygons: Sequence[Any],
    zones: Sequence[Mapping[str, Any]],
    *,
    axis: str,
    anchor_factor: float,
    steel_density_kg_m3: float,
    min_step: float,
) -> dict[str, Any]:
    """Lay out bars for wire zones over the physical field polygons.

    ``polygons`` are the physical polygons of the scene (``active`` + ``background_only``; see
    :func:`physical_polygons`), so bars are clipped at the field boundary and around openings.
    ``zones`` must contain exactly one ``bg`` zone (its ``arm`` is the background grid) and any
    number of ``additional`` zones.  ``min_step`` is the guide quantum lower bound of
    ``layout_rebars`` (``min_bar_gap_mm`` or ``REBAR_MIN_INTERNAL_STEP``).

    Returns ``{"is_feasible", "status", "bars", "zones", "mass_metrics", "layout", "warnings",
    "errors"}``.  An infeasible or partial layout yields empty ``bars``/``zones`` and zero masses;
    ``layout`` is the raw ``layout_rebars`` output for diagnostics.
    """
    from A101.rebar_field_layout import layout_rebars

    axis = _axis(axis)
    factor = float(anchor_factor)
    if not isfinite(factor) or factor < 0:
        raise ValueError("anchor_factor должен быть конечным и неотрицательным")
    density = float(steel_density_kg_m3)
    if not isfinite(density) or density <= 0:
        raise ValueError("steel_density_kg_m3 должен быть положительным")
    quantum = float(min_step)
    if not isfinite(quantum) or quantum <= 0:
        raise ValueError("min_step должен быть положительным")

    field = [p for p in polygons if not (hasattr(p, "is_empty") and p.is_empty)]
    if not field:
        raise ValueError("Не переданы физические полигоны сцены")
    bg, additional = _split_zones(zones)
    bg_d, bg_step = _arm(bg)
    boxes = [zone_to_box(zone, axis=axis) for zone in additional]

    layout = dict(layout_rebars(
        field, boxes, background=(bg_d, bg_step), axis=axis, min_step=quantum,
    ) or {})
    status = str(layout.get("status", "") or "").lower()
    warnings = list(layout.get("warnings", []) or [])
    errors = list(layout.get("errors", []) or [])
    if not layout.get("is_feasible"):
        return {
            "is_feasible": False,
            "status": status or "infeasible",
            "bars": [],
            "zones": [],
            "mass_metrics": _zero_mass(),
            "layout": layout,
            "warnings": warnings,
            "errors": errors,
        }
    derived = _derive(layout, zones, axis=axis, anchor_factor=factor)
    return {
        "is_feasible": True,
        "status": status or "feasible",
        "bars": _bars(layout, derived, axis=axis),
        "zones": derived["zones"],
        "mass_metrics": _mass_metrics(layout, derived, density=density),
        "layout": layout,
        "warnings": warnings,
        "errors": errors,
    }
