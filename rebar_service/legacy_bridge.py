from __future__ import annotations

import importlib
import inspect
from typing import Any, Mapping, Optional

from .result_adapter import to_legacy_result


def _call(obj: Any, names: tuple[str, ...], variants: tuple[tuple[Any, ...], ...], kwargs: Optional[dict] = None):
    kwargs = kwargs or {}
    for name in names:
        fn = getattr(obj, name, None)
        if not callable(fn):
            continue
        for args in variants:
            try:
                value = fn(*args, **kwargs)
                return value
            except TypeError:
                continue
            except Exception:
                return None
    return None


class LegacyBridge:
    """Best-effort adapter; failure here never invalidates component results."""

    def __init__(self, store: Any = None) -> None:
        self.store = store or self._discover_store()

    @staticmethod
    def _discover_store() -> Any:
        try:
            module = importlib.import_module("rebar_service.store")
        except Exception:
            return None
        for name in ("store", "STORE", "task_store", "redis_store"):
            value = getattr(module, name, None)
            if value is not None:
                return value
        for name in ("get_store", "create_store", "build_store"):
            fn = getattr(module, name, None)
            if callable(fn):
                try:
                    return fn()
                except Exception:
                    pass
        return module

    def load_task(self, task_id: str) -> Any:
        if self.store is None:
            return None
        return _call(
            self.store,
            ("get_task", "load_task", "task", "get_task_data", "load"),
            ((task_id,),),
        )

    def save_result(self, task_id: str, total_n: int, solution: Mapping[str, Any]) -> bool:
        if self.store is None:
            return False
        result = to_legacy_result(solution)
        value = _call(
            self.store,
            ("save_result", "set_result", "put_result", "store_result"),
            (
                (task_id, int(total_n), result),
                (task_id, result, int(total_n)),
                (task_id, int(total_n), result, True),
            ),
        )
        return value is not None

    def publish(self, task_id: str, event: Mapping[str, Any]) -> bool:
        if self.store is None:
            return False
        value = _call(
            self.store,
            ("publish_event", "emit_event", "add_event", "publish"),
            ((task_id, dict(event)), (dict(event), task_id)),
        )
        return value is not None

    def update_task(self, task_id: str, **changes: Any) -> bool:
        if self.store is None:
            return False
        value = _call(
            self.store,
            ("update_task", "patch_task", "set_task_state"),
            ((task_id, changes), (task_id,)),
            kwargs=changes,
        )
        return value is not None
