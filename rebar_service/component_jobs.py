from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Mapping, Optional
from uuid import NAMESPACE_URL, uuid5


class JobKind(str, Enum):
    prepare_field = "prepare_field"
    prepare_component = "prepare_component"
    solve_component = "solve_component"
    fit_component = "fit_component"
    combine_frontiers = "combine_frontiers"
    layout_solution = "layout_solution"
    validate_solution = "validate_solution"
    prepare_whole = "prepare_whole"
    solve_whole = "solve_whole"
    fit_whole = "fit_whole"


def stable_digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class JobEnvelope:
    kind: str
    task_id: str
    payload: Dict[str, Any] = field(default_factory=dict)
    generation: int = 0
    dedupe_key: Optional[str] = None
    job_id: Optional[str] = None
    created_at: Optional[str] = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        kind = self.kind.value if isinstance(self.kind, JobKind) else str(self.kind)
        object.__setattr__(self, "kind", kind)
        dedupe = self.dedupe_key or self.default_dedupe_key()
        object.__setattr__(self, "dedupe_key", dedupe)
        object.__setattr__(self, "job_id", self.job_id or str(uuid5(NAMESPACE_URL, dedupe)))
        object.__setattr__(self, "created_at", self.created_at or datetime.now(timezone.utc).isoformat())

    def default_dedupe_key(self) -> str:
        coordinate = {
            key: self.payload.get(key)
            for key in ("component_id", "n", "total_n", "solution_id", "source", "frontier_version")
            if key in self.payload
        }
        if not coordinate:
            coordinate = {"payload": stable_digest(self.payload)}
        return "%s:%s:%s:%s" % (
            self.kind,
            self.task_id,
            int(self.generation),
            stable_digest(coordinate),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), default=str)

    @classmethod
    def from_value(cls, value: Any) -> "JobEnvelope":
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, Mapping):
            raise ValueError("job должен быть JSON-объектом")
        return cls(**dict(value))
