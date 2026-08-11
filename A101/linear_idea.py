import numpy as np
from itertools import accumulate

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