from __future__ import annotations

from typing import Any, Mapping, Sequence


def normalize_overlay_id(value: int | str | None) -> int:
    """Normalize public overlay selector; 0 means the unmodified base analysis."""

    if value in (None, ""):
        return 0
    overlay_id = int(value)
    if overlay_id < 0:
        raise ValueError("overlay must be a non-negative integer")
    return overlay_id


def resolve_overlay(
    polygons: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    through_id: int | str | None = 0,
) -> list[dict[str, Any]]:
    """Return stable source polygons annotated with their state at one overlay revision.

    Event order is append order (`seq`), not numeric overlay id order. The requested
    event itself is included. Source polygon rows are never removed or renumbered.
    """

    overlay_id = normalize_overlay_id(through_id)
    ordered = sorted((dict(row) for row in events), key=lambda row: int(row.get("seq", 0)))
    if overlay_id == 0:
        selected: list[dict[str, Any]] = []
    else:
        target_seq = next(
            (int(row.get("seq", 0)) for row in ordered if int(row.get("id", row.get("overlay_id", -1))) == overlay_id),
            None,
        )
        if target_seq is None:
            raise KeyError(f"overlay={overlay_id} not found")
        selected = [row for row in ordered if int(row.get("seq", 0)) <= target_seq]

    state: list[dict[str, Any]] = [
        {"overlay_state": "active", "active": True, "real": False}
        for _ in polygons
    ]
    for event in selected:
        event_type = str(event.get("type", "")).lower()
        if event_type not in {"clean", "unclean"}:
            raise ValueError(f"unknown overlay event type: {event_type}")
        real = bool(event.get("real", False))
        for raw_idx in event.get("idxs", []) or []:
            idx = int(raw_idx)
            if idx < 0 or idx >= len(polygons):
                raise IndexError(f"source polygon index out of range: {idx}")
            if event_type == "unclean":
                state[idx] = {"overlay_state": "active", "active": True, "real": False}
            elif real:
                state[idx] = {"overlay_state": "background_only", "active": False, "real": True}
            else:
                state[idx] = {"overlay_state": "removed", "active": False, "real": False}

    result: list[dict[str, Any]] = []
    for idx, polygon in enumerate(polygons):
        result.append({**dict(polygon), **state[idx], "source_index": idx})
    return result
