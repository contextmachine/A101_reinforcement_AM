from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

from .jsonutil import dumps

V2_STAGES = frozenset({"preparing", "solving", "fitting", "baring", "validation"})
_STAGE_KINDS = {
    "preparing": frozenset({"prepare"}),
    "solving": frozenset({"solve"}),
    "fitting": frozenset({"fit"}),
    "baring": frozenset({"task_bars", "bars_request"}),
    "validation": frozenset({"verification"}),
}


@dataclass(frozen=True)
class V2Job:
    stage: str
    kind: str
    task_id: str
    n: int | None = None
    attempt: int = 1
    request_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    dedupe_key: str | None = None
    job_id: str | None = None
    created_at: float | None = None
    schema_version: int = 2

    def __post_init__(self) -> None:
        stage = str(self.stage).lower()
        kind = str(self.kind)
        if stage not in V2_STAGES:
            raise ValueError(f"Unknown v2 stage: {stage}")
        if kind not in _STAGE_KINDS[stage]:
            raise ValueError(f"Job kind {kind!r} does not belong to stage {stage!r}")
        if int(self.attempt) < 1:
            raise ValueError("attempt must be >= 1")
        if self.n is not None and int(self.n) < 1:
            raise ValueError("n must be >= 1")
        object.__setattr__(self, "stage", stage)
        coordinate = {
            "stage": stage,
            "kind": kind,
            "task_id": str(self.task_id),
            "n": None if self.n is None else int(self.n),
            "attempt": int(self.attempt),
            "request_id": self.request_id,
            "payload": self.payload,
        }
        dedupe = self.dedupe_key or f"v2:{dumps(coordinate)}"
        object.__setattr__(self, "dedupe_key", dedupe)
        object.__setattr__(self, "job_id", self.job_id or uuid5(NAMESPACE_URL, dedupe).hex)
        object.__setattr__(self, "created_at", float(self.created_at or time.time()))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_value(cls, value: Mapping[str, Any]) -> "V2Job":
        allowed = {
            "stage", "kind", "task_id", "n", "attempt", "request_id", "payload",
            "dedupe_key", "job_id", "created_at", "schema_version",
        }
        row = dict(value)
        return cls(**{key: row[key] for key in allowed if key in row})
