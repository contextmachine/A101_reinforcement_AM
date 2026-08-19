from __future__ import annotations

import dataclasses
import json
import math
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if dataclasses.is_dataclass(value):
        return to_jsonable(dataclasses.asdict(value))
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (Path, Enum)):
        return str(value.value if isinstance(value, Enum) else value)
    if hasattr(value, "model_dump"):
        return to_jsonable(value.model_dump(mode="python"))
    if hasattr(value, "tolist"):
        return to_jsonable(value.tolist())
    if hasattr(value, "item"):
        try:
            return to_jsonable(value.item())
        except Exception:
            pass
    return str(value)


def dumps(value: Any) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def loads(value: str | bytes | None, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
