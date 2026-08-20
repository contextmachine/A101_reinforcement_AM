from __future__ import annotations

from typing import Any, Mapping

from A101.read_dxf import extract_polygons_from_bytes


class SourcePolygonsError(ValueError):
    pass


def source_polygons_from_input(source_input: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the original DXF mosaic in a JSON-serializable form."""
    if not isinstance(source_input, Mapping) or source_input.get("kind") != "dxf":
        raise SourcePolygonsError("Source polygons are available only for DXF tasks")

    content = source_input.get("content")
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise SourcePolygonsError("DXF source content is missing")

    try:
        mosaic = extract_polygons_from_bytes(bytes(content))
    except Exception as exc:
        raise SourcePolygonsError(f"Failed to parse source DXF: {exc}") from exc

    return [
        {
            "points": [[float(x), float(y)] for x, y in poly["points"]],
            "color": int(poly["color"]),
            "load": float(poly["load"]),
        }
        for poly in mosaic
    ]
