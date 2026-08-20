from __future__ import annotations

from io import BytesIO, StringIO, TextIOWrapper

import ezdxf
import numpy as np
from ezdxf.filemanagement import dxf_stream_info
from shapely.geometry import Polygon


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
        mosaic.append(
            {
                "points": np.array(
                    [
                        tuple(list(face.dxf.vtx0)[:2]),
                        tuple(list(face.dxf.vtx1)[:2]),
                        tuple(list(face.dxf.vtx2)[:2]),
                        tuple(list(face.dxf.vtx3)[:2]),
                    ]
                )
                * 1000,
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