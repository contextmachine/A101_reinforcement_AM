from __future__ import annotations

import io
import math
import pickle
import pickletools
from typing import Any

import numpy as np
from shapely.geometry import Polygon


class UnsafePickleError(ValueError):
    pass


_FORBIDDEN_OPCODES = {
    "PERSID",
    "BINPERSID",
    "EXT1",
    "EXT2",
    "EXT4",
    "INST",
    "OBJ",
    "NEWOBJ",
    "NEWOBJ_EX",
}


class _RestrictedUnpickler(pickle.Unpickler):
    _ALLOWED = {
        ("numpy._core.multiarray", "_reconstruct"),
        ("numpy.core.multiarray", "_reconstruct"),
        ("numpy", "ndarray"),
        ("numpy", "dtype"),
        ("shapely.io", "from_wkb"),
    }

    def find_class(self, module: str, name: str):  # noqa: D401
        if (module, name) not in self._ALLOWED:
            raise UnsafePickleError(f"Unsupported pickle global: {module}.{name}")
        if module in {"numpy._core.multiarray", "numpy.core.multiarray"} and name == "_reconstruct":
            from numpy._core.multiarray import _reconstruct
            return _reconstruct
        if module == "numpy" and name == "ndarray":
            return np.ndarray
        if module == "numpy" and name == "dtype":
            return np.dtype
        if module == "shapely.io" and name == "from_wkb":
            from shapely.io import from_wkb

            return from_wkb
        raise UnsafePickleError(f"Unsupported pickle global: {module}.{name}")

    def persistent_load(self, pid):
        raise UnsafePickleError("Persistent pickle references are not supported")


def _validate_pickle_program(payload: bytes) -> None:
    try:
        for opcode, _arg, _pos in pickletools.genops(payload):
            if opcode.name in _FORBIDDEN_OPCODES:
                raise UnsafePickleError(f"Unsupported pickle opcode: {opcode.name}")
    except UnsafePickleError:
        raise
    except Exception as exc:
        raise UnsafePickleError(f"Invalid pickle stream: {exc}") from exc


def _points_from_row(row: dict[str, Any], index: int) -> list[list[float]]:
    points = row.get("points")
    geometry = row.get("geometry")
    if points is None and geometry is not None and hasattr(geometry, "exterior"):
        points = list(geometry.exterior.coords)[:-1]
    if points is None:
        raise UnsafePickleError(f"polygon #{index}: points/geometry missing")

    try:
        array = np.asarray(points, dtype=float)
    except Exception as exc:
        raise UnsafePickleError(f"polygon #{index}: points must be numeric") from exc
    if array.ndim != 2 or array.shape[1] < 2 or array.shape[0] < 3:
        raise UnsafePickleError(f"polygon #{index}: points must have shape Nx2 with N>=3")
    array = array[:, :2]
    if not np.isfinite(array).all():
        raise UnsafePickleError(f"polygon #{index}: points contain non-finite values")
    if len({(float(x), float(y)) for x, y in array}) < 3:
        raise UnsafePickleError(f"polygon #{index}: fewer than 3 unique points")

    polygon = Polygon(array)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    if polygon.is_empty or polygon.area <= 0:
        raise UnsafePickleError(f"polygon #{index}: zero/invalid area")

    if geometry is not None:
        if not hasattr(geometry, "geom_type"):
            raise UnsafePickleError(f"polygon #{index}: geometry is not a Shapely geometry")
        if geometry.geom_type != "Polygon" or geometry.is_empty or geometry.area <= 0:
            raise UnsafePickleError(f"polygon #{index}: geometry must be a non-empty Polygon")

    return [[float(x), float(y)] for x, y in array]


def load_source_polygons_pickle(payload: bytes, *, max_polygons: int = 100_000) -> dict[str, Any]:
    """Load the supported NumPy/Shapely polygon pickle without arbitrary globals."""
    if not isinstance(payload, (bytes, bytearray, memoryview)) or not payload:
        raise UnsafePickleError("Pickle payload is empty")
    raw = bytes(payload)
    _validate_pickle_program(raw)
    try:
        value = _RestrictedUnpickler(io.BytesIO(raw)).load()
    except UnsafePickleError:
        raise
    except Exception as exc:
        raise UnsafePickleError(f"Failed to decode supported pickle: {exc}") from exc

    if isinstance(value, dict) and "polygons" in value:
        rows = value.get("polygons")
    else:
        rows = value
    if not isinstance(rows, (list, tuple)):
        raise UnsafePickleError("Pickle top level must be a list of polygons")
    if len(rows) > int(max_polygons):
        raise UnsafePickleError(f"Too many polygons: {len(rows)} > {int(max_polygons)}")

    polygons: list[dict[str, Any]] = []
    for index, raw_row in enumerate(rows):
        if not isinstance(raw_row, dict):
            raise UnsafePickleError(f"polygon #{index}: expected dict")
        try:
            load = float(raw_row["load"])
        except (KeyError, TypeError, ValueError) as exc:
            raise UnsafePickleError(f"polygon #{index}: load must be numeric") from exc
        if not math.isfinite(load):
            raise UnsafePickleError(f"polygon #{index}: load must be finite")
        item: dict[str, Any] = {
            "points": _points_from_row(raw_row, index),
            "load": load,
        }
        if raw_row.get("color") is not None:
            try:
                item["color"] = int(raw_row["color"])
            except (TypeError, ValueError) as exc:
                raise UnsafePickleError(f"polygon #{index}: color must be an integer") from exc
        polygons.append(item)
    if not polygons:
        raise UnsafePickleError("Pickle contains no polygons")
    return {"kind": "polygons", "units": "mm", "polygons": polygons}
