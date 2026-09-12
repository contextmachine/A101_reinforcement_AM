"""Vendored subset of the A101_reinforcement experiments (contextmachine/A101_reinforcement).

Snapshot: commit 3d7b914 plus the author's uncommitted edits to experiments/agglo.py, taken on
2026-09-12. Only the modules the alternative solver needs are vendored (catalog, dxf_io, grid and
experiments/agglo.py, experiments/ilp.py); update by copying the files again.
"""
from .catalog import Background, BandMapping, RebarOption, ladder_options, map_bands, resolve_background
from .dxf_io import Mosaic, read_mosaic
from .grid import RequirementGrid, build_grid, denoise_bands
