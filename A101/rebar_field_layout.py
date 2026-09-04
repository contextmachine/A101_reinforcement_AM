from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import permutations
from math import ceil, floor
from typing import Any, Mapping, Sequence

from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPolygon, Polygon, box, shape
from shapely.ops import unary_union

_EPS = 1e-7
VERSION = "v10-robust-global-layout-2026-09-04"


@dataclass
class _Zone:
    index: int
    input_id: Any
    source_index: Any
    cls: Any
    bounds: tuple[float, float, float, float]
    geometry: Any
    diameter: float
    step: float
    assigned: tuple[int, ...] = ()
    raw: Any = None
    bars: list[tuple[float, float, float, float]] = field(default_factory=list)
    track_ids: list[int] = field(default_factory=list)
    components: set[int] = field(default_factory=set)
    parent_class: Any = None
    layer_index: int | None = None


@dataclass
class _Track:
    id: int
    zone: int | None
    component: int
    guide: float
    diameter: float
    step: float
    intervals: list[tuple[float, float]]
    allowed: tuple[float, float]
    background: bool = False
    ordinal: int = 0
    x: float | None = None


def _geom(value: Any):
    if hasattr(value, "geom_type"):
        return value
    if isinstance(value, Mapping):
        if "geometry" in value:
            return _geom(value["geometry"])
        if "points" in value:
            return Polygon(value["points"])
        if "type" in value and "coordinates" in value:
            return shape(value)
    if isinstance(value, (list, tuple)):
        if len(value) == 2 and (hasattr(value[0], "geom_type") or isinstance(value[0], (list, tuple, Mapping))):
            try:
                return _geom(value[0])
            except Exception:
                pass
        return Polygon([tuple(map(float, p[:2])) for p in value])
    raise ValueError("Полигон должен быть Shapely-геометрией, GeoJSON или массивом координат")


def _polygons(items: Sequence[Any]) -> tuple[list[Any], list[Polygon]]:
    geoms = []
    for item in items:
        g = _geom(item)
        if not g.is_valid:
            g = g.buffer(0)
        if g.is_empty:
            continue
        geoms.append(g)
    if not geoms:
        raise ValueError("Не переданы непустые полигоны")
    merged = unary_union(geoms)
    parts = _polygon_geoms(merged)
    if not parts:
        raise ValueError("Объединение полигонов не содержит площадных компонент")
    parts.sort(key=lambda g: (g.bounds[0], g.bounds[1], g.bounds[2], g.bounds[3]))
    return geoms, parts


def _polygon_geoms(g: Any) -> list[Polygon]:
    if isinstance(g, Polygon):
        return [g]
    if isinstance(g, MultiPolygon):
        return list(g.geoms)
    if isinstance(g, GeometryCollection):
        return [p for x in g.geoms for p in _polygon_geoms(x)]
    return []


def _lookup(values: Mapping[Any, Any], key: Any):
    try:
        if key in values:
            return values[key]
    except TypeError:
        pass
    text = str(key)
    if text in values:
        return values[text]
    try:
        number = int(key)
    except (TypeError, ValueError):
        return None
    return values.get(number)


def _item_class(item: Any) -> tuple[Any, bool]:
    """Return (class, has explicit diameter/step pair)."""
    if isinstance(item, Mapping):
        explicit = any(item.get(k) is not None for k in ("diameter", "step", "rebar", "reinforcement"))
        return item.get("class"), explicit
    if isinstance(item, (str, bytes, bytearray)):
        raise ValueError("Элемент boxes не может быть строкой")
    q = tuple(item)
    if len(q) == 6:
        return None, True
    if len(q) == 5:
        value = q[4]
        return (None, True) if isinstance(value, (list, tuple)) and len(value) == 2 else (value, False)
    if len(q) == 2 and hasattr(q[0], "bounds"):
        return q[1], False
    return None, False


def _recipe_leaves(cls: Any, recipes: Mapping[Any, Sequence[Any]], trail: tuple[Any, ...] = ()) -> list[Any]:
    recipe = _lookup(recipes, cls)
    if recipe is None:
        return [cls]
    if cls in trail:
        raise ValueError(f"Циклический recipe: {' -> '.join(map(str, (*trail, cls)))}")
    if isinstance(recipe, (str, bytes)) or not recipe:
        raise ValueError(f"Некорректный recipe для class={cls}: {recipe!r}")
    out: list[Any] = []
    for part in recipe:
        out.extend(_recipe_leaves(part, recipes, (*trail, cls)))
    return out


def _expanded_items(items: Sequence[Any], recipes: Mapping[Any, Sequence[Any]]) -> list[Any]:
    out: list[Any] = []
    for source_index, item in enumerate(items):
        cls, explicit = _item_class(item)
        leaves = [cls] if explicit or cls is None else _recipe_leaves(cls, recipes)
        if len(leaves) == 1 and leaves[0] == cls:
            out.append(item)
            continue
        if isinstance(item, Mapping):
            base = dict(item)
            parent_id = base.get("id", source_index)
            source = base.get("source_index", source_index)
            for layer_index, leaf in enumerate(leaves):
                q = dict(base)
                q.update({
                    "id": f"{parent_id}:{layer_index}",
                    "source_index": source,
                    "class": leaf,
                    "_parent_class": cls,
                    "_layer_index": layer_index,
                })
                out.append(q)
            continue
        q = tuple(item)
        bounds = q[0].bounds if len(q) == 2 and hasattr(q[0], "bounds") else q[:4]
        for layer_index, leaf in enumerate(leaves):
            out.append({
                "id": f"{source_index}:{layer_index}",
                "source_index": source_index,
                "bounds": bounds,
                "class": leaf,
                "_parent_class": cls,
                "_layer_index": layer_index,
            })
    return out


def _zone(item: Any, i: int, diameters: Mapping[Any, float], steps: Mapping[Any, float]) -> _Zone:
    parent_class, layer_index = None, None
    if isinstance(item, Mapping):
        geometry = None
        if item.get("geometry") is not None:
            geometry = _geom(item["geometry"])
            if not geometry.is_valid:
                geometry = geometry.buffer(0)
            if geometry.is_empty:
                geometry = None
        b = geometry.bounds if geometry is not None else (
            item.get("bounds") or item.get("final rectangle") or item.get("final_rectangle")
        )
        if b is None:
            raise ValueError(f"boxes[{i}]: отсутствует geometry/bounds")
        cls = item.get("class")
        pair = item.get("rebar") or item.get("reinforcement")
        d = item.get("diameter")
        s = item.get("step")
        d = _lookup(diameters, cls) if d is None else d
        s = _lookup(steps, cls) if s is None else s
        if pair is not None:
            d = pair[0] if d is None else d
            s = pair[1] if s is None else s
        assigned = tuple(map(int, item.get("assigned_polygons", ()) or ()))
        input_id, source = item.get("id", i), item.get("source_index", i)
        parent_class = item.get("_parent_class", cls)
        layer_index = item.get("_layer_index")
    else:
        if isinstance(item, (str, bytes, bytearray)):
            raise ValueError(f"boxes[{i}] не может быть строкой")
        q = tuple(item)
        if len(q) == 6:
            b, d, s, cls = q[:4], q[4], q[5], None
        elif len(q) == 5:
            b, cls = q[:4], q[4]
            if isinstance(cls, (list, tuple)) and len(cls) == 2:
                d, s, cls = cls[0], cls[1], None
            else:
                d, s = _lookup(diameters, cls), _lookup(steps, cls)
        elif len(q) == 2 and hasattr(q[0], "bounds"):
            b, cls = q[0].bounds, q[1]
            d, s = _lookup(diameters, cls), _lookup(steps, cls)
        else:
            raise ValueError(f"boxes[{i}] должен быть dict, (x0,y0,x1,y1,class) или (...,diameter,step)")
        geometry = _geom(q[0]) if len(q) == 2 and hasattr(q[0], "bounds") else box(*map(float, b[:4]))
        assigned, input_id, source, parent_class = (), i, i, cls
    if d is None or s is None:
        raise ValueError(
            f"boxes[{i}]: не заданы diameter/step для class={cls}. "
            "Для составного класса передайте recipes, а diameters/steps — для базовых классов."
        )
    try:
        x0, y0, x1, y1 = map(float, b[:4])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"boxes[{i}]: bounds должны содержать четыре числа, получено {b!r}") from exc
    d, s = float(d), float(s)
    if not (x0 < x1 and y0 < y1 and d > 0 and s > 0):
        raise ValueError(f"boxes[{i}]: некорректная геометрия или армирование")
    return _Zone(
        index=i,
        input_id=input_id,
        source_index=source,
        cls=cls,
        bounds=(x0, y0, x1, y1),
        geometry=geometry if geometry is not None else box(x0, y0, x1, y1),
        diameter=d,
        step=s,
        assigned=assigned,
        raw=item,
        parent_class=parent_class,
        layer_index=None if layer_index is None else int(layer_index),
    )


def _zones(
    items: Any,
    diameters: Mapping[Any, float] | None,
    steps: Mapping[Any, float] | None,
    recipes: Mapping[Any, Sequence[Any]] | None = None,
) -> list[_Zone]:
    if isinstance(items, Mapping):
        wrapper = items
        if wrapper.get("zones"):
            items = wrapper["zones"]
        elif "rectangles" in wrapper:
            items = wrapper["rectangles"]
        elif "zones" in wrapper:
            items = wrapper["zones"]
        else:
            raise ValueError("boxes-словарь должен содержать ключ 'zones' или 'rectangles'")
        if items is None:
            raise ValueError(
                f"fit-result не содержит рассчитанных прямоугольников; status={wrapper.get('status')!r}"
            )
    if items is None:
        return []
    if isinstance(items, (str, bytes, bytearray, Mapping)):
        raise ValueError("boxes должен быть списком зон/прямоугольников или fit-result")
    raw = list(items)
    expanded = _expanded_items(raw, recipes or {})
    return [_zone(x, i, diameters or {}, steps or {}) for i, x in enumerate(expanded)]

def _merge(intervals: Sequence[tuple[float, float]], eps: float = _EPS) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted((float(a), float(b)) for a, b in intervals if b > a + eps):
        if out and a <= out[-1][1] + eps:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [tuple(x) for x in out]


def _projection(g: Any) -> list[tuple[float, float]]:
    return _merge([(p.bounds[1], p.bounds[3]) for p in _polygon_geoms(g)])


def _segments(g: Any, x: float, intervals: Sequence[tuple[float, float]] | None = None) -> list[tuple[float, float]]:
    if g.is_empty:
        return []
    y0, y1 = g.bounds[1], g.bounds[3]
    hit = g.intersection(LineString([(x, y0 - 1), (x, y1 + 1)]))
    lines = []

    def add(z):
        if z.is_empty:
            return
        if isinstance(z, LineString):
            ys = [p[1] for p in z.coords]
            if max(ys) > min(ys) + _EPS:
                lines.append((min(ys), max(ys)))
        elif isinstance(z, (MultiLineString, GeometryCollection)):
            for q in z.geoms:
                add(q)

    add(hit)
    if intervals is None:
        return _merge(lines)
    return _merge([(max(a, c), min(b, d)) for a, b in lines for c, d in intervals])


def _background_positions(component: Polygon, step: float) -> list[float]:
    x0, _, x1, _ = component.bounds
    width = x1 - x0
    n = max(0, floor(width / step + _EPS))
    first = x0 + (width - n * step) / 2
    return [first + k * step for k in range(n + 1)]


def _guide_slots(left: float, right: float, quantum: float) -> list[float]:
    out, x = [], right
    while x > left + _EPS:
        out.append(x)
        x -= quantum
    return sorted(out)


def _clip_interval(intervals: Sequence[tuple[float, float]], lo: float, hi: float):
    return _merge([(max(a, lo), min(b, hi)) for a, b in intervals])


def _overlap(a: Sequence[tuple[float, float]], b: Sequence[tuple[float, float]]) -> float:
    return sum(max(0.0, min(y1, v1) - max(y0, v0)) for y0, y1 in a for v0, v1 in b)


def _max_layers(interval_sets: Sequence[Sequence[tuple[float, float]]]) -> int:
    events = []
    for intervals in interval_sets:
        for a, b in intervals:
            events += [(a, 1), (b, -1)]
    active = best = 0
    for _, delta in sorted(events, key=lambda z: (z[0], z[1])):
        active += delta
        best = max(best, active)
    return best


def _floor_guide(values: Sequence[float], x: float) -> float:
    return max((v for v in values if v <= x + _EPS), default=values[0])


def _ceil_guide(values: Sequence[float], x: float) -> float:
    return min((v for v in values if v >= x - _EPS), default=values[-1])


def _required_x(zone: _Zone, source_geoms: Sequence[Any], component: Polygon, left: float, right: float, y0: float, y1: float):
    gs = [source_geoms[i] for i in zone.assigned if 0 <= i < len(source_geoms)]
    if not gs:
        return None
    g = unary_union(gs).intersection(component).intersection(box(left, y0, right, y1))
    return None if g.is_empty else (g.bounds[0], g.bounds[2])


def _snap(zone: _Zone, left: float, right: float, slots: list[float], req: tuple[float, float] | None):
    values = [left, *slots]
    raw0, raw1 = max(left, zone.bounds[0]), min(right, zone.bounds[2])
    a = left if raw0 <= left + _EPS else _ceil_guide(values, raw0)
    b = right if raw1 >= right - _EPS else _floor_guide(values, raw1)
    if req:
        a, b = min(a, _floor_guide(values, req[0])), max(b, _ceil_guide(values, req[1]))
    if a >= b - _EPS:
        p = min(slots, key=lambda x: abs(x - (raw0 + raw1) / 2))
        j = values.index(p)
        a, b = values[max(0, j - 1)], p
    return a, b, raw0, raw1


def _strictly_contains(a: Sequence[tuple[float, float]], b: Sequence[tuple[float, float]]) -> bool:
    if not a or not b:
        return False
    return min(x[0] for x in a) <= min(x[0] for x in b) + _EPS and max(x[1] for x in a) >= max(x[1] for x in b) - _EPS and (
        min(x[0] for x in a) < min(x[0] for x in b) - _EPS or max(x[1] for x in a) > max(x[1] for x in b) + _EPS
    )


def _shift_weaker(fragments: list[dict[str, Any]]) -> None:
    for i, a in enumerate(fragments):
        for b in fragments[i + 1:]:
            if not (_strictly_contains(a["intervals"], b["intervals"]) or _strictly_contains(b["intervals"], a["intervals"])):
                continue
            low, high = (a, b) if a["intensity"] < b["intensity"] else (b, a)
            if low["intensity"] == high["intensity"]:
                continue
            values = [low["left"], *low["slots"]]
            if abs(low["b"] - high["b"]) <= _EPS:
                q = _floor_guide(values, low["b"] - _EPS * 10)
                if q > low["a"] + _EPS and (low["required"] is None or q >= low["required"][1] - _EPS):
                    low["b"] = q
            elif abs(low["a"] - high["a"]) <= _EPS:
                q = _ceil_guide(values, low["a"] + _EPS * 10)
                if q < low["b"] - _EPS and (low["required"] is None or q <= low["required"][0] + _EPS):
                    low["a"] = q


def _boundary_conflicts(fragments: list[dict[str, Any]]) -> None:
    by_component = defaultdict(list)
    for f in fragments:
        by_component[f["component"]].append(f)
    for group in by_component.values():
        for a in group:
            for b in group:
                if a["strip_id"] + 1 != b["strip_id"] or abs(a["right"] - b["left"]) > _EPS:
                    continue
                edge = a["right"]
                if abs(a["b"] - edge) > _EPS or abs(b["a"] - edge) > _EPS or _overlap(a["intervals"], b["intervals"]) <= _EPS:
                    continue
                low = a if a["intensity"] < b["intensity"] else b if b["intensity"] < a["intensity"] else None
                if low is None:
                    continue
                values = [low["left"], *low["slots"]]
                if low is a:
                    q = _floor_guide(values, edge - _EPS * 10)
                    if q > low["a"] + _EPS and (low["required"] is None or q >= low["required"][1] - _EPS):
                        low["b"] = q
                else:
                    q = _ceil_guide(values, edge + _EPS * 10)
                    if q < low["b"] - _EPS and (low["required"] is None or q <= low["required"][0] + _EPS):
                        low["a"] = q


def _order_positions(group: list[_Track]) -> tuple[list[int], dict[int, float]]:
    n = len(group)
    weights = {(i, j): _overlap(group[i].intervals, group[j].intervals) for i in range(n) for j in range(i + 1, n)}

    def positions(order):
        p = {order[0]: 0.0}
        for jj in range(1, len(order)):
            j = order[jj]
            p[j] = max((p[i] + (group[i].diameter + group[j].diameter) / 2 for i in order[:jj] if weights.get(tuple(sorted((i, j))), 0) > _EPS), default=0.0)
        return p

    def score(order):
        p = positions(order)
        slack = sum(w * max(0.0, abs(p[j] - p[i]) - (group[i].diameter + group[j].diameter) / 2) for (i, j), w in weights.items() if w > _EPS and i in p and j in p)
        width = max(p[i] + group[i].diameter / 2 for i in order) - min(p[i] - group[i].diameter / 2 for i in order)
        return slack, width, tuple(group[i].id for i in order), p

    if n <= 7:
        best = min((score(o), o) for o in permutations(range(n)))
        return list(best[1]), best[0][3]

    degree = [sum(w for (i, j), w in weights.items() if k in (i, j)) for k in range(n)]
    order = [max(range(n), key=lambda i: (degree[i], group[i].diameter, -group[i].id))]
    left = set(range(n)) - set(order)
    while left:
        j = max(left, key=lambda q: (sum(weights.get(tuple(sorted((q, i))), 0) for i in order), degree[q], -group[q].id))
        best = None
        for k in range(len(order) + 1):
            cand = order[:k] + [j] + order[k:]
            z = score(cand)
            if best is None or z[:3] < best[0][:3]:
                best = z, cand
        order, left = best[1], left - {j}
    return order, positions(order)


def _anchor(group: list[_Track], order: list[int], pos: Mapping[int, float]) -> float:
    ends = sorted({y for t in group for ab in t.intervals for y in ab})
    probes = [(a + b) / 2 for a, b in zip(ends, ends[1:]) if b > a + _EPS]
    active = max(([i for i, t in enumerate(group) if any(a < y < b for a, b in t.intervals)] for y in probes), key=lambda x: (len(x), sum(group[i].diameter for i in x)), default=list(range(len(group))))
    active = sorted(active, key=lambda i: (pos[i], group[i].id))
    m = len(active)
    if m % 2:
        return pos[active[m // 2]]
    a, b = active[m // 2 - 1], active[m // 2]
    return ((pos[a] + group[a].diameter / 2) + (pos[b] - group[b].diameter / 2)) / 2


def _track_overlap(a: _Track, b: _Track) -> bool:
    return a.component == b.component and _overlap(a.intervals, b.intervals) > _EPS


def _guide_preferences(tracks: Sequence[_Track]) -> dict[int, float]:
    """Symmetric same-guide targets; global packing may move them only as needed."""
    out: dict[int, float] = {}
    groups = defaultdict(list)
    for t in tracks:
        groups[(t.component, round(t.guide, 8))].append(t)
    for (_, guide), group in groups.items():
        if len(group) == 1:
            out[group[0].id] = float(guide)
            continue
        if len(group) <= 7:
            order, pos = _order_positions(group)
        else:
            weights = [sum(_overlap(t.intervals, q.intervals) for q in group) for t in group]
            order = sorted(range(len(group)), key=lambda i: (-weights[i], -sum(b-a for a,b in group[i].intervals), -group[i].diameter, group[i].id))
            pos = {}
            for j in order:
                pos[j] = max(
                    (pos[i] + (group[i].diameter + group[j].diameter) / 2
                     for i in pos if _track_overlap(group[i], group[j])),
                    default=0.0,
                )
        anchor = _anchor(group, order, pos)
        for i, t in enumerate(group):
            out[t.id] = float(guide + pos[i] - anchor)
    return out


def _conflict_components(group: Sequence[_Track]) -> list[list[int]]:
    n = len(group)
    adj = [set() for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if _track_overlap(group[i], group[j]):
                adj[i].add(j)
                adj[j].add(i)
    out, seen = [], set()
    for start in range(n):
        if start in seen:
            continue
        stack, part = [start], []
        while stack:
            i = stack.pop()
            if i in seen:
                continue
            seen.add(i)
            part.append(i)
            stack.extend(adj[i] - seen)
        out.append(part)
    return out


def _compact_pack(group: Sequence[_Track], order: Sequence[int], preferred: Mapping[int, float]):
    """Minimum-width placement for an order, shifted by a weighted median."""
    pos: dict[int, float] = {}
    for j in order:
        t = group[j]
        pos[j] = max(
            (pos[i] + (group[i].diameter + t.diameter) / 2
             for i in pos if _track_overlap(group[i], t)),
            default=0.0,
        )
    lo = max(t.allowed[0] - pos[i] for i, t in enumerate(group))
    hi = min(t.allowed[1] - pos[i] for i, t in enumerate(group))
    if lo > hi + _EPS:
        return None
    targets = []
    for i, t in enumerate(group):
        weight = max(_EPS, sum(b - a for a, b in t.intervals)) * (4.0 if t.background else 1.0)
        targets.append((float(preferred[t.id]) - pos[i], weight))
    targets.sort()
    half, acc = sum(w for _, w in targets) / 2, 0.0
    shift = targets[-1][0]
    for value, weight in targets:
        acc += weight
        if acc >= half:
            shift = value
            break
    shift = min(max(shift, lo), hi)
    return {i: pos[i] + shift for i in range(len(group))}


def _pack_order(group: Sequence[_Track], order: Sequence[int], preferred: Mapping[int, float]):
    """Pack a fixed left-to-right order using exact diameter clearances."""
    before: dict[int, list[int]] = {}
    done: list[int] = []
    for j in order:
        before[j] = [i for i in done if _track_overlap(group[i], group[j])]
        done.append(j)

    earliest: dict[int, float] = {}
    for j in order:
        t = group[j]
        x = t.allowed[0]
        for i in before[j]:
            x = max(x, earliest[i] + (group[i].diameter + t.diameter) / 2)
        if x > t.allowed[1] + _EPS:
            return None
        earliest[j] = x

    latest: dict[int, float] = {}
    for i in reversed(order):
        t = group[i]
        x = t.allowed[1]
        for j in order:
            if j in latest and i in before[j]:
                x = min(x, latest[j] - (t.diameter + group[j].diameter) / 2)
        if x < t.allowed[0] - _EPS:
            return None
        latest[i] = x

    placed: dict[int, float] = {}
    for j in order:
        t = group[j]
        lo = earliest[j]
        for i in before[j]:
            lo = max(lo, placed[i] + (group[i].diameter + t.diameter) / 2)
        hi = latest[j]
        if lo > hi + _EPS:
            return None
        placed[j] = min(max(float(preferred[t.id]), lo), hi)
    return placed


def _pack_group(group: list[_Track], preferred: Mapping[int, float]):
    n = len(group)
    base = sorted(range(n), key=lambda i: (preferred[group[i].id], group[i].guide, group[i].id))
    guide_order = sorted(range(n), key=lambda i: (group[i].guide, preferred[group[i].id], group[i].id))
    compact_order = _order_positions(group)[0] if n <= 7 else base
    orders = [
        base, guide_order, compact_order,
        list(reversed(base)), list(reversed(guide_order)), list(reversed(compact_order)),
    ]
    if n <= 7:
        orders.extend(permutations(range(n)))

    guide_groups = defaultdict(list)
    for i, t in enumerate(group):
        guide_groups[round(t.guide, 8)].append(i)
    lengths = [sum(b - a for a, b in t.intervals) for t in group]
    best, seen = None, set()

    def score(placed, order):
        center_distances = []
        for guide, ids in guide_groups.items():
            if _max_layers([group[i].intervals for i in ids]) % 2:
                center_distances.append(min(abs(placed[i] - guide) for i in ids))
        movement = sum(lengths[i] * abs(placed[i] - preferred[group[i].id]) for i in range(n))
        return (
            sum(d > _EPS for d in center_distances),
            sum(center_distances),
            sum(abs(placed[i] - group[i].guide) for i in range(n) if group[i].background),
            movement,
            max(abs(placed[i] - preferred[group[i].id]) for i in range(n)),
            max(placed.values()) - min(placed.values()) if n > 1 else 0.0,
            tuple(group[i].id for i in order),
        )

    for raw in orders:
        order = tuple(raw)
        if order in seen:
            continue
        seen.add(order)
        for placed in (_pack_order(group, order, preferred), _compact_pack(group, order, preferred)):
            if placed is None:
                continue
            z = score(placed, order)
            if best is None or z < best[0]:
                best = z, placed
    return None if best is None else best[1]


def _spacing_violations(tracks: Sequence[_Track]) -> list[dict[str, Any]]:
    out = []
    for i, a in enumerate(tracks):
        if a.x is None:
            continue
        for b in tracks[i + 1:]:
            if b.x is None or not _track_overlap(a, b):
                continue
            actual = abs(a.x - b.x)
            required = (a.diameter + b.diameter) / 2
            if actual < required - _EPS:
                out.append({
                    "type": "bar_clearance",
                    "component": a.component,
                    "track_a": a.id,
                    "track_b": b.id,
                    "actual": float(actual),
                    "required": float(required),
                    "overlap_y": float(_overlap(a.intervals, b.intervals)),
                })
    return out


def _clearance_stats(tracks: Sequence[_Track]) -> dict[str, Any]:
    rows = []
    for i, a in enumerate(tracks):
        if a.x is None:
            continue
        for b in tracks[i + 1:]:
            if b.x is None or not _track_overlap(a, b):
                continue
            actual = abs(a.x - b.x)
            required = (a.diameter + b.diameter) / 2
            rows.append((actual, actual - required))
    return {
        "checked_clearance_pairs": len(rows),
        "minimum_center_distance": None if not rows else float(min(x[0] for x in rows)),
        "minimum_surface_gap": None if not rows else float(min(x[1] for x in rows)),
    }


def _track_adjacency(tracks: Sequence[_Track]) -> list[set[int]]:
    """Overlap graph for longitudinal track intervals, built by a sweep."""
    from heapq import heappop, heappush

    adjacency = [set() for _ in tracks]
    spans = [
        (min(a for a, _ in track.intervals), max(b for _, b in track.intervals), i)
        for i, track in enumerate(tracks)
        if track.intervals
    ]
    spans.sort()
    heap: list[tuple[float, int]] = []
    active: set[int] = set()

    for start, end, i in spans:
        while heap and heap[0][0] <= start + _EPS:
            _, j = heappop(heap)
            active.discard(j)
        for j in active:
            if _track_overlap(tracks[i], tracks[j]):
                adjacency[i].add(j)
                adjacency[j].add(i)
        active.add(i)
        heappush(heap, (end, i))
    return adjacency


def _tracks_conflict(left: _Track, right: _Track) -> bool:
    if left.x is None or right.x is None:
        return False
    required = (left.diameter + right.diameter) / 2
    return abs(left.x - right.x) < required - _EPS


def _nearest_free_position(
    index: int,
    tracks: Sequence[_Track],
    adjacency: Sequence[set[int]],
    target: float,
) -> float | None:
    """Nearest point in a track's bounds outside all neighbour clearances."""
    track = tracks[index]
    lo, hi = map(float, track.allowed)
    if lo > hi + _EPS:
        return None

    forbidden: list[tuple[float, float]] = []
    for other_index in adjacency[index]:
        other = tracks[other_index]
        if other.x is None:
            continue
        clearance = (track.diameter + other.diameter) / 2
        a = max(lo, float(other.x) - clearance)
        b = min(hi, float(other.x) + clearance)
        if b >= a - _EPS:
            forbidden.append((a, b))

    forbidden.sort()
    merged: list[list[float]] = []
    for a, b in forbidden:
        # Touching endpoints are feasible, therefore only overlapping interiors merge.
        if merged and a < merged[-1][1] - _EPS:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])

    target = min(max(float(target), lo), hi)
    candidates = [target, lo, hi]
    for a, b in merged:
        candidates.extend((a, b))

    def free(x: float) -> bool:
        # ``merged`` contains open forbidden interiors: exact clearance at an
        # endpoint is valid.  A linear scan is O(degree), performed only for
        # candidate boundaries; unlike the previous implementation it does not
        # rescan every neighbour for every candidate.
        return not any(a + _EPS < x < b - _EPS for a, b in merged)

    valid = [
        min(max(float(x), lo), hi)
        for x in candidates
        if lo - _EPS <= float(x) <= hi + _EPS and free(min(max(float(x), lo), hi))
    ]
    if not valid:
        return None
    # Clipping a forbidden interval to ``allowed`` can turn its outer endpoint
    # into a truly forbidden point (for example two tracks both at ``hi``).
    # Therefore inspect candidates in preference order and perform one exact
    # neighbour check for each until a genuinely free point is found.
    for x in sorted(set(valid), key=lambda value: (abs(value - target), abs(value - track.guide), value)):
        if all(
            other.x is None
            or abs(x - float(other.x)) >= (track.diameter + other.diameter) / 2 - _EPS
            for other in (tracks[j] for j in adjacency[index])
        ):
            return float(x)
    return None




def _push_collision_chain(
    seed: tuple[int, int],
    tracks: list[_Track],
    adjacency: Sequence[set[int]],
    preferred: Mapping[int, float],
) -> set[int] | None:
    """Resolve a blocked pair by pushing a short local chain left or right."""
    i, j = seed
    original = [track.x for track in tracks]

    attempts = []
    for moving, obstacle in ((i, j), (j, i)):
        if tracks[moving].background:
            continue
        if tracks[obstacle].background:
            directions = (1, -1) if float(tracks[moving].x) >= float(tracks[obstacle].x) else (-1, 1)
        else:
            directions = (1, -1) if float(preferred[tracks[moving].id]) >= float(preferred[tracks[obstacle].id]) else (-1, 1)
        for direction in directions:
            attempts.append((moving, obstacle, direction))

    for moving, obstacle, direction in attempts:
        for track, value in zip(tracks, original):
            track.x = value
        changed: set[int] = set()
        visiting: set[int] = set()

        def transformed_bounds(track: _Track) -> tuple[float, float]:
            lo, hi = map(float, track.allowed)
            return (lo, hi) if direction > 0 else (-hi, -lo)

        def place(index: int, target: float) -> bool:
            track = tracks[index]
            if track.background or index in visiting:
                return False
            visiting.add(index)
            lo, hi = transformed_bounds(track)
            x = max(float(target), lo)

            while True:
                if x > hi + _EPS:
                    visiting.remove(index)
                    return False
                restart = False
                ahead = []
                for other_index in adjacency[index]:
                    other = tracks[other_index]
                    if other.x is None:
                        continue
                    ox = direction * float(other.x)
                    clearance = (track.diameter + other.diameter) / 2
                    if abs(x - ox) >= clearance - _EPS:
                        continue
                    if ox <= x + _EPS or other.background:
                        x = ox + clearance
                        restart = True
                        break
                    ahead.append((ox, other_index, clearance))
                if restart:
                    continue
                track.x = direction * x
                changed.add(index)
                for _, other_index, clearance in sorted(ahead):
                    other = tracks[other_index]
                    if _tracks_conflict(track, other):
                        if not place(other_index, x + clearance):
                            visiting.remove(index)
                            return False
                if any(
                    tracks[q].x is not None and _tracks_conflict(track, tracks[q])
                    for q in adjacency[index]
                ):
                    # A recursively moved neighbour may force this track farther.
                    blockers = [
                        q for q in adjacency[index]
                        if tracks[q].x is not None and _tracks_conflict(track, tracks[q])
                    ]
                    if not blockers:
                        visiting.remove(index)
                        return True
                    x = max(
                        direction * float(tracks[q].x)
                        + (track.diameter + tracks[q].diameter) / 2
                        for q in blockers
                    )
                    continue
                visiting.remove(index)
                return True

        obstacle_x = direction * float(tracks[obstacle].x)
        clearance = (tracks[moving].diameter + tracks[obstacle].diameter) / 2
        if place(moving, obstacle_x + clearance) and not any(
            _tracks_conflict(tracks[k], tracks[q])
            for k in changed
            for q in adjacency[k]
            if tracks[q].x is not None
        ):
            return changed

    for track, value in zip(tracks, original):
        track.x = value
    return None

def _repair_local_cluster(
    seed: tuple[int, int],
    tracks: list[_Track],
    adjacency: Sequence[set[int]],
    preferred: Mapping[int, float],
) -> set[int] | None:
    """Repack a small cross-coordinate neighbourhood around a blocked pair."""
    i, j = seed
    component = tracks[i].component
    center = (float(tracks[i].x) + float(tracks[j].x)) / 2
    max_diameter = max((t.diameter for t in tracks if t.component == component), default=32.0)
    fixed = [t.guide for t in tracks if t.component == component and t.background]
    gaps = sorted(b - a for a, b in zip(sorted(fixed), sorted(fixed)[1:]) if b > a + _EPS)
    base_radius = max(8 * max_diameter, gaps[len(gaps) // 2] if gaps else 300.0)
    original = [t.x for t in tracks]

    for radius in (base_radius, 2 * base_radius, 4 * base_radius):
        cluster = {
            k
            for k, track in enumerate(tracks)
            if not track.background
            and track.component == component
            and track.x is not None
            and center - radius - _EPS <= float(track.x) <= center + radius + _EPS
            and (k in {i, j} or k in adjacency[i] or k in adjacency[j])
        }
        cluster.update((i, j))
        cluster = {k for k in cluster if not tracks[k].background}
        if not cluster:
            continue

        lengths = {k: sum(b - a for a, b in tracks[k].intervals) for k in cluster}
        common = lambda k: (-lengths[k], -len(adjacency[k]), -tracks[k].diameter,
                            abs(float(preferred[tracks[k].id]) - center), k)
        rest = [k for k in cluster if k not in {i, j}]
        orders = [
            [i, j] + sorted(rest, key=common),
            sorted(cluster, key=common),
            sorted(cluster, key=lambda k: (-len(adjacency[k]), -lengths[k],
                                           abs(float(preferred[tracks[k].id]) - center), k)),
            sorted(cluster, key=lambda k: (float(preferred[tracks[k].id]), common(k))),
            list(reversed(sorted(cluster, key=lambda k: (float(preferred[tracks[k].id]), common(k))))),
        ]

        seen_orders = set()
        for raw_order in orders:
            order = tuple(raw_order)
            if order in seen_orders:
                continue
            seen_orders.add(order)
            for k in cluster:
                tracks[k].x = None
            failed = False
            for k in order:
                x = _nearest_free_position(k, tracks, adjacency, preferred[tracks[k].id])
                if x is None:
                    failed = True
                    break
                tracks[k].x = x
            if not failed and not any(
                _tracks_conflict(tracks[k], tracks[q])
                for k in cluster
                for q in adjacency[k]
                if tracks[q].x is not None
            ):
                return cluster
            for k, value in enumerate(original):
                tracks[k].x = value

    for k, value in enumerate(original):
        tracks[k].x = value
    return None

def _separate(tracks: list[_Track]) -> list[dict[str, Any]]:
    """Place tracks near their guides and repair only actual local collisions.

    The previous implementation tried to impose one global left-to-right order on
    every transitively connected track.  Background tracks connect almost the
    whole field, so a locally feasible layout could be rejected as one enormous
    packing problem.  Here all preferred positions are assigned first and only
    the genuinely colliding tracks are moved.
    """
    preferred = _guide_preferences(tracks)
    adjacency = _track_adjacency(tracks)

    for track in tracks:
        if track.background:
            track.x = float(track.guide)
        else:
            lo, hi = map(float, track.allowed)
            track.x = min(max(float(preferred[track.id]), lo), hi)

    violations = {
        (i, j)
        for i, neighbours in enumerate(adjacency)
        for j in neighbours
        if j > i and _tracks_conflict(tracks[i], tracks[j])
    }

    moved = 0
    while violations and moved < 2 * len(tracks):
        i, j = min(violations)
        options = []
        for k in (i, j):
            track = tracks[k]
            if track.background:
                continue
            old = track.x
            track.x = None
            x = _nearest_free_position(k, tracks, adjacency, preferred[track.id])
            track.x = old
            if x is not None:
                options.append((abs(x - preferred[track.id]), abs(x - float(old)), len(adjacency[k]), k, x))

        if not options:
            cluster = _push_collision_chain((i, j), tracks, adjacency, preferred)
            if cluster is None:
                cluster = _repair_local_cluster((i, j), tracks, adjacency, preferred)
            if cluster is None:
                return [{
                    "type": "bar_packing_infeasible",
                    "component": tracks[i].component,
                    "tracks": [i, j],
                    "allowed_x": [
                        max(tracks[i].allowed[0], tracks[j].allowed[0]),
                        min(tracks[i].allowed[1], tracks[j].allowed[1]),
                    ],
                    "required_diameter_sum": float(tracks[i].diameter + tracks[j].diameter),
                    "reason": "local_cluster_repair_failed",
                }]
            moved += len(cluster)
            for k in cluster:
                for other in adjacency[k]:
                    violations.discard((min(k, other), max(k, other)))
            continue

        *_, k, x = min(options)
        tracks[k].x = float(x)
        moved += 1
        for other in adjacency[k]:
            violations.discard((min(k, other), max(k, other)))

    errors = [] if not violations else [{
        "type": "bar_packing_infeasible",
        "component": tracks[next(iter(violations))[0]].component,
        "tracks": sorted({i for pair in violations for i in pair}),
        "reason": "local_repair_iteration_limit",
    }]
    errors.extend(_spacing_violations(tracks))
    return errors


def _parts(g: Any) -> list[dict[str, Any]]:
    return [{
        "exterior": [[float(x), float(y)] for x, y in p.exterior.coords],
        "holes": [[[float(x), float(y)] for x, y in ring.coords] for ring in p.interiors],
    } for p in _polygon_geoms(g)]


def _multiple_bounds(xs: Sequence[float], y0: float, y1: float, step: float, preferred: tuple[float, float], limits: tuple[float, float]):
    a, b = min(xs), max(xs)
    width = max(step, ceil(max(0.0, b - a) / step - 1e-12) * step)
    lo, hi = max(limits[0], b - width), min(a, limits[1] - width)
    if lo > hi + _EPS:
        return (a, y0, b, y1), False
    target = (preferred[0] + preferred[1] - width) / 2
    x0 = min(max(target, lo), hi)
    return (x0, y0, x0 + width, y1), True


def layout_rebars_y(
    polygons: Sequence[Any],
    boxes: Any,
    background: tuple[float, float] = (18, 300),
    *,
    diameters: Mapping[Any, float] | None = None,
    steps: Mapping[Any, float] | None = None,
    recipes: Mapping[Any, Sequence[Any]] | None = None,
    min_step: float = 100.0,
) -> dict[str, Any]:
    """Heuristic vertical-bar layout over fitted reinforcement boxes; coordinates are unchanged."""
    bg_d, bg_step = map(float, background)
    if bg_d <= 0 or bg_step <= 0 or min_step <= 0:
        raise ValueError("background и min_step должны быть положительными")
    source_geoms, components = _polygons(polygons)
    zones = _zones(boxes, diameters, steps, recipes)
    field_geom = unary_union(components)
    warnings: list[dict[str, Any]] = []
    tracks: list[_Track] = []
    component_rows, fragments, guide_rows = [], [], []
    background_zone_ids = []

    def add_track(zone, component, guide, diameter, step, intervals, allowed, background=False, ordinal=0):
        t = _Track(len(tracks), zone, component, float(guide), float(diameter), float(step), _merge(intervals), tuple(map(float, allowed)), background, ordinal)
        tracks.append(t)
        return t.id

    # Background and strip/region construction.
    for ci, comp in enumerate(components):
        xmin, ymin, xmax, ymax = map(float, comp.bounds)
        positions = _background_positions(comp, bg_step)
        bg_track_ids = [add_track(None, ci, x, bg_d, bg_step, _segments(comp, x), (x, x), True) for x in positions]
        background_zone_ids.append(bg_track_ids)
        edges = sorted({xmin, xmax, *positions})
        strips = [(a, b) for a, b in zip(edges, edges[1:]) if b > a + _EPS]
        component_rows.append({"id": ci, "bounds": (xmin, ymin, xmax, ymax), "area": float(comp.area), "holes": len(comp.interiors), "background_positions": positions, "strips": strips})

        for si, (left, right) in enumerate(strips):
            strip_geom = box(left, ymin - 1, right, ymax + 1)
            entries = []
            for z in zones:
                part = comp.intersection(z.geometry).intersection(strip_geom)
                if part.is_empty or part.area <= _EPS:
                    continue
                intervals = _projection(part)
                if intervals:
                    entries.append((z, intervals))
            regions = _merge([q for _, ints in entries for q in ints])
            for ri, (ry0, ry1) in enumerate(regions):
                active = [(z, _clip_interval(ints, ry0, ry1)) for z, ints in entries if _clip_interval(ints, ry0, ry1)]
                endpoints = sorted({ry0, ry1, *(y for _, ints in active for q in ints for y in q)})
                max_count = 1
                for a, b in zip(endpoints, endpoints[1:]):
                    if b <= a + _EPS:
                        continue
                    y = (a + b) / 2
                    count = 1 + sum(max(1, ceil((min(right, z.bounds[2]) - max(left, z.bounds[0])) / z.step - 1e-12)) for z, ints in active if any(u - _EPS <= y <= v + _EPS for u, v in ints))
                    max_count = max(max_count, count)
                quantum = max(float(min_step), bg_step / max_count)
                slots = _guide_slots(left, right, quantum)
                region_frags = []
                for z, ints in active:
                    req = _required_x(z, source_geoms, comp, left, right, ry0, ry1)
                    a, b, raw0, raw1 = _snap(z, left, right, slots, req)
                    region_frags.append({
                        "component": ci, "strip_id": si, "region_id": ri, "left": left, "right": right,
                        "y0": ry0, "y1": ry1, "zone": z.index, "intervals": ints, "slots": slots,
                        "a": a, "b": b, "raw0": raw0, "raw1": raw1, "required": req,
                        "demand": max(1, ceil((raw1 - raw0) / z.step - 1e-12)),
                        "intensity": z.diameter * z.diameter / z.step,
                    })
                _shift_weaker(region_frags)
                fragments += region_frags
                guide_rows += [{
                    "component_id": ci, "strip_id": si, "region_id": ri, "strip": (left, right),
                    "x": x, "y0": ry0, "y1": ry1, "internal_step": quantum,
                    "max_required_count": max_count, "allocatable": x > left + _EPS,
                } for x in [left, *slots]]

    _boundary_conflicts(fragments)

    # Allocate dense zones first, then use sparse zones to fill the least loaded guides.
    occupancy: dict[tuple[int, float], list[int]] = defaultdict(list)
    for t in tracks:
        occupancy[(t.component, round(t.guide, 8))].append(t.id)
    for f in sorted(fragments, key=lambda q: (zones[q["zone"]].step, -q["intensity"], -q["demand"], q["zone"])):
        z = zones[f["zone"]]
        candidates = [x for x in f["slots"] if f["a"] + _EPS < x <= f["b"] + _EPS] or [min(f["slots"], key=lambda x: abs(x - (f["raw0"] + f["raw1"]) / 2))]
        ideals = [f["raw1"] - k * z.step for k in range(f["demand"])]
        local = Counter()
        for ideal in ideals:
            def key(x):
                old = [tracks[i] for i in occupancy[(f["component"], round(x, 8))]]
                overlaps = [t for t in old if _overlap(t.intervals, f["intervals"]) > _EPS]
                return (
                    _max_layers([t.intervals for t in overlaps] + [f["intervals"]]),
                    sum(_overlap(t.intervals, f["intervals"]) for t in overlaps),
                    len(overlaps), local[x], abs(x - ideal), -x,
                )
            x = min(candidates, key=key)
            ordinal = local[x]
            local[x] += 1
            cb = components[f["component"]].bounds
            zb = z.geometry.bounds
            lo, hi = max(float(cb[0]), float(zb[0])), min(float(cb[2]), float(zb[2]))
            inset = min(max(10 * _EPS, 1e-6), max(0.0, (hi - lo) / 4))
            allowed = (lo + inset, hi - inset) if hi - lo > 2 * inset else (lo, hi)
            tid = add_track(z.index, f["component"], x, z.diameter, z.step, f["intervals"], allowed, False, ordinal)
            occupancy[(f["component"], round(x, 8))].append(tid)
            z.track_ids.append(tid)
            z.components.add(f["component"])

    errors = _separate(tracks)
    if errors:
        public_tracks = [{
            "id": t.id, "zone_id": t.component if t.background else len(components) + int(t.zone),
            "input_zone_index": t.zone, "component_id": t.component, "background": t.background,
            "guide_x": t.guide, "x": None if t.x is None else float(t.x),
            "diameter": t.diameter, "step": t.step, "intervals": t.intervals,
            "allowed_x": t.allowed, "ordinal": t.ordinal,
        } for t in tracks]
        return {
            "status": "Infeasible", "is_feasible": False, "axis": "y",
            "background": {"diameter": bg_d, "step": bg_step},
            "components": component_rows, "zones": [], "tracks": public_tracks,
            "bars": [], "guides": guide_rows, "warnings": warnings, "errors": errors,
            "stats": {
                "components": len(components), "input_zones": len(zones), "zones": 0,
                "tracks": len(tracks), "bar_segments": 0, "max_stack": 0,
                "hard_conflicts": len(errors),
                "packing_errors": sum(e.get("type") == "bar_packing_infeasible" for e in errors),
                "spacing_violations": sum(e.get("type") == "bar_clearance" for e in errors),
                **_clearance_stats(tracks),
            },
        }

    # Clip every fixed-x track by its actual component/zone geometry.
    bar_rows = []
    for t in tracks:
        comp = components[t.component]
        geom = comp
        segs = _segments(geom, float(t.x), t.intervals)
        if not segs:
            warnings.append({"type": "empty_track", "track": t.id, "guide": t.guide, "x": t.x})
        for y0, y1 in segs:
            row = {
                "id": len(bar_rows), "track_id": t.id,
                "zone_id": t.component if t.background else len(components) + int(t.zone),
                "input_zone_index": t.zone, "component_id": t.component,
                "background": t.background, "diameter": t.diameter, "step": t.step,
                "guide_x": t.guide, "x0": float(t.x), "y0": y0, "x1": float(t.x), "y1": y1,
            }
            bar_rows.append(row)
            if not t.background:
                zones[t.zone].bars.append((row["x0"], y0, row["x1"], y1))

    # Fill guide diagnostics using maximum simultaneous occupancy, not just total tracks.
    for g in guide_rows:
        ids = [t.id for t in tracks if t.component == g["component_id"] and abs(t.guide - g["x"]) <= _EPS and _overlap(t.intervals, [(g["y0"], g["y1"])]) > _EPS]
        g["track_ids"] = ids
        g["tracks_total"] = len(ids)
        g["track_count"] = _max_layers([tracks[i].intervals for i in ids])
        g["actual_xs"] = sorted({float(tracks[i].x) for i in ids})

    out_zones = []
    for ci, comp in enumerate(components):
        tids = background_zone_ids[ci]
        rows = [b for b in bar_rows if b["track_id"] in tids]
        bars = [(b["x0"], b["y0"], b["x1"], b["y1"]) for b in rows]
        out_zones.append({
            "id": len(out_zones), "input_id": None, "source_index": None, "component_id": ci,
            "component_ids": [ci], "class": None, "background": True, "diameter": bg_d,
            "step": bg_step, "bounds": tuple(map(float, comp.bounds)), "nominal_positions": component_rows[ci]["background_positions"],
            "track_ids": tids, "bars": bars, "parts": _parts(comp),
        })

    for z in zones:
        rows = [b for b in bar_rows if b["input_zone_index"] == z.index and not b["background"]]
        if rows:
            xs, y0, y1 = [b["x0"] for b in rows], min(b["y0"] for b in rows), max(b["y1"] for b in rows)
            touched = sorted(z.components)
            limits = (min(components[i].bounds[0] for i in touched), max(components[i].bounds[2] for i in touched))
            assigned = [source_geoms[i] for i in z.assigned if 0 <= i < len(source_geoms)]
            if assigned:
                required = unary_union(assigned).intersection(field_geom)
                if not required.is_empty:
                    xs += [required.bounds[0], required.bounds[2]]
            bounds, multiple = _multiple_bounds(xs, y0, y1, z.step, (z.bounds[0], z.bounds[2]), limits)
            if not multiple:
                warnings.append({"type": "non_multiple_width", "zone": z.input_id, "actual_width": bounds[2] - bounds[0], "step": z.step})
            parts = _parts(field_geom.intersection(box(*bounds)))
        else:
            bounds, parts, touched = z.bounds, [], sorted(z.components)
            warnings.append({"type": "zone_without_bars", "zone": z.input_id})
        out_zones.append({
            "id": len(out_zones), "input_id": z.input_id, "source_index": z.source_index,
            "component_id": touched[0] if len(touched) == 1 else None, "component_ids": touched,
            "class": z.cls, "parent_class": z.parent_class, "layer_index": z.layer_index,
            "background": False, "diameter": z.diameter, "step": z.step,
            "primary_bounds": z.bounds, "bounds": bounds, "track_ids": z.track_ids,
            "bars": z.bars, "parts": parts, "assigned_polygons": list(z.assigned),
        })

    public_tracks = [{
        "id": t.id, "zone_id": t.component if t.background else len(components) + int(t.zone),
        "input_zone_index": t.zone, "component_id": t.component, "background": t.background,
        "guide_x": t.guide, "x": float(t.x), "diameter": t.diameter, "step": t.step,
        "intervals": t.intervals, "allowed_x": t.allowed, "ordinal": t.ordinal,
    } for t in tracks]
    hard = sum(w.get("type") in {"stack_overlap_relaxed", "empty_track", "zone_without_bars"} for w in warnings)
    return {
        "status": "Feasible" if not hard else "Partial", "is_feasible": not hard, "axis": "y",
        "background": {"diameter": bg_d, "step": bg_step},
        "components": component_rows, "zones": out_zones, "tracks": public_tracks,
        "bars": bar_rows, "guides": guide_rows, "warnings": warnings, "errors": [],
        "stats": {
            "components": len(components), "input_zones": len(zones), "zones": len(out_zones),
            "tracks": len(tracks), "bar_segments": len(bar_rows),
            "max_stack": max((g["track_count"] for g in guide_rows), default=1),
            "hard_conflicts": hard, "packing_errors": 0, "spacing_violations": 0,
            **_clearance_stats(tracks),
        },
    }


def layout_rebars(
    polygons: Sequence[Any],
    boxes: Any,
    background: tuple[float, float] = (18, 300),
    *,
    axis: str = "y",
    diameters: Mapping[Any, float] | None = None,
    steps: Mapping[Any, float] | None = None,
    recipes: Mapping[Any, Sequence[Any]] | None = None,
    min_step: float = 100.0,
) -> dict[str, Any]:
    """Axis-neutral wrapper around :func:`layout_rebars_y`.

    World ``axis='x'`` is converted by swapping X/Y, solved by the native
    vertical implementation, then restored. No coordinate origin is lost.
    """

    from .axis_orientation import (
        normalize_axis,
        orient_polygon_items,
        orient_rectangles,
        restore_bar_layout,
    )

    axis = normalize_axis(axis)
    if axis == "y":
        return layout_rebars_y(
            polygons,
            boxes,
            background,
            diameters=diameters,
            steps=steps,
            recipes=recipes,
            min_step=min_step,
        )

    work = layout_rebars_y(
        orient_polygon_items(polygons, "x"),
        orient_rectangles(boxes, "x"),
        background,
        diameters=diameters,
        steps=steps,
        recipes=recipes,
        min_step=min_step,
    )
    return restore_bar_layout(work, "x")
