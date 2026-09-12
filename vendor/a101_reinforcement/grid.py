"""Turning the irregular FE mosaic into a regular raster of required options."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .catalog import BandMapping, RebarOption
from .dxf_io import Mosaic


# --------------------------------------------------------------------------- #
# ТЗ de-noising rule: "если в зоне располагается ТОЛЬКО 1 конечный элемент,
# его допускается игнорировать и использовать предыдущий диапазон по шкале"
# --------------------------------------------------------------------------- #
def _fe_adjacency(polygons: np.ndarray, tol_mm: float = 1.0) -> list[list[int]]:
    """Neighbour lists for faces sharing an edge (vertices snapped to ``tol_mm``)."""
    edge_faces: dict[tuple[tuple[int, int], tuple[int, int]], list[int]] = {}
    keys = np.round(polygons / tol_mm).astype(np.int64)
    for f, quad in enumerate(keys):
        for a in range(4):
            p, q = tuple(quad[a]), tuple(quad[(a + 1) % 4])
            if p == q:  # degenerate edge of a triangular face
                continue
            edge_faces.setdefault((min(p, q), max(p, q)), []).append(f)
    adj: list[list[int]] = [[] for _ in range(len(polygons))]
    for faces in edge_faces.values():
        for i, a in enumerate(faces):
            for b in faces[i + 1:]:
                adj[a].append(b)
                adj[b].append(a)
    return adj


def _components(members: np.ndarray, adj: list[list[int]]) -> list[list[int]]:
    seen = np.zeros(len(adj), dtype=bool)
    out = []
    for start in np.flatnonzero(members):
        if seen[start]:
            continue
        stack, comp = [start], []
        seen[start] = True
        while stack:
            f = stack.pop()
            comp.append(f)
            for g in adj[f]:
                if members[g] and not seen[g]:
                    seen[g] = True
                    stack.append(g)
        out.append(comp)
    return out


def denoise_bands(mosaic: Mosaic, max_isolated_fe: int = 1) -> np.ndarray:
    """Demote islands of ``<= max_isolated_fe`` elements to the previous band."""
    bands = mosaic.bands.copy()
    adj = _fe_adjacency(mosaic.polygons)
    for level in range(int(bands.max()), 0, -1):
        for comp in _components(bands >= level, adj):
            if len(comp) <= max_isolated_fe:
                bands[comp] = level - 1
    return bands


# --------------------------------------------------------------------------- #
# Rasterisation
# --------------------------------------------------------------------------- #
def _inside_quad(quad: np.ndarray, pu: np.ndarray, pv: np.ndarray) -> np.ndarray:
    """Point-in-quad test; the quad is split into two triangles.

    Uses the orientation (cross-product) sign test, which is independent of the
    winding order and degrades gracefully on the degenerate (triangular) faces
    ЛИРА exports at mesh refinements.
    """
    inside = np.zeros(pu.shape, dtype=bool)
    for ia, ib, ic in ((0, 1, 2), (0, 2, 3)):
        (ax, ay), (bx, by), (cx, cy) = quad[ia], quad[ib], quad[ic]
        twice_area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
        if abs(twice_area) < 1e-9:
            continue
        eps = 1e-6 * abs(twice_area)
        d1 = (bx - ax) * (pv - ay) - (by - ay) * (pu - ax)
        d2 = (cx - bx) * (pv - by) - (cy - by) * (pu - bx)
        d3 = (ax - cx) * (pv - cy) - (ay - cy) * (pu - cx)
        inside |= ((d1 >= -eps) & (d2 >= -eps) & (d3 >= -eps)) | (
            (d1 <= eps) & (d2 <= eps) & (d3 <= eps)
        )
    return inside


@dataclass
class RequirementGrid:
    """Regular raster in (u, v): ``u`` runs along the bars, ``v`` across them."""

    cell_mm: float
    origin_u: float
    origin_v: float
    #: (nv, nu) index into ``options``; 0 means "no additional reinforcement"
    need: np.ndarray
    #: (nv, nu) True where the raster cell is inside the slab
    covered: np.ndarray
    #: options[0] is None, the rest are ordered by ascending area
    options: list[RebarOption | None]
    direction: str
    fe_u_mm: float
    fe_v_mm: float

    @property
    def shape(self) -> tuple[int, int]:
        return self.need.shape

    def u_mm(self, i: float) -> float:
        return self.origin_u + i * self.cell_mm

    def v_mm(self, j: float) -> float:
        return self.origin_v + j * self.cell_mm

    def to_xy(self, u0: float, u1: float, v0: float, v1: float) -> tuple[float, float, float, float]:
        """Convert a (u, v) rectangle back to drawing coordinates."""
        if self.direction == "X":
            return u0, v0, u1, v1
        return v0, u0, v1, u1


def build_grid(
    mosaic: Mosaic,
    bands: np.ndarray,
    mapping: list[BandMapping],
    *,
    cell_mm: float = 100.0,
) -> RequirementGrid:
    """Rasterise the (de-noised) mosaic into a grid of required rebar options."""
    # unique options, ascending by area -> raster values 1..K
    used = sorted({m.option for m in mapping if m.option is not None})
    rank = {opt: i + 1 for i, opt in enumerate(used)}
    band_rank = np.array(
        [0 if m.option is None else rank[m.option] for m in mapping], dtype=np.int16
    )

    fe_x, fe_y = mosaic.fe_size_mm()
    if mosaic.direction == "X":
        polys = mosaic.polygons
        fe_u, fe_v = fe_x, fe_y
    else:  # swap axes so that u is always the bar direction
        polys = mosaic.polygons[:, :, ::-1]
        fe_u, fe_v = fe_y, fe_x

    pts = polys.reshape(-1, 2)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    origin_u = np.floor(lo[0] / cell_mm) * cell_mm
    origin_v = np.floor(lo[1] / cell_mm) * cell_mm
    nu = int(np.ceil((hi[0] - origin_u) / cell_mm))
    nv = int(np.ceil((hi[1] - origin_v) / cell_mm))

    need = np.zeros((nv, nu), dtype=np.int16)
    covered = np.zeros((nv, nu), dtype=bool)

    for quad, band in zip(polys, bands):
        r = band_rank[band]
        i0 = max(0, int((quad[:, 0].min() - origin_u) / cell_mm) - 1)
        i1 = min(nu, int((quad[:, 0].max() - origin_u) / cell_mm) + 2)
        j0 = max(0, int((quad[:, 1].min() - origin_v) / cell_mm) - 1)
        j1 = min(nv, int((quad[:, 1].max() - origin_v) / cell_mm) + 2)
        if i0 >= i1 or j0 >= j1:
            continue
        cu = origin_u + (np.arange(i0, i1) + 0.5) * cell_mm
        cv = origin_v + (np.arange(j0, j1) + 0.5) * cell_mm
        gu, gv = np.meshgrid(cu, cv)
        hit = _inside_quad(quad, gu, gv)
        if not hit.any():
            continue
        sub_need = need[j0:j1, i0:i1]
        sub_cov = covered[j0:j1, i0:i1]
        np.maximum(sub_need, np.where(hit, r, 0).astype(np.int16), out=sub_need)
        sub_cov |= hit

    return RequirementGrid(
        cell_mm=cell_mm,
        origin_u=float(origin_u),
        origin_v=float(origin_v),
        need=need,
        covered=covered,
        options=[None, *used],
        direction=mosaic.direction,
        fe_u_mm=fe_u,
        fe_v_mm=fe_v,
    )


# --------------------------------------------------------------------------- #
# Regions of the raster that are optimised independently
# --------------------------------------------------------------------------- #
@dataclass
class Region:
    id: int
    j0: int  # first row (v), inclusive
    j1: int  # last row (v), inclusive
    i0: int  # first column (u), inclusive
    i1: int  # last column (u), inclusive
    need: np.ndarray  # (j1-j0+1, i1-i0+1) slice, masked to this region


def find_regions(
    grid: RequirementGrid,
    close_cells: int = 1,
    merge_overlapping: bool = True,
    min_zone_rows: int = 1,
) -> list[Region]:
    """Connected requirement blobs (8-connected, optionally dilated to merge)."""
    mask = grid.need > 0
    if not mask.any():
        return []
    dilated = mask.copy()
    for _ in range(close_cells):
        d = dilated
        out = d.copy()
        out[1:, :] |= d[:-1, :]
        out[:-1, :] |= d[1:, :]
        out[:, 1:] |= d[:, :-1]
        out[:, :-1] |= d[:, 1:]
        dilated = out

    nv, nu = mask.shape
    label = np.full((nv, nu), -1, dtype=np.int32)
    regions: list[Region] = []
    next_label = 0
    for sj, si in zip(*np.nonzero(dilated)):
        if label[sj, si] >= 0:
            continue
        rid = next_label
        next_label += 1
        stack = [(sj, si)]
        label[sj, si] = rid
        cells = []
        while stack:
            j, i = stack.pop()
            cells.append((j, i))
            for dj in (-1, 0, 1):
                for di in (-1, 0, 1):
                    nj, ni = j + dj, i + di
                    if 0 <= nj < nv and 0 <= ni < nu and dilated[nj, ni] and label[nj, ni] < 0:
                        label[nj, ni] = rid
                        stack.append((nj, ni))
        cj = np.array([c[0] for c in cells])
        ci = np.array([c[1] for c in cells])
        j0, j1, i0, i1 = int(cj.min()), int(cj.max()), int(ci.min()), int(ci.max())
        sub = np.where(
            (label[j0:j1 + 1, i0:i1 + 1] == rid) & mask[j0:j1 + 1, i0:i1 + 1],
            grid.need[j0:j1 + 1, i0:i1 + 1],
            0,
        ).astype(np.int16)
        if sub.any():
            regions.append(Region(len(regions), j0, j1, i0, i1, sub))
    return _merge_overlapping(regions, min_zone_rows) if merge_overlapping else regions


def _merge_overlapping(regions: list[Region], min_rows: int = 1) -> list[Region]:
    """Fuse regions whose zones could land on top of each other.

    Two blobs with overlapping boxes would be given independent strip partitions,
    and their zone rectangles could then collide.  Solving them together keeps one
    common set of strips, so the zones stay side by side.  A blob shorter than the
    minimum zone width grows to that width, so it is compared at that size.
    """
    def rows(r: Region) -> tuple[float, float]:
        pad = max(0.0, (min_rows - (r.j1 - r.j0 + 1)) / 2.0)
        return r.j0 - pad, r.j1 + pad

    merged = True
    while merged:
        merged = False
        for a in range(len(regions)):
            for b in range(a + 1, len(regions)):
                ra, rb = regions[a], regions[b]
                (aj0, aj1), (bj0, bj1) = rows(ra), rows(rb)
                if aj1 < bj0 or bj1 < aj0 or ra.i1 < rb.i0 or rb.i1 < ra.i0:
                    continue
                j0, j1 = min(ra.j0, rb.j0), max(ra.j1, rb.j1)
                i0, i1 = min(ra.i0, rb.i0), max(ra.i1, rb.i1)
                need = np.zeros((j1 - j0 + 1, i1 - i0 + 1), dtype=np.int16)
                for r in (ra, rb):
                    view = need[r.j0 - j0:r.j1 - j0 + 1, r.i0 - i0:r.i1 - i0 + 1]
                    np.maximum(view, r.need, out=view)
                regions = (
                    [r for k, r in enumerate(regions) if k not in (a, b)]
                    + [Region(0, j0, j1, i0, i1, need)]
                )
                merged = True
                break
            if merged:
                break
    return [Region(k, r.j0, r.j1, r.i0, r.i1, r.need) for k, r in enumerate(regions)]
