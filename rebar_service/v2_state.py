from __future__ import annotations

from typing import Literal

V2State = Literal[
    "pending", "preparing", "solving", "fitting", "baring",
    "error", "success", "cancelled",
]

V2_STATES = frozenset({
    "pending", "preparing", "solving", "fitting", "baring",
    "error", "success", "cancelled",
})
_ACTIVE_OR_DONE = frozenset({"pending", "preparing", "solving", "fitting", "baring", "success"})
_RETRYABLE = frozenset({"error", "cancelled"})


def normalize_v2_state(value: str) -> V2State:
    state = str(value)
    if state not in V2_STATES:
        raise ValueError(f"Unknown v2 state: {value}")
    return state  # type: ignore[return-value]


def next_n_action(state: str | None) -> Literal["create", "keep", "retry"]:
    if state is None:
        return "create"
    normalized = normalize_v2_state(state)
    if normalized in _ACTIVE_OR_DONE:
        return "keep"
    if normalized in _RETRYABLE:
        return "retry"
    raise AssertionError(normalized)
