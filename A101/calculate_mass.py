from math import pi, hypot
from itertools import combinations_with_replacement
from bisect import bisect_right

def comb_indices(length, n):
    return list(combinations_with_replacement(range(length), n))

def rebar_summary(
    rec_opt=None,
    fit_result=None,
    divisors=None,
    diameters=None,
    density=7850,
    axis="y",
    holds=None,
    polygons=None,
):
    """Сводка по стержням. Все координаты, диаметры и анкеровка — мм."""

    axis = str(axis).lower()
    if axis not in ("x", "y"):
        raise ValueError("axis должен быть 'x' или 'y'")

    divisors, diameters, holds = divisors or {}, diameters or {}, holds or {}
    warnings = []

    def rect4(r):
        try:
            return tuple(map(float, r[:4]))
        except Exception:
            return None

    def bar4(b):
        try:
            if isinstance(b, dict):
                return tuple(float(b[k]) for k in ("x0", "y0", "x1", "y1"))
            return tuple(map(float, b[:4]))
        except Exception:
            return None

    def polygon_union(items):
        if not items:
            return None
        try:
            from shapely.geometry import Polygon, shape
            from shapely.ops import unary_union

            geoms = []
            for item in items:
                if isinstance(item, dict) and "geometry" in item:
                    item = item["geometry"]
                elif isinstance(item, (tuple, list)) and len(item) == 2 and isinstance(item[1], (int, float)):
                    item = item[0]
                if hasattr(item, "geom_type"):
                    g = item
                elif isinstance(item, dict):
                    g = shape(item)
                else:
                    g = Polygon(item)
                if not g.is_valid:
                    g = g.buffer(0)
                if not g.is_empty:
                    geoms.append(g)
            return unary_union(geoms) if geoms else None
        except Exception as e:
            warnings.append(f"Не удалось построить объединение полигонов: {e}")
            return None

    field = polygon_union(polygons)

    def intervals(g, coord):
        out = []
        if g is None or g.is_empty:
            return out
        if g.geom_type in {"LineString", "LinearRing"}:
            vals = [coord(x, y) for x, y, *_ in g.coords]
            if vals:
                out.append((min(vals), max(vals)))
        elif hasattr(g, "geoms"):
            for q in g.geoms:
                out.extend(intervals(q, coord))
        return out

    def extend_bar(bar, hold):
        x0, y0, x1, y1 = bar
        if hold <= 0:
            return bar
        if axis == "y":
            x, a, b = (x0 + x1) / 2, min(y0, y1), max(y0, y1)
            candidate = (x, a - hold, x, b + hold)
            coord = lambda _x, y: y
        else:
            y, a, b = (y0 + y1) / 2, min(x0, x1), max(x0, x1)
            candidate = (a - hold, y, b + hold, y)
            coord = lambda x, _y: x
        if field is None:
            return candidate

        try:
            from shapely.geometry import LineString

            line = LineString([(candidate[0], candidate[1]), (candidate[2], candidate[3])])
            raw = sorted(intervals(line.intersection(field), coord))
            merged = []
            for u, v in raw:
                if merged and u <= merged[-1][1] + 1e-7:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], v))
                else:
                    merged.append((u, v))
            if not merged:
                return bar
            u, v = max(
                merged,
                key=lambda z: (
                    z[0] <= a + 1e-7 and z[1] >= b - 1e-7,
                    max(0.0, min(z[1], b) - max(z[0], a)),
                    z[1] - z[0],
                ),
            )
            return (x, u, x, v) if axis == "y" else (u, y, v, y)
        except Exception as e:
            warnings.append(f"Не удалось обрезать стержень по полигонам: {e}")
            return candidate

    def extend_rectangle(rect, hold):
        if rect is None or hold <= 0:
            return rect
        x0, y0, x1, y1 = rect
        if field is None:
            return (x0, y0 - hold, x1, y1 + hold) if axis == "y" else (x0 - hold, y0, x1 + hold, y1)

        try:
            from shapely.geometry import box

            safe = field.buffer(1e-7)
            if not safe.covers(box(*rect)):
                warnings.append("Исходный final rectangle не целиком лежит в объединении полигонов")
                return rect

            def covered(a, b):
                q = (x0, a, x1, b) if axis == "y" else (a, y0, b, y1)
                return safe.covers(box(*q))

            a, b = (y0, y1) if axis == "y" else (x0, x1)
            f0, f1, f2, f3 = field.bounds
            lo_bound, hi_bound = (f1, f3) if axis == "y" else (f0, f2)
            target_lo, target_hi = max(lo_bound, a - hold), min(hi_bound, b + hold)

            if covered(target_lo, b):
                lo = target_lo
            else:
                bad, good = target_lo, a
                for _ in range(55):
                    mid = (bad + good) / 2
                    if covered(mid, b):
                        good = mid
                    else:
                        bad = mid
                lo = good

            if covered(a, target_hi):
                hi = target_hi
            else:
                good, bad = b, target_hi
                for _ in range(55):
                    mid = (good + bad) / 2
                    if covered(a, mid):
                        good = mid
                    else:
                        bad = mid
                hi = good

            return (x0, lo, x1, hi) if axis == "y" else (lo, y0, hi, y1)
        except Exception as e:
            warnings.append(f"Не удалось построить rectangle с анкеровкой: {e}")
            return rect

    primary = []
    if rec_opt is not None:
        try:
            primary = [rect4(r) for r in rec_opt]
        except Exception:
            primary = []

    try:
        fit_zones = fit_result["zones"]
        fit_rects = fit_result.get("rectangles") or []
        fit_ok = bool(fit_zones)
    except Exception:
        fit_zones, fit_rects, fit_ok = [], [], False

    empty = {
        "final rectangle": None,
        "final rectangle with anchorage": None,
        "width": None,
        "length": None,
        "diameter": None,
        "step": None,
        "hold": None,
        "bars count": None,
        "zone mass": None,
        "zone mass without anchorage": None,
        "zone mass with anchorage": None,
        "anchorage mass": None,
        "bars": [],
        "bars with anchorage": [],
    }
    if not fit_ok:
        zones = [{"primary rectangle": r, **empty} for r in primary if r is not None]
        return {
            "N": len(zones),
            "mass": None,
            "mass without anchorage": None,
            "mass with anchorage": None,
            "anchorage mass": None,
            "zones": zones,
            "warnings": warnings,
        }

    zones = []
    for i, z in enumerate(fit_zones):
        try:
            cls = int(z["class"])
        except Exception:
            cls = None

        final = rect4(z.get("bounds"))
        if final is None and i < len(fit_rects):
            final = rect4(fit_rects[i])
        try:
            src = z.get("source_index", i)
            prim = primary[src] if src is not None and 0 <= src < len(primary) else None
        except Exception:
            prim = None
        try:
            bars = [q for b in z.get("bars", []) if (q := bar4(b)) is not None]
        except Exception:
            bars = []

        hold = max(0.0, float(holds.get(cls, 0.0))) if cls is not None else 0.0
        anchored_bars = [extend_bar(b, hold) for b in bars]
        anchored_final = extend_rectangle(final, hold)

        width = length = None
        if final is not None:
            x0, y0, x1, y1 = final
            width, length = (x1 - x0, y1 - y0) if axis == "y" else (y1 - y0, x1 - x0)

        d, step = diameters.get(cls), divisors.get(cls)
        base_mass = anchored_mass = None
        if d is not None and bars:
            area = pi * (float(d) / 1000) ** 2 / 4
            factor = float(density) * area / 1000
            base_mass = factor * sum(hypot(x1 - x0, y1 - y0) for x0, y0, x1, y1 in bars)
            anchored_mass = factor * sum(hypot(x1 - x0, y1 - y0) for x0, y0, x1, y1 in anchored_bars)

        zones.append({
            "primary rectangle": prim,
            "final rectangle": final,
            "final rectangle with anchorage": anchored_final,
            "width": width,
            "length": length,
            "diameter": None if d is None else float(d),
            "step": None if step is None else float(step),
            "hold": hold,
            "bars count": len(bars),
            "zone mass": base_mass,
            "zone mass without anchorage": base_mass,
            "zone mass with anchorage": anchored_mass,
            "anchorage mass": None if base_mass is None or anchored_mass is None else anchored_mass - base_mass,
            "bars": bars,
            "bars with anchorage": anchored_bars,
        })

    def total(key):
        values = [z[key] for z in zones]
        return sum(values) if values and all(v is not None for v in values) else None

    base_total, anchored_total = total("zone mass without anchorage"), total("zone mass with anchorage")
    return {
        "N": len(zones),
        "mass": base_total,
        "mass without anchorage": base_total,
        "mass with anchorage": anchored_total,
        "anchorage mass": None if base_total is None or anchored_total is None else anchored_total - base_total,
        "zones": zones,
        "warnings": warnings,
    }

def ds_arm(d, s):
    return round(10*(d/2)**2*pi/s, 1)

def make_rebar_classes(loads, back_grid, stock, max_lay=2):
    back = ds_arm(*back_grid)
    arm = [ds_arm(*x) for x in stock]

    # Все допустимые комбинации: (), (0,), (1,), ..., (0,0), (0,1), ...
    combs = [()] + [
        c
        for n in range(1, max_lay + 1)
        for c in combinations_with_replacement(range(len(stock)), n)
    ]

    # Полное армирование = фон + добавочные слои
    variants = sorted(
        ((back + sum(arm[i] for i in c), c) for c in combs),
        key=lambda x: (x[0], len(x[1]), x[1]),
    )
    values = [x[0] for x in variants]

    # Сохраняет старую логику np.searchsorted(..., side="right"):
    # выбираем первое значение СТРОГО больше load.
    selected = {}
    for load in sorted(set(loads)):
        j = bisect_right(values, load)
        if j == len(variants):
            raise ValueError(f"Недостаточно армирования для load={load}")
        selected[load] = variants[j][1]

    # Базовые сетки, реально используемые выбранными комбинациями
    used = {i for c in selected.values() for i in c}

    # Все необходимые классы:
    # фон + выбранные комбинации + их одиночные составляющие
    classes = {(), *selected.values(), *((i,) for i in used)}
    classes = sorted(
        classes,
        key=lambda c: (back + sum(arm[i] for i in c), len(c), c),
    )

    cls = {c: n for n, c in enumerate(classes)}
    base_cls = {c[0]: cls[c] for c in classes if len(c) == 1}

    recipes = {
        cls[c]: tuple(base_cls[i] for i in c)
        for c in classes
        if len(c) > 1
    }

    diameters = {
        base_cls[i]: stock[i][0]
        for i in base_cls
    }

    steps = {
        base_cls[i]: stock[i][1]
        for i in base_cls
    }

    densities = {
        base_cls[i]: arm[i]
        for i in base_cls
    }

    load2cls = {
        load: cls[c]
        for load, c in selected.items()
    }

    return {
        "load2cls": load2cls,
        "recipes": recipes,
        "densities": densities,
        "diameters": diameters,
        "steps": steps,
        "back_arm": back,
    }

def loads_to_classes(load_matrix, load2cls, *, atol=1e-8):
    """Map physical loads to integer classes; NaN/uncovered cells become 0."""

    import numpy as np

    values = np.asarray(load_matrix, dtype=float)
    out = np.zeros(values.shape, dtype=np.int64)
    finite = np.isfinite(values)
    matched = ~finite | np.isclose(values, 0.0, rtol=0.0, atol=atol)

    for load, cls in dict(load2cls).items():
        mask = finite & np.isclose(values, float(load), rtol=0.0, atol=atol)
        out[mask] = int(cls)
        matched |= mask

    unknown = finite & ~matched
    if np.any(unknown):
        sample = np.unique(values[unknown])[:10].tolist()
        raise ValueError(f"Неизвестные нагрузки в матрице: {sample}")
    return out
