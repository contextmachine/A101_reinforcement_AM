"""Reading the ЛИРА-САПР mosaic out of a DXF export, and writing the result back."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import ezdxf
import numpy as np

MOSAIC_LAYER = "KLEENKA"
SCALE_LAYER = "COLORSCALE"
SCALE_BLOCK = "KLEENKA"
MM_PER_UNIT = 1000.0  # ТЗ: the export is in metres for PNG, in "units"=metres here

_AXIS_X = {"X", "Х"}  # latin / cyrillic
_AXIS_Y = {"Y", "У"}


@dataclass
class Mosaic:
    """Finite-element mosaic of *required total* reinforcement, in mm coordinates."""

    path: Path
    #: (n, 4, 2) polygon vertices, mm, in plate coordinates (x right, y up)
    polygons: np.ndarray
    #: (n,) AutoCAD colour index of each face
    colors: np.ndarray
    #: (n,) index of the colour-scale band of each face
    bands: np.ndarray
    #: (n,) required total reinforcement, cm²/m (upper bound of the band)
    required: np.ndarray
    #: scale bands as (color, lower, upper), left to right
    scale: list[tuple[int, float, float]]
    #: bar direction encoded in the file name: "X" or "Y"
    direction: str
    #: "top" / "bottom" face of the slab
    face: str

    @property
    def background_as(self) -> float:
        """Area of the background mesh = second boundary of the colour scale."""
        return self.scale[0][2]

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        pts = self.polygons.reshape(-1, 2)
        return (*pts.min(axis=0), *pts.max(axis=0))

    def fe_size_mm(self) -> tuple[float, float]:
        """Median finite-element size along x and y (mm)."""
        w = self.polygons[:, :, 0].max(axis=1) - self.polygons[:, :, 0].min(axis=1)
        h = self.polygons[:, :, 1].max(axis=1) - self.polygons[:, :, 1].min(axis=1)
        return float(np.median(w)), float(np.median(h))

    def areas_m2(self) -> np.ndarray:
        p = self.polygons / 1000.0
        x, y = p[:, :, 0], p[:, :, 1]
        return 0.5 * np.abs(
            np.sum(x * np.roll(y, -1, axis=1) - np.roll(x, -1, axis=1) * y, axis=1)
        )


def _axis_from_name(stem: str) -> str:
    letters = re.findall(r"[XХYУ]", stem, flags=re.IGNORECASE)
    if not letters:
        raise ValueError(
            f"cannot tell the bar direction from '{stem}'; pass direction explicitly"
        )
    last = letters[-1].upper()
    if last in _AXIS_X:
        return "X"
    if last in _AXIS_Y:
        return "Y"
    raise ValueError(f"unexpected axis letter '{last}' in '{stem}'")


def _face_from_name(stem: str) -> str:
    low = stem.lower()
    if "нижн" in low:
        return "bottom"
    if "верхн" in low:
        return "top"
    return "unknown"


def _quad(entity) -> np.ndarray:
    return np.array(
        [
            tuple(entity.dxf.vtx0)[:2],
            tuple(entity.dxf.vtx1)[:2],
            tuple(entity.dxf.vtx2)[:2],
            tuple(entity.dxf.vtx3)[:2],
        ],
        dtype=float,
    )


def read_mosaic(path: str | Path, direction: str | None = None) -> Mosaic:
    path = Path(path)
    doc = ezdxf.readfile(str(path))
    msp = doc.modelspace()

    # --- colour scale: SOLIDs in the legend block, ordered left to right ------
    block = doc.blocks.get(SCALE_BLOCK)
    swatches = []
    for solid in block.query(f'SOLID[layer=="{SCALE_LAYER}"]'):
        pts = _quad(solid)
        swatches.append((float(pts[:, 0].mean()), int(solid.dxf.color)))
    swatches.sort()

    # --- scale labels: block attributes, ordered left to right ---------------
    labels = []
    for insert in msp.query(f'INSERT[name=="{SCALE_BLOCK}"]'):
        for attr in insert.attribs:
            pos = getattr(attr.dxf, "align_point", None) or attr.dxf.insert
            text = attr.dxf.text.strip()
            if not text:
                continue
            try:
                value = float(text.replace(",", "."))
            except ValueError:
                continue
            labels.append((float(tuple(pos)[0]), value))
    labels.sort()

    if len(labels) != len(swatches) + 1:
        raise ValueError(
            f"{path.name}: colour scale has {len(swatches)} bands but "
            f"{len(labels)} numeric labels (expected {len(swatches) + 1})"
        )

    scale = [
        (color, labels[i][1], labels[i + 1][1])
        for i, (_, color) in enumerate(swatches)
    ]
    band_of_color = {color: i for i, (color, _, _) in enumerate(scale)}

    # --- mosaic faces --------------------------------------------------------
    polygons, colors = [], []
    for face in msp.query(f'3DFACE[layer=="{MOSAIC_LAYER}"]'):
        color = int(face.dxf.color)
        if color not in band_of_color:
            raise ValueError(f"{path.name}: face colour {color} is not on the colour scale")
        polygons.append(_quad(face))
        colors.append(color)
    if not polygons:
        raise ValueError(f"{path.name}: no 3DFACE entities on layer {MOSAIC_LAYER}")

    polygons = np.array(polygons) * MM_PER_UNIT
    colors = np.array(colors, dtype=int)
    bands = np.array([band_of_color[c] for c in colors], dtype=int)
    required = np.array([scale[b][2] for b in bands], dtype=float)

    return Mosaic(
        path=path,
        polygons=polygons,
        colors=colors,
        bands=bands,
        required=required,
        scale=scale,
        direction=direction or _axis_from_name(path.stem),
        face=_face_from_name(path.stem),
    )


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

ZONE_LAYER = "ДОП_АРМИРОВАНИЕ_ЗОНЫ"
CORE_LAYER = "ДОП_АРМИРОВАНИЕ_ЗОНЫ_БЕЗ_АНКЕРОВКИ"
BAR_LAYER = "ДОП_АРМИРОВАНИЕ_СТЕРЖНИ"
TEXT_LAYER = "ДОП_АРМИРОВАНИЕ_ВЫНОСКИ"
MOSAIC_OUT_LAYER = "МОЗАИКА"

_ZONE_COLORS = [5, 3, 4, 6, 1, 2, 30, 40, 210, 150]


def write_zones_dxf(
    path: str | Path,
    mosaic: Mosaic,
    zones: list,
    *,
    include_mosaic: bool = True,
) -> None:
    """Write the mosaic + the generated zones with ТЗ-style leaders (mm)."""
    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = 4  # millimetres
    msp = doc.modelspace()
    for name, color in (
        (MOSAIC_OUT_LAYER, 8),
        (ZONE_LAYER, 5),
        (CORE_LAYER, 2),
        (BAR_LAYER, 1),
        (TEXT_LAYER, 3),
    ):
        if name not in doc.layers:
            doc.layers.add(name, color=color)

    if include_mosaic:
        for poly, color in zip(mosaic.polygons, mosaic.colors):
            msp.add_solid(
                [poly[0], poly[1], poly[3], poly[2]],  # SOLID vertex order
                dxfattribs={"layer": MOSAIC_OUT_LAYER, "color": int(color)},
            )

    opts = sorted({z.option.label for z in zones})
    color_of = {label: _ZONE_COLORS[i % len(_ZONE_COLORS)] for i, label in enumerate(opts)}
    text_h = 120.0

    for zone in zones:
        color = color_of[zone.option.label]
        x0, y0, x1, y1 = zone.rect_xy()
        msp.add_lwpolyline(
            [(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
            close=True,
            dxfattribs={"layer": ZONE_LAYER, "color": color},
        )
        # the same zone without the 40d tails — «L треб.» of the ТЗ, on its own
        # layer so it can be switched off independently of the detailed bars
        cx0, cy0, cx1, cy1 = zone.core_rect_xy()
        msp.add_lwpolyline(
            [(cx0, cy0), (cx1, cy0), (cx1, cy1), (cx0, cy1)],
            close=True,
            dxfattribs={"layer": CORE_LAYER, "color": color},
        )
        # bar direction indicator through the middle of the zone
        if zone.direction == "X":
            mid = 0.5 * (y0 + y1)
            msp.add_line((x0, mid), (x1, mid), dxfattribs={"layer": BAR_LAYER, "color": color})
        else:
            mid = 0.5 * (x0 + x1)
            msp.add_line((mid, y0), (mid, y1), dxfattribs={"layer": BAR_LAYER, "color": color})

        leader_from = (0.5 * (x0 + x1), 0.5 * (y0 + y1))
        leader_to = (x1 + 600.0, y1 + 600.0)
        msp.add_leader(
            [leader_from, leader_to],
            dxfattribs={"layer": TEXT_LAYER, "color": color},
        )
        msp.add_mtext(
            zone.annotation,
            dxfattribs={
                "layer": TEXT_LAYER,
                "color": color,
                "char_height": text_h,
                "insert": (leader_to[0] + 100.0, leader_to[1]),
                "attachment_point": 4,  # middle-left
            },
        )

    doc.saveas(str(path))
