from collections import defaultdict

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.path import Path
from matplotlib.patches import PathPatch
from shapely.geometry import Polygon, box, MultiPolygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union
import shapely
from shapely.strtree import STRtree


def polygon_from_points(points):
    geom = Polygon(points)
    return geom if geom.is_valid else geom.buffer(0)


def geometry_to_polygons(geom):
    if geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type == "MultiPolygon":
        return list(geom.geoms)
    if geom.geom_type == "GeometryCollection":
        return [
            p
            for g in geom.geoms
            for p in (
                [g] if g.geom_type == "Polygon"
                else list(g.geoms) if g.geom_type == "MultiPolygon"
                else []
            )
        ]
    return []


def remove_redundant_points(poly, tol=1e-9):
    def clean(coords):
        p = np.asarray(coords[:-1], dtype=float)
        n = len(p)

        if n <= 3:
            return np.vstack((p, p[0]))

        prev = np.roll(p, 1, axis=0)
        nxt = np.roll(p, -1, axis=0)

        cross = (
            (p[:, 0] - prev[:, 0]) * (nxt[:, 1] - p[:, 1])
            - (p[:, 1] - prev[:, 1]) * (nxt[:, 0] - p[:, 0])
        )

        p = p[np.abs(cross) > tol]
        return np.vstack((p, p[0]))

    if poly.is_empty:
        return poly

    if poly.geom_type == "Polygon":
        return Polygon(
            clean(poly.exterior.coords),
            [clean(r.coords) for r in poly.interiors]
        )

    if poly.geom_type == "MultiPolygon":
        return type(poly)(
            [remove_redundant_points(p, tol) for p in poly.geoms]
        )

    return poly


def rect_polygons(data):
    by_load = defaultdict(list)

    for item in data:
        geom = polygon_from_points(item["points"])
        if not geom.is_empty:
            by_load[item["load"]].append(box(*geom.bounds))

    loads = sorted(by_load)
    merged = {load: unary_union(boxes) for load, boxes in by_load.items()}

    result = []

    # Идём от большего load к меньшему:
    # накопленная higher = всё, что должно перекрывать текущий слой.
    higher = None

    for load in reversed(loads):
        geom = merged[load].difference(higher) if higher else merged[load]

        for poly in geometry_to_polygons(geom):
            if poly.area > 0:
                result.append({
                    "geometry": remove_redundant_points(poly),
                    "load": load
                })

        higher = geom if higher is None else unary_union([higher, geom])

    return result


def plot_poly(result, title="Result"):
    fig, ax = plt.subplots(figsize=(12, 8))

    loads = np.array([x["load"] for x in result])
    norm = plt.Normalize(loads.min(), loads.max())
    cmap = plt.get_cmap("viridis")

    for item in result:
        load = item["load"]
        color = cmap(norm(load))

        for poly in geometry_to_polygons(item["geometry"]):
            vertices = []
            codes = []

            ext = np.asarray(poly.exterior.coords)
            vertices.extend(ext)
            codes.extend(
                [Path.MOVETO]
                + [Path.LINETO] * (len(ext) - 2)
                + [Path.CLOSEPOLY]
            )

            for interior in poly.interiors:
                hole = np.asarray(interior.coords)
                vertices.extend(hole)
                codes.extend(
                    [Path.MOVETO]
                    + [Path.LINETO] * (len(hole) - 2)
                    + [Path.CLOSEPOLY]
                )

            ax.add_patch(PathPatch(
                Path(vertices, codes),
                facecolor=color,
                edgecolor="black",
                alpha=.7
            ))

    ax.autoscale()
    ax.set_aspect("equal")
    ax.set_title(title)
    ax.grid()

    plt.show()




def fill_notches(polys, threshold, eps=1e-9):

    def cross(a, b):
        return a[0] * b[1] - a[1] * b[0]

    def clean(coords):
        p = np.asarray(coords[:-1], float)

        while len(p) > 3:
            keep = [
                i for i in range(len(p))
                if abs(cross(
                    p[i] - p[i - 1],
                    p[(i + 1) % len(p)] - p[i]
                )) > eps
            ]
            if len(keep) == len(p):
                break
            p = p[keep]

        return p

    def fill_once(poly):
        poly = orient(poly, sign=1)
        pts = clean(poly.exterior.coords)
        n = len(pts)

        turn = np.array([
            cross(
                pts[i] - pts[i - 1],
                pts[(i + 1) % n] - pts[i]
            )
            for i in range(n)
        ])

        fills = []

        for i in range(n):
            # convex -> concave -> concave -> convex
            if not (
                turn[i - 1] > eps and
                turn[i] < -eps and
                turn[(i + 1) % n] < -eps and
                turn[(i + 2) % n] > eps
            ):
                continue

            a, b = pts[i], pts[(i + 1) % n]
            p1, p2 = pts[i - 1], pts[(i + 2) % n]

            bottom = np.linalg.norm(b - a)
            if bottom > threshold:
                continue

            l1 = np.linalg.norm(a - p1)
            l2 = np.linalg.norm(p2 - b)

            if min(l1, l2) <= eps:
                continue

            d1 = (p1 - a) / l1
            d2 = (p2 - b) / l2

            if not np.allclose(d1, d2, atol=eps):
                continue

            d = d1 * min(l1, l2)

            fills.append(Polygon([
                a,
                b,
                b + d,
                a + d
            ]))

        if not fills:
            return poly, False

        return unary_union([poly, *fills]), True

    # Новый список, исходный не меняем
    result = [p.copy() for p in polys]

    for item in result:
        geom = item["geometry"]

        while True:
            geom, changed = fill_once(geom)
            if not changed:
                break

        item["geometry"] = geom

    return result


def simplify_short_edges(polys, threshold=1000, eps=1e-8):

    def cross(a, b):
        return a[0] * b[1] - a[1] * b[0]

    def clean(poly):
        """Убирает дубли и коллинеарные точки."""
        p = np.asarray(poly.exterior.coords[:-1], float)

        while len(p) > 3:
            keep = np.array([
                np.linalg.norm(p[i] - p[i - 1]) > eps and
                abs(cross(
                    p[i] - p[i - 1],
                    p[(i + 1) % len(p)] - p[i]
                )) > eps
                for i in range(len(p))
            ])

            if keep.all():
                break
            p = p[keep]

        return p

    def step(poly):
        """Выполняет одно локальное увеличение полигона."""
        poly = orient(poly, sign=1)   # CCW
        p = clean(poly)
        n = len(p)

        turns = np.array([
            cross(
                p[i] - p[i - 1],
                p[(i + 1) % n] - p[i]
            )
            for i in range(n)
        ])

        for i in range(n):
            a = p[i]
            b = p[(i + 1) % n]

            # только короткие стороны
            if np.linalg.norm(b - a) >= threshold - eps:
                continue

            ta = turns[i]
            tb = turns[(i + 1) % n]

            # Выпирающая "шапка П": два выпуклых угла
            if ta > eps and tb > eps:
                continue

            prev = p[i - 1]
            nxt = p[(i + 2) % n]

            # Вогнутый угол со стороны a
            if ta < -eps and tb > eps:
                d = prev - a

            # Вогнутый угол со стороны b
            elif ta > eps and tb < -eps:
                d = nxt - b

            # Дно ямки: оба угла вогнутые
            elif ta < -eps and tb < -eps:
                d1 = prev - a
                d2 = nxt - b

                l1 = np.linalg.norm(d1)
                l2 = np.linalg.norm(d2)

                u1 = d1 / l1
                u2 = d2 / l2

                if not np.allclose(u1, u2, atol=eps):
                    continue

                d = u1 * min(l1, l2)

            else:
                continue

            box = Polygon([
                a,
                b,
                b + d,
                a + d
            ])

            new = shapely.union(poly, box)

            # Бокс уже мог оказаться внутри после предыдущих изменений
            if new.area > poly.area + eps:
                return new, True

        return poly, False

    result = [x.copy() for x in polys]

    for item in result:
        g = item["geometry"]

        while True:
            g, changed = step(g)
            if not changed:
                break

        item["geometry"] = g

    return result


def simplify_steps(polys, threshold=1000, eps=1e-8):

    def cross(a, b):
        return a[0] * b[1] - a[1] * b[0]

    def clean(p):
        p = [np.asarray(x, float) for x in p]

        while len(p) > 3:
            q = []
            n = len(p)

            for i in range(n):
                a, b, c = p[i - 1], p[i], p[(i + 1) % n]

                if (np.linalg.norm(b - a) > eps and
                    abs(cross(b - a, c - b)) > eps):
                    q.append(b)

            if len(q) == len(p):
                break

            p = q

        return p

    def process(poly):
        poly = orient(poly, sign=1)
        holes = [list(r.coords) for r in poly.interiors]
        p = clean(poly.exterior.coords[:-1])

        unchanged = 0

        while len(p) >= 4 and unchanged < len(p):

            prev, a, b, nxt = p[-1], p[0], p[1], p[2]

            Lprev = np.linalg.norm(a - prev)
            L     = np.linalg.norm(b - a)
            Lnext = np.linalg.norm(nxt - b)

            # Интересуют только короткие стороны
            if L >= threshold - eps:
                p = p[1:] + p[:1]
                unchanged += 1
                continue

            # Короткая сторона между двумя длинными — сохраняем
            if Lprev >= threshold - eps and Lnext >= threshold - eps:
                p = p[1:] + p[:1]
                unchanged += 1
                continue

            # По направлению обхода угол в b должен быть вогнутым
            if cross(b - a, nxt - b) >= -eps:
                p = p[1:] + p[:1]
                unchanged += 1
                continue

            # --------------------------------------------------
            # Выбираем глубину заполнения
            # --------------------------------------------------

            if Lnext >= threshold - eps:
                # Впереди длинная сторона, сзади короткая.
                #
                # Нельзя:
                # 1. делать box глубже threshold
                # 2. оставлять от длинной стороны кусок < threshold
                #
                # Поэтому:
                depth = min(
                    threshold,
                    Lnext - threshold
                )

                # Например Lnext == threshold:
                # вообще нельзя от неё ничего откусить
                if depth <= eps:
                    p = p[1:] + p[:1]
                    unchanged += 1
                    continue

            else:
                # Следующая сторона тоже короткая —
                # поглощаем её полностью.
                depth = Lnext

            u = (nxt - b) / Lnext

            q = a + u * depth
            r = b + u * depth

            # --------------------------------------------------
            # Локально перестраиваем контур
            # --------------------------------------------------

            if depth >= Lnext - eps:
                # Следующая короткая сторона поглощена полностью.
                #
                # ВАЖНО: nxt сохраняем, а clean сам решит,
                # стала ли эта точка коллинеарной.
                p = [q] + p[2:]

            else:
                # Длинную сторону поглотили только частично.
                #
                # Остаток r -> nxt гарантированно >= threshold.
                p = [q, r] + p[2:]

            p = clean(p)

            # Продолжаем именно от изменённой ступеньки,
            # а не начинаем обход полигона заново.
            k = next(
                (i for i, x in enumerate(p)
                 if np.linalg.norm(x - q) <= eps),
                None
            )

            if k is not None:
                p = p[k:] + p[:k]

            unchanged = 0

        return Polygon(p, holes)

    # входной список не изменяем
    return [
        {**item, "geometry": process(item["geometry"])}
        for item in polys
    ]


def polygons_to_grid(polygons, min_size=300, eps=1e-9):

    geoms = np.array([p["geometry"] for p in polygons], dtype=object)
    loads = np.array([p["load"] for p in polygons])

    # ---------------------------------------------------------
    # Кластеризация координат.
    # Близкие линии последовательно объединяются, пока
    # расстояния между всеми соседними не станут >= min_size.
    # Крайние координаты сохраняются.
    # ---------------------------------------------------------
    def cluster(values):
        v = np.sort(np.asarray(values, float))

        # сначала почти одинаковые координаты
        groups = []
        for x in v:
            if groups and abs(x - groups[-1][0]) < eps:
                groups[-1][1] += 1
            else:
                groups.append([x, 1])

        # [coordinate, weight, fixed]
        c = [
            [x, w, i == 0 or i == len(groups) - 1]
            for i, (x, w) in enumerate(groups)
        ]

        while len(c) > 2:
            x = np.array([z[0] for z in c])
            gaps = np.diff(x)

            if gaps.min() >= min_size - eps:
                break

            i = gaps.argmin()
            a, b = c[i], c[i + 1]

            # край диапазона не двигаем
            if a[2]:
                new = [a[0], a[1] + b[1], True]
            elif b[2]:
                new = [b[0], a[1] + b[1], True]
            else:
                new = [
                    (a[0] * a[1] + b[0] * b[1]) / (a[1] + b[1]),
                    a[1] + b[1],
                    False
                ]

            c[i:i + 2] = [new]

        result = np.array([z[0] for z in c])

        # Если весь диапазон меньше min_size, две крайние координаты
        # всё равно образуют одну корректную ячейку. Для локальных
        # component-задач это нормальный случай: минимальная физическая
        # ширина зоны обеспечивается позже, а сетка обязана сохранить
        # реальный контур требования.
        if len(result) > 2 and np.diff(result).min() < min_size - eps:
            raise ValueError(
                f"Не удалось кластеризовать координаты с min_size={min_size}"
            )

        return result

    # ---------------------------------------------------------
    # Все реальные координаты границ
    # ---------------------------------------------------------
    coords = np.vstack([
        shapely.get_coordinates(g)
        for g in geoms
        if not g.is_empty
    ])

    xs = cluster(coords[:, 0])
    ys = cluster(coords[:, 1])

    # ---------------------------------------------------------
    # Создаём прямоугольные ячейки
    # ---------------------------------------------------------
    x0, y0 = np.meshgrid(xs[:-1], ys[:-1])
    x1, y1 = np.meshgrid(xs[1:], ys[1:])

    x0, y0 = x0.ravel(), y0.ravel()
    x1, y1 = x1.ravel(), y1.ravel()

    cells = shapely.box(x0, y0, x1, y1)

    # ---------------------------------------------------------
    # Находим, какие исходные полигоны пересекают каждую ячейку
    # ---------------------------------------------------------
    tree = STRtree(geoms)
    ci, pi = tree.query(cells, predicate="intersects")

    areas = np.asarray(
        shapely.area(
            shapely.intersection(cells[ci], geoms[pi])
        )
    )

    # касание только по линии не учитываем
    m = areas > eps
    ci, pi, areas = ci[m], pi[m], areas[m]

    # ---------------------------------------------------------
    # Каждой ячейке назначаем полигон с максимальным overlap
    # ---------------------------------------------------------
    best_area = np.zeros(len(cells))
    best_poly = np.full(len(cells), -1, dtype=int)

    for c, p, a in zip(ci, pi, areas):
        if a > best_area[c]:
            best_area[c] = a
            best_poly[c] = p

    # ---------------------------------------------------------
    # Формируем результат
    # ---------------------------------------------------------
    result = []

    for k, p in enumerate(best_poly):
        if p < 0:
            continue

        result.append({
            "x": x0[k],
            "y": y0[k],
            "width": x1[k] - x0[k],
            "height": y1[k] - y0[k],
            "load": loads[p],
            "geometry": cells[k],
            "source_index": int(p),
        })

    return result