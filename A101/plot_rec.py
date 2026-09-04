import numpy as np
import matplotlib.pyplot as plt
from matplotlib.path import Path
from matplotlib.patches import PathPatch, Rectangle

from A101.poly_bbox import geometry_to_polygons


def plot_poly2(
    result,
    boxes=None,
    title="Result",
    skip_min_loads=0,
):
    """
    Parameters
    ----------
    result : list[dict]
        Список объектов с ключами:
        - "load"
        - "geometry"

    boxes : np.ndarray, shape (N, 5), optional
        Боксы в формате:
        [x1, y1, x2, y2, class_id]

    title : str
        Заголовок графика.

    skip_min_loads : int
        Сколько минимальных уникальных значений load не визуализировать.
        Например:
            loads = [22, 22, 22, 45, 45, 66, 45, 34, 22, 22]
            skip_min_loads = 2

        Будут исключены load = 22 и load = 34.
    """

    # --- Определяем, какие load нужно пропустить ---
    loads = np.array([item["load"] for item in result])

    unique_loads = np.unique(loads)
    skipped_loads = set(unique_loads[:skip_min_loads])

    # Оставляем только те элементы, которые нужно рисовать
    filtered_result = [
        item for item in result
        if item["load"] not in skipped_loads
    ]

    fig, ax = plt.subplots(figsize=(12, 8))

    # Нормализацию делаем только по отображаемым значениям
    if filtered_result:
        filtered_loads = np.array(
            [item["load"] for item in filtered_result]
        )

        norm = plt.Normalize(
            filtered_loads.min(),
            filtered_loads.max()
        )
    else:
        norm = plt.Normalize(0, 1)

    cmap = plt.get_cmap("viridis")

    # --- Рисуем полигоны ---
    for item in filtered_result:
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

            ax.add_patch(
                PathPatch(
                    Path(vertices, codes),
                    facecolor=color,
                    edgecolor="black",
                    alpha=.7
                )
            )

    # --- Рисуем боксы ---
    if boxes is not None:
        boxes = np.asarray(boxes)

        if boxes.ndim != 2 or boxes.shape[1] != 5:
            raise ValueError(
                f"boxes должен иметь форму (N, 5), получено {boxes.shape}"
            )

        for x1, y1, x2, y2, class_id in boxes:
            width = x2 - x1
            height = y2 - y1

            rect = Rectangle(
                (x1, y1),
                width,
                height,
                fill=False,
                edgecolor="red",
                linewidth=2,
            )

            ax.add_patch(rect)

            ax.text(
                x1,
                y1,
                str(int(class_id)),
                color="red",
                fontsize=10,
                verticalalignment="bottom",
                horizontalalignment="left",
                bbox=dict(
                    facecolor="white",
                    alpha=0.7,
                    edgecolor="none"
                )
            )

    ax.autoscale()
    ax.set_aspect("equal")
    ax.set_title(title)
    ax.grid()

    plt.show()