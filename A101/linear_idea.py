import numpy as np
from itertools import accumulate
from functools import lru_cache

def consecutive_subsets(N):
    return [
        list(range(start, start + length))
        for length in range(1, N + 1)
        for start in range(N - length + 1)
    ]


def generate_groups(mask, weights, L):
    mask = np.asarray(mask, bool)
    runs, i, n = [], 0, len(mask)

    while i < n:
        if not mask[i]:
            i += 1
            continue
        s = e = i
        while e + 1 < n:
            while e + 1 < n and mask[e + 1]:
                e += 1
            z = e + 1
            while z < n and not mask[z]:
                z += 1
            if z == n or weights[e + 1:z].sum() > L:
                break
            e = z
        runs.append((s, e))
        i = e + 1

    return [[list(range(s, e + 1)) for s, e in
             [(runs[a][0], runs[b][1]) if a == b else (runs[a][0], runs[b][1])
              for a, b in zip((0, *[i + 1 for i in range(len(runs)-1) if m >> i & 1]),
                              [i for i in range(len(runs)-1) if m >> i & 1] + [len(runs)-1])]]
            for m in range(1 << (len(runs) - 1))]

def generate_groups2(mask, w, L):
    mask = np.asarray(mask)
    runs, i, n = [], 0, len(mask)

    while i < n:
        while i < n and mask[i] == 0: i += 1
        if i == n: break

        s = e = i
        while True:
            while e + 1 < n and mask[e + 1] == mask[s]:
                e += 1
            z = e + 1
            while z < n and mask[z] == 0:
                z += 1
            lim = L[mask[s]] if isinstance(L, dict) else L
            if z == n or mask[z] != mask[s] or w[e+1:z].sum() > lim:
                break
            e = z

        runs.append((s, e))
        i = e + 1

    m = len(runs)
    return [[
        (idx := list(range(runs[a][0], runs[b][1] + 1)), mask[idx].max())
        for a, b in zip(
            [0] + [i + 1 for i in range(m - 1) if c >> i & 1],
            [i for i in range(m - 1) if c >> i & 1] + [m - 1]
        )
    ] for c in range(1 << max(0, m - 1))]


def rectangles_to_xyxy(rectangles, xs, ys):
    """
    Переводит прямоугольники из индексов ячеек:

        (xmin, ymin, xmax, ymax)

    в физические координаты:

        (xmin, ymin, xmax, ymax)

    Индексы xmax/ymax во входных данных включительные.
    """

    x_edges = [0.0, *accumulate(xs)]
    y_edges = [0.0, *accumulate(ys)]

    result = []

    for rdata in rectangles:
        if len(rdata) == 4:
            xmin, ymin, xmax, ymax = rdata
        else:
            xmin, ymin, xmax, ymax, col = rdata
        result.append((
            x_edges[xmin],
            y_edges[ymin],
            x_edges[xmax + 1],
            y_edges[ymax + 1],
            col
        ))

    return result

def groups_fast(mask, widths, L):
    mask = np.asarray(mask)
    wp = np.r_[0, np.cumsum(widths)]
    runs, i, n = [], 0, len(mask)

    while i < n:
        while i < n and mask[i] == 0:
            i += 1
        if i == n:
            break

        s = e = i
        while True:
            while e + 1 < n and mask[e + 1] == mask[s]:
                e += 1

            z = e + 1
            while z < n and mask[z] == 0:
                z += 1

            lim = L[int(mask[s])] if isinstance(L, dict) else L

            if (
                z == n
                or mask[z] != mask[s]
                or wp[z] - wp[e + 1] > lim
            ):
                break

            e = z

        runs.append((s, e, int(mask[s])))
        i = e + 1

    out = []

    for a in range(len(runs)):
        mx = 0
        y1 = runs[a][0]

        for b in range(a, len(runs)):
            mx = max(mx, runs[b][2])
            out.append((y1, runs[b][1], mx))

    return out


def generate_all_rectangles(
    int_matrix,
    x_steps,
    y_steps,
    xs,
    min_w,
    holds,
):
    A = np.asarray(int_matrix)
    ny, nx = A.shape

    active_col = np.any(A != 0, axis=0)

    nz = np.vstack([
        np.zeros((1, nx), dtype=np.int32),
        np.cumsum(A != 0, axis=0),
    ])

    cache = {}
    rectangles = set()

    for x1 in range(nx):
        if not active_col[x1]:
            continue

        x2_start = max(
            x1,
            np.searchsorted(
                xs,
                xs[x1] + min_w,
                side="left",
            ) - 1,
        )

        if x2_start >= nx:
            continue

        profile = A[:, x1:x2_start + 1].max(axis=1)

        for x2 in range(x2_start, nx):
            if x2 > x2_start:
                np.maximum(
                    profile,
                    A[:, x2],
                    out=profile,
                )

            if not active_col[x2] or not profile.any():
                continue

            key = profile.tobytes()

            groups = cache.get(key)

            if groups is None:
                groups = groups_fast(
                    profile,
                    y_steps,
                    holds,
                )
                cache[key] = groups

            for y1, y2, w in groups:
                if (
                    nz[y2 + 1, x1] - nz[y1, x1]
                    or
                    nz[y2 + 1, x2] - nz[y1, x2]
                ):
                    rectangles.add(
                        (x1, y1, x2, y2, w)
                    )

    return list(rectangles)

def generate_recipe_rectangles(
    int_matrix,
    recipes,
    x_steps,
    y_steps,
    xs,
    min_w,
    holds,
):
    A = np.asarray(int_matrix)

    @lru_cache(None)
    def expand(c):
        if c == 0:
            return ()
        if c not in recipes:
            return (c,)
        return tuple(sorted(
            (v for x in recipes[c] for v in expand(x)),
            reverse=True,
        ))

    classes = set(map(int, np.unique(A))) | set(recipes)
    classes |= {x for r in recipes.values() for x in r}

    base = sorted(c for c in classes if c and c not in recipes)
    req = {c: expand(c) for c in classes}

    out = set()
    seen_views = set()

    for b in base:
        # Сколько слоёв класса >= b требуется каждому классу.
        need = {
            c: tuple(v for v in req[c] if v >= b)
            for c in classes
        }

        for layer in range(max(map(len, need.values()), default=0)):
            active = {
                c for c in classes
                if len(need[c]) > layer
            }
            if not active:
                continue

            # 1. Detailed:
            # сохраняем исходные composite-классы.
            detailed = np.zeros_like(A)
            for c in active:
                detailed[A == c] = c

            # 2. Promoted:
            # убираем границу между классами, которые на данном
            # слое требуют один и тот же тип арматуры.
            groups = {}
            for c in active:
                v = need[c][layer]
                groups[v] = max(groups.get(v, 0), c)

            promoted = np.zeros_like(A)
            for c in active:
                promoted[A == c] = groups[need[c][layer]]

            for M in (detailed, promoted):
                key = (b, M.tobytes())
                if key in seen_views or not np.any(M):
                    continue
                seen_views.add(key)

                view_holds = holds
                if isinstance(holds, dict):
                    view_holds = {
                        int(v): holds[b]
                        for v in np.unique(M)
                        if v
                    }

                rects = generate_all_rectangles(
                    int_matrix=M,
                    x_steps=x_steps,
                    y_steps=y_steps,
                    xs=xs,
                    min_w=min_w,
                    holds=view_holds,
                )

                # Геометрию берём из view,
                # selectable-класс задаём базовый.
                for x1, y1, x2, y2, _ in rects:
                    out.add((x1, y1, x2, y2, b))

    return list(out)

def relabel_rectangle_candidates(rectangles, recipes):
    """Convert requirement-class geometries into selectable base classes.

    Direct base-class rectangles are retained. Composite classes are expanded
    recursively; repeated recipe layers need only one geometry because their
    multiplicity is represented by the optimizer variable upper bound.
    """

    recipes = {int(k): tuple(map(int, v)) for k, v in dict(recipes or {}).items()}
    cache = {}

    def expand(cls, stack=()):
        cls = int(cls)
        if cls <= 0:
            return ()
        if cls in cache:
            return cache[cls]
        if cls in stack:
            raise ValueError(f"Циклический recipe: {' -> '.join(map(str, (*stack, cls)))}")
        parts = recipes.get(cls)
        result = (cls,) if parts is None else tuple(x for part in parts for x in expand(part, (*stack, cls)))
        cache[cls] = result
        return result

    out = set()
    for i, raw in enumerate(rectangles):
        if len(raw) != 5:
            raise ValueError(f"rectangles[{i}] должен иметь формат (x0,y0,x1,y1,class)")
        x0, y0, x1, y1, cls = raw
        for base in set(expand(cls)):
            out.add((int(x0), int(y0), int(x1), int(y1), int(base)))
    return sorted(out)
