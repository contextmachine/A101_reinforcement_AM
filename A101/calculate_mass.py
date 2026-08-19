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
):
    axis = axis.lower()
    if axis not in ("x", "y"):
        raise ValueError("axis должен быть 'x' или 'y'")

    divisors = divisors or {}
    diameters = diameters or {}

    def rect4(r):
        try:
            return tuple(map(float, r[:4]))
        except Exception:
            return None

    # Валидные исходные прямоугольники.
    primary = []
    if rec_opt is not None:
        try:
            primary = [rect4(r) for r in rec_opt]
        except Exception:
            primary = []

    # Проверяем, есть ли вообще результат fit.
    try:
        fit_zones = fit_result["zones"]
        fit_rects = fit_result.get("rectangles") or []
        fit_ok = bool(fit_zones)
    except Exception:
        fit_zones, fit_rects, fit_ok = [], [], False

    # fit отсутствует / Infeasible:
    # сохраняем только исходные прямоугольники.
    if not fit_ok:
        zones = [
            {
                "primary rectangle": r,
                "final rectangle": None,
                "width": None,
                "length": None,
                "diameter": None,
                "step": None,
                "bars count": None,
                "zone mass": None,
                "bars": [],
            }
            for r in primary
            if r is not None
        ]

        return {
            "N": len(zones),
            "mass": None,
            "zones": zones,
        }

    def bar4(b):
        try:
            if isinstance(b, dict):
                return tuple(float(b[k]) for k in ("x0", "y0", "x1", "y1"))
            return tuple(map(float, b[:4]))
        except Exception:
            return None

    zones = []

    for i, z in enumerate(fit_zones):
        try:
            cls = int(z["class"])
        except Exception:
            cls = None

        # final rectangle
        final = rect4(z.get("bounds"))
        if final is None and i < len(fit_rects):
            final = rect4(fit_rects[i])

        # primary rectangle
        try:
            src = z.get("source_index", i)
            prim = (
                primary[src]
                if src is not None and 0 <= src < len(primary)
                else None
            )
        except Exception:
            prim = None

        # bars
        try:
            bars = [x for b in z.get("bars", []) if (x := bar4(b)) is not None]
        except Exception:
            bars = []

        width = length = None
        if final is not None:
            x0, y0, x1, y1 = final
            if axis == "y":
                width, length = x1 - x0, y1 - y0
            else:
                width, length = y1 - y0, x1 - x0

        d = diameters.get(cls)
        step = divisors.get(cls)

        zone_mass = None
        if d is not None and bars:
            area = pi * (float(d) / 1000) ** 2 / 4
            total_length = sum(
                hypot(x1 - x0, y1 - y0) / 1000
                for x0, y0, x1, y1 in bars
            )
            zone_mass = float(density) * area * total_length

        zones.append({
            "primary rectangle": prim,
            "final rectangle": final,
            "width": width,
            "length": length,
            "diameter": None if d is None else float(d),
            "step": None if step is None else float(step),
            "bars count": len(bars),
            "zone mass": zone_mass,
            "bars": bars,
        })

    masses = [z["zone mass"] for z in zones if z["zone mass"] is not None]

    return {
        "N": len(zones),
        "mass": sum(masses) if len(masses) == len(zones) else None,
        "zones": zones,
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
