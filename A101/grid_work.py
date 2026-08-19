from shapely.geometry import Polygon, MultiPolygon, box
import numpy as np
from shapely.strtree import STRtree
import matplotlib.pyplot as plt
from A101.poly_bbox import geometry_to_polygons


def remove_collinear_from_ring(ring):
    coords = list(ring.coords)

    # Убираем замыкающую копию первой точки
    if coords[0] == coords[-1]:
        coords = coords[:-1]

    result = []

    n = len(coords)

    for i in range(n):
        prev = coords[i - 1]
        curr = coords[i]
        nxt = coords[(i + 1) % n]

        collinear = (
            (prev[0] == curr[0] == nxt[0]) or
            (prev[1] == curr[1] == nxt[1])
        )

        if not collinear:
            result.append(curr)

    # Замыкаем контур
    result.append(result[0])

    return result


def remove_collinear_points(poly):
    # Внешний контур
    exterior = remove_collinear_from_ring(poly.exterior)

    # Все внутренние контуры
    interiors = [
        remove_collinear_from_ring(interior)
        for interior in poly.interiors
    ]

    return Polygon(exterior, interiors)

def clean_poly(polygons):
    result = []
    for obj in polygons:
        geom = obj["geometry"]
        if isinstance(geom, MultiPolygon):
            for polygon in geom.geoms:
                new_obj = obj.copy()
                new_obj["geometry"] = remove_collinear_points(polygon)
                result.append(new_obj)
        else:
            new_obj = obj.copy()
            new_obj["geometry"] = remove_collinear_points(geom)
            result.append(new_obj)
    return result

def resolve_overlaps(polygons):
    # Сначала самые "сильные" полигоны
    polygons_sorted = sorted(
        polygons,
        key=lambda obj: obj["load"],
        reverse=True
    )

    result = []
    occupied = None

    for obj in polygons_sorted:
        geom = obj["geometry"]

        # Убираем из текущего полигона всё,
        # что уже занято полигонами с большим load
        if occupied is not None:
            geom = geom.difference(occupied)

        # После вычитания геометрия может исчезнуть
        if geom.is_empty:
            continue

        # difference может вернуть Polygon или MultiPolygon.
        # Разбиваем MultiPolygon на отдельные объекты.
        if isinstance(geom, MultiPolygon):
            for polygon in geom.geoms:
                new_obj = obj.copy()
                new_obj["geometry"] = polygon
                result.append(new_obj)
        else:
            new_obj = obj.copy()
            new_obj["geometry"] = geom
            result.append(new_obj)

        # Добавляем исходную геометрию в занятую область,
        # а не обрезанную geom
        if occupied is None:
            occupied = obj["geometry"]
        else:
            occupied = occupied.union(obj["geometry"])

    return result

def get_grid(result):

    # ---------------------------------------------------------
    # 1. Собираем координаты
    # ---------------------------------------------------------

    xs = set()
    ys = set()

    for item in result:
        geom = item["geometry"]

        for poly in geometry_to_polygons(geom):

            for x, y in poly.exterior.coords:
                xs.add(x)
                ys.add(y)

            for interior in poly.interiors:
                for x, y in interior.coords:
                    xs.add(x)
                    ys.add(y)

    xs = np.array(sorted(xs))
    ys = np.array(sorted(ys))

    # ---------------------------------------------------------
    # 2. Создаём все ячейки
    # ---------------------------------------------------------

    geometries = []
    cells = []

    for ix in range(len(xs) - 1):
        x1 = xs[ix]
        x2 = xs[ix + 1]

        width = x2 - x1

        if width <= 0:
            continue

        for iy in range(len(ys) - 1):
            y1 = ys[iy]
            y2 = ys[iy + 1]

            height = y2 - y1

            if height <= 0:
                continue

            cell = box(x1, y1, x2, y2)

            geometries.append(cell)

            cells.append({
                "x": x1,
                "y": y1,
                "width": width,
                "height": height,
                "load": None,
            })

    # ---------------------------------------------------------
    # 3. Создаём spatial index
    # ---------------------------------------------------------

    polygon_geometries = [
        item["geometry"]
        for item in result
    ]

    tree = STRtree(polygon_geometries)

    # ---------------------------------------------------------
    # 4. Ищем пересечения
    # ---------------------------------------------------------

    for cell, cell_data in zip(geometries, cells):

        # Вместо проверки ВСЕХ полигонов
        # STRtree сначала находит только потенциально
        # пересекающиеся.
        indices = tree.query(cell)

        for index in indices:

            polygon = polygon_geometries[index]

            if polygon.contains(cell.representative_point()):
                cell_data["load"] = result[index]["load"]
                break

    return cells

def get_grid_matrix(result):

    # ---------------------------------------------------------
    # 1. Собираем координаты
    # ---------------------------------------------------------

    xs = set()
    ys = set()

    for item in result:
        geom = item["geometry"]

        for poly in geometry_to_polygons(geom):

            for x, y in poly.exterior.coords:
                xs.add(x)
                ys.add(y)

            for interior in poly.interiors:
                for x, y in interior.coords:
                    xs.add(x)
                    ys.add(y)

    xs = np.array(sorted(xs))
    ys = np.array(sorted(ys))

    # ---------------------------------------------------------
    # 2. Создаём spatial index
    # ---------------------------------------------------------

    polygon_geometries = [
        item["geometry"]
        for item in result
    ]

    tree = STRtree(polygon_geometries)

    # ---------------------------------------------------------
    # 3. Создаём матрицу load
    # ---------------------------------------------------------

    # Строки    -> Y
    # Столбцы   -> X
    #
    # matrix[iy, ix] соответствует:
    # x: xs[ix]   -> xs[ix + 1]
    # y: ys[iy]   -> ys[iy + 1]

    # Uncovered cells are the background reinforcement class (load 0).
    load_matrix = np.zeros(
        (len(ys) - 1, len(xs) - 1),
        dtype=float,
    )

    # ---------------------------------------------------------
    # 4. Заполняем матрицу
    # ---------------------------------------------------------

    for ix in range(len(xs) - 1):

        x1 = xs[ix]
        x2 = xs[ix + 1]

        for iy in range(len(ys) - 1):

            y1 = ys[iy]
            y2 = ys[iy + 1]

            cell = box(x1, y1, x2, y2)

            indices = tree.query(cell)

            for index in indices:

                polygon = polygon_geometries[index]

                if polygon.contains(cell.representative_point()):
                    load_matrix[iy, ix] = result[index]["load"]
                    break

    return xs, ys, load_matrix

from matplotlib.patches import Rectangle


def plot_grid(grid, title="Grid"):
    fig, ax = plt.subplots(figsize=(14, 10))

    loads = sorted({
        cell["load"]
        for cell in grid
        if cell["load"] is not None
    })

    cmap = plt.get_cmap("tab20")

    color_by_load = {
        load: cmap(i % 20)
        for i, load in enumerate(loads)
    }

    for cell in grid:

        x = cell["x"]
        y = cell["y"]
        width = cell["width"]
        height = cell["height"]
        load = cell["load"]

        if load is None:
            facecolor = "white"
        else:
            facecolor = color_by_load[load]

        rect = Rectangle(
            (x, y),
            width,
            height,
            facecolor=facecolor,
            edgecolor="black",
            linewidth=0.01,
            alpha=0.6,
        )

        ax.add_patch(rect)

    ax.autoscale()
    ax.set_aspect("equal")
    ax.set_title(title)
    ax.grid(False)

    plt.show()