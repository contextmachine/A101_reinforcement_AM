from collections import defaultdict

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.path import Path
from matplotlib.patches import PathPatch
from shapely.geometry import Polygon, box
from shapely.ops import unary_union


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