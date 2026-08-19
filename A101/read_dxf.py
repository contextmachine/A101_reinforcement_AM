import ezdxf
import numpy as np
from shapely.geometry import Polygon


def extract_polygons(dxf_path):

    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()

    block = doc.blocks.get("KLEENKA")
    scale = []
    for solid in block.query('SOLID[layer=="COLORSCALE"]'):
        scale.append({
            "points": np.array([
                tuple(list(solid.dxf.vtx0)[:2]),
                tuple(list(solid.dxf.vtx1)[:2]),
                tuple(list(solid.dxf.vtx2)[:2]),
                tuple(list(solid.dxf.vtx3)[:2]),
            ]),
            "color": int(solid.dxf.color),
        })
        scale[-1]["position"] = scale[-1]["points"].mean(axis=0)
    scale.sort(key=lambda x: x["position"][0])

    labels = []
    for insert in msp.query('INSERT[name=="KLEENKA"]'):
        for attr in insert.attribs:
            pos = getattr(attr.dxf, "align_point", attr.dxf.insert)

            labels.append({
                "text": attr.dxf.text,
                "position": tuple(list(pos)[:2]),
            })
    labels.sort(key=lambda x: x["position"][0])

    color_map = {color['color']: float(text['text']) for color, text in zip(scale, labels[1:])}

    mosaic = []
    for face in msp.query('3DFACE[layer=="KLEENKA"]'):
        mosaic.append({
            "points": np.array([
                tuple(list(face.dxf.vtx0)[:2]),
                tuple(list(face.dxf.vtx1)[:2]),
                tuple(list(face.dxf.vtx2)[:2]),
                tuple(list(face.dxf.vtx3)[:2]),
            ])*1000,
            "color": int(face.dxf.color),
            "load": color_map[int(face.dxf.color)]-1
        })
    for poly in mosaic:
        poly['geometry'] = Polygon(poly['points'])

    return mosaic