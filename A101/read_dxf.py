from __future__ import annotations

from io import BytesIO, StringIO, TextIOWrapper
from copy import deepcopy
import ezdxf
import numpy as np
from ezdxf.filemanagement import dxf_stream_info
from shapely.geometry import Polygon
import shapely
from shapely.strtree import STRtree


def _is_usable_face(points, eps: float = 1e-9) -> bool:
    """Return True for 3DFACE geometry that represents a non-zero area polygon.

    DXF exports may contain line-like 3DFACE entities with only two unique
    vertices.  Those are not reinforcement cells and must not invalidate the
    entire drawing.  Triangles remain valid even when vtx3 repeats vtx2.
    """
    arr = np.asarray(points, dtype=float)
    if arr.ndim != 2 or arr.shape[0] < 3 or arr.shape[1] < 2:
        return False
    xy = arr[:, :2]
    if len(np.unique(xy, axis=0)) < 3:
        return False
    geometry = Polygon(xy)
    return (not geometry.is_empty) and float(geometry.area) > float(eps)


def _extract_polygons_from_doc(doc):
    msp = doc.modelspace()

    block = doc.blocks.get("KLEENKA")
    scale = []
    for solid in block.query('SOLID[layer=="COLORSCALE"]'):
        scale.append(
            {
                "points": np.array(
                    [
                        tuple(list(solid.dxf.vtx0)[:2]),
                        tuple(list(solid.dxf.vtx1)[:2]),
                        tuple(list(solid.dxf.vtx2)[:2]),
                        tuple(list(solid.dxf.vtx3)[:2]),
                    ]
                ),
                "color": int(solid.dxf.color),
            }
        )
        scale[-1]["position"] = scale[-1]["points"].mean(axis=0)
    scale.sort(key=lambda x: x["position"][0])

    labels = []
    for insert in msp.query('INSERT[name=="KLEENKA"]'):
        for attr in insert.attribs:
            pos = getattr(attr.dxf, "align_point", None) or attr.dxf.insert
            labels.append(
                {
                    "text": attr.dxf.text,
                    "position": tuple(list(pos)[:2]),
                }
            )
    labels.sort(key=lambda x: x["position"][0])

    color_map = {
        color["color"]: float(text["text"])
        for color, text in zip(scale, labels[1:])
    }

    mosaic = []
    for face in msp.query('3DFACE[layer=="KLEENKA"]'):
        color = int(face.dxf.color)
        points = (
            np.array(
                [
                    tuple(list(face.dxf.vtx0)[:2]),
                    tuple(list(face.dxf.vtx1)[:2]),
                    tuple(list(face.dxf.vtx2)[:2]),
                    tuple(list(face.dxf.vtx3)[:2]),
                ]
            )
            * 1000
        )
        if not _is_usable_face(points):
            continue
        mosaic.append(
            {
                "points": points,
                "color": color,
                "load": color_map[color] - 1,
            }
        )

    for poly in mosaic:
        poly["geometry"] = Polygon(poly["points"])

    return mosaic


def extract_polygons(dxf_path):
    return _extract_polygons_from_doc(ezdxf.readfile(dxf_path))


def extract_polygons_from_bytes(content: bytes):
    """Parse an ASCII DXF payload without creating a temporary file."""
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise TypeError("DXF content must be bytes")

    raw = bytes(content)
    # DXF control structure and $DWGCODEPAGE are ASCII-compatible, so latin-1
    # is safe for this first lightweight pass. Then reopen with the detected
    # source encoding for the actual ezdxf parser.
    probe = StringIO(raw.decode("latin-1"))
    info = dxf_stream_info(probe)

    stream = TextIOWrapper(
        BytesIO(raw),
        encoding=info.encoding,
        errors="surrogateescape",
        newline=None,
    )
    try:
        doc = ezdxf.read(stream)
    finally:
        stream.detach()

    return _extract_polygons_from_doc(doc)


def smooth_load(polys, threshold=.6, eps=1e-9):
    polys = deepcopy(polys)
    n = len(polys)
    if not n:
        return polys

    load = np.array([p["load"] for p in polys], float)
    geom = np.array([p["geometry"] for p in polys], object)

    for p in polys:
        p["old_load"] = p["load"]

    i, j = STRtree(geom).query(geom, predicate="intersects")
    m = i < j
    i, j = i[m], j[m]

    L = np.asarray(shapely.length(shapely.intersection(
        shapely.boundary(geom[i]), shapely.boundary(geom[j])
    )))
    m = L > eps
    i, j, L = i[m], j[m], L[m]

    total = np.zeros(n)
    lower = np.zeros(n)
    np.add.at(total, i, L)
    np.add.at(total, j, L)

    m1, m2 = load[j] < load[i], load[i] < load[j]
    np.add.at(lower, i[m1], L[m1])
    np.add.at(lower, j[m2], L[m2])

    ratio = np.divide(lower, total, out=np.zeros(n), where=total > eps)
    candidate = ratio > threshold

    neighbors = [[] for _ in range(n)]
    for a, b, l in zip(i, j, L):
        neighbors[a].append((b, l))
        neighbors[b].append((a, l))

    new_load = load.copy()

    for k in np.where(candidate)[0]:
        cand_nb = [x for x, _ in neighbors[k] if candidate[x]]

        # Если рядом кандидат с таким же или большим load — не меняем
        if any(load[x] >= load[k] for x in cand_nb):
            continue

        # Нижний сосед с максимальной длиной касания
        low = [(x, l) for x, l in neighbors[k] if load[x] < load[k]]
        if not low:
            continue

        x = max(low, key=lambda z: z[1])[0]
        target = load[x]

        # Не опускаемся ниже соседних кандидатов
        if cand_nb:
            target = max(target, max(load[x] for x in cand_nb))

        new_load[k] = target

    for k, p in enumerate(polys):
        p["load"] = new_load[k]
        p["lower_touch_ratio"] = ratio[k]
        p["same_or_higher_neighbors"] = [
            x for x, _ in neighbors[k] if load[x] >= load[k]
        ]

    return polys