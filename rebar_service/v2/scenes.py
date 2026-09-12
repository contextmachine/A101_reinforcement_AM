"""Scene-side helpers of the /v2 API.

Everything here is pure or takes the store as an explicit argument, so the whole
module is testable without FastAPI, Redis or PostgreSQL. The v2 router is the only
place that translates these results into HTTP.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Mapping, Sequence


class SceneNotFoundError(LookupError):
    """The requested scene (or overlay revision) does not exist."""


class SceneNotReadyError(RuntimeError):
    """The scene exists but its polygons are not materialised yet."""


# Stored/internal vocabulary ``active | background_only | removed`` is translated to the
# wire vocabulary ``active | real | empty`` here and nowhere else.
_TO_WIRE = {"active": "active", "background_only": "real", "removed": "empty"}
_FROM_WIRE = {"active": "active", "real": "background_only", "empty": "removed"}


def to_wire_overlay_state(value: str | None) -> str:
    """Map one stored overlay state to its /v2 wire spelling."""

    key = str(value or "active")
    try:
        return _TO_WIRE[key]
    except KeyError:
        raise ValueError(f"неизвестное состояние overlay: {value}") from None


def from_wire_overlay_state(value: str | None) -> str:
    """Map one /v2 wire overlay state back to the stored vocabulary."""

    key = str(value or "active")
    try:
        return _FROM_WIRE[key]
    except KeyError:
        raise ValueError(f"неизвестное состояние overlay: {value}") from None


# ------------------------------------------------------------------ polygons


def polygon_to_wire(row: Mapping[str, Any], index: int | None = None) -> dict[str, Any]:
    """Convert one resolved scene polygon row into an ``FEPolygonOut`` dictionary."""

    color = row.get("color")
    source_index = row.get("source_index")
    if source_index is None:
        source_index = index if index is not None else 0
    return {
        "load": float(row.get("load", 0.0) or 0.0),
        "color": None if color is None else int(color),
        "points": [[float(point[0]), float(point[1])] for point in row.get("points", []) or []],
        "overlay_state": to_wire_overlay_state(row.get("overlay_state")),
        "source_index": int(source_index),
    }


def polygons_to_wire(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Convert the resolved polygon list of a scene into the /v2 response body."""

    return [polygon_to_wire(row, index) for index, row in enumerate(rows)]


def scene_variant(smooth: bool) -> str:
    """Name of the stored polygon variant for the ``smooth`` flag."""

    return "smooth" if smooth else "raw"


def require_ready_scene(store: Any, scene_id: str) -> dict[str, Any]:
    """Load a scene row, refusing missing (404) and unprepared (409) scenes."""

    scene = store.get_scene(scene_id)
    if scene is None:
        raise SceneNotFoundError(f"Сцена {scene_id} не найдена")
    if str(scene.get("state")) != "ready":
        raise SceneNotReadyError(f"Сцена {scene_id} ещё не готова")
    return dict(scene)


def resolved_polygons(
    store: Any,
    scene_id: str,
    *,
    smooth: bool = False,
    overlay_id: int | str | None = 0,
) -> list[dict[str, Any]]:
    """Return the ``FEPolygonOut`` rows of one scene revision."""

    require_ready_scene(store, scene_id)
    resolved = store.resolve_scene_overlay_id(scene_id, overlay_id)
    rows = store.resolved_scene_polygons(
        scene_id, variant=scene_variant(smooth), overlay_id=resolved
    )
    return polygons_to_wire(rows)


# ------------------------------------------------------------------- uploads


def dxf_input_obj(filename: str | None, content: bytes) -> dict[str, Any]:
    """Build the stored source object of ``POST /v2/dxf_upload``."""

    return {"kind": "dxf", "filename": filename or "input.dxf", "content": bytes(content)}


def json_input_obj(
    polygons: Sequence[Mapping[str, Any]],
    *,
    filename: str = "polygons.json",
) -> dict[str, Any]:
    """Build the stored source object of ``POST /v2/json_upload``.

    The body is a bare ``FEPolygon[]`` array. It is re-encoded verbatim (``color``
    included) so that ``Store.create_scene`` parses it through the very same
    ``source_polygons_from_json_bytes`` path as the v1 file upload.
    """

    body: list[dict[str, Any]] = []
    for index, polygon in enumerate(polygons):
        points = [[float(point[0]), float(point[1])] for point in polygon.get("points", []) or []]
        if len(points) < 3:
            raise ValueError(f"полигон #{index} должен содержать не менее 3 точек")
        row: dict[str, Any] = {"points": points, "load": float(polygon.get("load", 0.0))}
        if polygon.get("color") is not None:
            row["color"] = int(polygon["color"])
        body.append(row)
    if not body:
        raise ValueError("список полигонов не должен быть пустым")
    content = json.dumps(body, ensure_ascii=False).encode("utf-8")
    return {"kind": "json", "filename": filename, "content": content}


def tables_input_obj(
    bundle: bytes,
    *,
    load_column: int = 1,
    nodes_filename: str | None = None,
    elements_filename: str | None = None,
    loads_filename: str | None = None,
) -> dict[str, Any]:
    """Build the stored source object of ``POST /v2/tables_upload``."""

    return {
        "kind": "xlsx_tables",
        "filename": "tables.zip",
        "content": bytes(bundle),
        "load_column": int(load_column),
        "nodes_filename": nodes_filename or "nodes.xlsx",
        "elements_filename": elements_filename or "elements.xlsx",
        "loads_filename": loads_filename or "loads.xlsx",
    }


def create_scene(store: Any, input_obj: Mapping[str, Any]) -> dict[str, Any]:
    """Persist one ready reusable scene and return the ``SceneCreated`` body."""

    scene_id = uuid.uuid4().hex
    store.create_scene(scene_id, {"state": "ready", "created_at": time.time()}, dict(input_obj))
    return {"scene_id": scene_id, "state": "ready"}


# ------------------------------------------------------------------ overlays


def _ordered_events(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return sorted((dict(row) for row in rows), key=lambda row: int(row.get("seq", 0) or 0))


def overlay_to_wire(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project one stored overlay event row onto the ``OverlayOut`` contract."""

    time_value = row.get("time")
    return {
        "id": int(row.get("id", row.get("overlay_id", 0)) or 0),
        "type": str(row.get("type", row.get("event_type", ""))),
        "idxs": [int(idx) for idx in (row.get("idxs") or [])],
        "real": bool(row.get("real", False)),
        "time": None if time_value is None else time_value,
    }


def append_overlays(
    store: Any,
    scene_id: str,
    overlays: Sequence[Mapping[str, Any]],
) -> int:
    """Append overlay events to a scene and return the id of the last one.

    Ids are allocated by the store; already-masked / already-unmasked indices are
    filtered there, so an event with an empty effect is still stored and still gets
    an id.
    """

    events: list[dict[str, Any]] = []
    for overlay in overlays:
        events.append(
            {
                "type": str(overlay.get("type", "")),
                "idxs": [int(idx) for idx in (overlay.get("idxs") or [])],
                "real": bool(overlay.get("real", False)),
                "time": overlay.get("time"),
            }
        )
    if not events:
        raise ValueError("overlays не должен быть пустым")
    rows = store.append_scene_overlay_events(scene_id, events)
    ordered = _ordered_events(rows or [])
    if not ordered:
        raise ValueError("хранилище не вернуло ни одного события overlay")
    return int(ordered[-1].get("id", ordered[-1].get("overlay_id", 0)) or 0)


def overlay_log(store: Any, scene_id: str, selector: int | str | None = 0) -> list[dict[str, Any]]:
    """Return the overlay events of a scene up to and including one revision.

    ``selector`` follows the public rules: ``0`` is the base state (empty log), a
    positive value is an existing event id, ``-1`` is the last event, ``-2`` the one
    before it and so on.
    """

    target = int(store.resolve_scene_overlay_id(scene_id, selector))
    rows = _ordered_events(store.scene_overlay_events(scene_id) or [])
    if target == 0:
        return []
    target_seq = next(
        (
            int(row.get("seq", 0) or 0)
            for row in rows
            if int(row.get("id", row.get("overlay_id", -1)) or -1) == target
        ),
        None,
    )
    if target_seq is None:
        raise KeyError(f"overlay={target} not found")
    return [overlay_to_wire(row) for row in rows if int(row.get("seq", 0) or 0) <= target_seq]
