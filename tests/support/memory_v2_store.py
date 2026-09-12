"""In-memory double for :class:`rebar_service.v2.store.V2Store`.

Implements the whole V2Store contract over plain dicts so pipeline/worker tests
run without PostgreSQL. Values are deep-copied on the way in and on the way out,
so a test that mutates what it stored (or what it read) cannot corrupt the store.
``schedule_lock`` yields ``None``: every method accepts ``conn=None`` and ignores
it, which is exactly how a caller uses the real store inside the lock.
"""

from __future__ import annotations

import copy
import time
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Sequence

_LIVE_N_STATES = ("pending", "preparing", "solving", "fitting", "bars")

# Same sentinel semantics as V2Store.set_n: omitted keeps, explicit None clears.
_UNSET: Any = object()


def _clone(value: Any) -> Any:
    return copy.deepcopy(value)


class MemoryV2Store:
    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.ns: dict[str, dict[int, dict[str, Any]]] = {}
        self.artifacts: dict[tuple[str, str], Any] = {}
        self.bar_tasks: dict[str, dict[str, Any]] = {}
        self.verification_tasks: dict[str, dict[str, Any]] = {}
        self.locks: list[str] = []

    # ---------- tasks ----------
    def create_task(
        self,
        task_id: str,
        *,
        scene_id: str,
        overlay_id: int,
        smooth: bool,
        config: Mapping[str, Any],
        ns: Sequence[int],
        conn: Any = None,
    ) -> None:
        requested = list(dict.fromkeys(int(n) for n in ns))
        invalid = [n for n in requested if n <= 0]
        if invalid:
            raise ValueError(f"N должен быть положительным: {invalid}")
        now = time.time()
        self.tasks[task_id] = {
            "task_id": task_id,
            "scene_id": scene_id,
            "overlay_id": int(overlay_id or 0),
            "smooth": bool(smooth),
            "config": _clone(dict(config or {})),
            "state": "created",
            "error": None,
            "max_useful_n": None,
            "min_useful_n": None,
            "prepare_info": None,
            "cancelled": False,
            "created_at": now,
            "updated_at": now,
        }
        rows = self.ns.setdefault(task_id, {})
        for n in requested:
            rows.setdefault(n, self._new_n_row(n, now))

    @staticmethod
    def _new_n_row(n: int, now: float) -> dict[str, Any]:
        return {
            "n": int(n),
            "state": "pending",
            "status": None,
            "fun": None,
            "mass_metrics": None,
            "result": None,
            "error": None,
            "updated_at": now,
        }

    def get_task(self, task_id: str, *, conn: Any = None) -> dict[str, Any] | None:
        row = self.tasks.get(task_id)
        return None if row is None else _clone(row)

    def set_task(self, task_id: str, *, conn: Any = None, **fields: Any) -> None:
        allowed = {"state", "error", "max_useful_n", "min_useful_n", "cancelled", "prepare_info"}
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise ValueError(f"неизвестные поля задачи v2: {unknown}")
        row = self.tasks.get(task_id)
        if row is None or not fields:
            return
        for name, value in fields.items():
            row[name] = _clone(value)
        row["updated_at"] = time.time()

    # ---------- per-N rows ----------
    def list_ns(self, task_id: str, *, conn: Any = None) -> list[dict[str, Any]]:
        rows = self.ns.get(task_id, {})
        return [
            {key: _clone(value) for key, value in rows[n].items() if key != "result"}
            for n in sorted(rows)
        ]

    def get_n(self, task_id: str, n: int, *, conn: Any = None) -> dict[str, Any] | None:
        row = self.ns.get(task_id, {}).get(int(n))
        return None if row is None else _clone(row)

    def set_n(
        self,
        task_id: str,
        n: int,
        *,
        state: Any = _UNSET,
        status: Any = _UNSET,
        fun: Any = _UNSET,
        mass_metrics: Any = _UNSET,
        result: Any = _UNSET,
        error: Any = _UNSET,
        conn: Any = None,
    ) -> None:
        # Mirrors V2Store.set_n: an omitted field is left alone, an explicit None clears it.
        if state is None:
            raise ValueError("state не может быть NULL")
        row = self.ns.get(task_id, {}).get(int(n))
        if row is None:
            return
        given = {
            "state": state,
            "status": status,
            "fun": fun,
            "mass_metrics": mass_metrics,
            "result": result,
            "error": error,
        }
        for key, value in given.items():
            if value is not _UNSET:
                row[key] = _clone(value)
        row["updated_at"] = time.time()

    def add_ns(self, task_id: str, ns: Sequence[int], *, conn: Any = None) -> list[int]:
        requested = list(dict.fromkeys(int(n) for n in ns))
        invalid = [n for n in requested if n <= 0]
        if invalid:
            raise ValueError(f"N должен быть положительным: {invalid}")
        rows = self.ns.setdefault(task_id, {})
        now = time.time()
        added: list[int] = []
        for n in requested:
            if n in rows:
                continue
            rows[n] = self._new_n_row(n, now)
            added.append(n)
        return added

    @contextmanager
    def schedule_lock(self, task_id: str) -> Iterator[None]:
        self.locks.append(task_id)
        yield None

    def ns_in_states(self, task_id: str, states: Sequence[str], conn: Any = None) -> list[int]:
        wanted = {str(state) for state in states}
        rows = self.ns.get(task_id, {})
        return sorted(n for n, row in rows.items() if row["state"] in wanted)

    def cancel(self, task_id: str, ns: Sequence[int] | None, *, conn: Any = None) -> list[int]:
        rows = self.ns.get(task_id, {})
        if ns is None:
            selected = sorted(rows)
        else:
            selected = sorted(set(int(n) for n in ns) & set(rows))
            if not selected:
                return []
        cancelled = [n for n in selected if rows[n]["state"] in _LIVE_N_STATES]
        now = time.time()
        for n in cancelled:
            rows[n]["state"] = "cancelled"
            rows[n]["updated_at"] = now
        task = self.tasks.get(task_id)
        if task is not None:
            if ns is None or (rows and all(row["state"] == "cancelled" for row in rows.values())):
                task["cancelled"] = True
                task["updated_at"] = now
        return cancelled

    def is_cancelled(self, task_id: str, n: int, *, conn: Any = None) -> bool:
        task = self.tasks.get(task_id)
        if task is None:
            return False
        if bool(task.get("cancelled")):
            return True
        row = self.ns.get(task_id, {}).get(int(n))
        return bool(row is not None and row["state"] == "cancelled")

    # ---------- artifacts ----------
    def save_artifact(self, task_id: str, key: str, value: Any, *, conn: Any = None) -> None:
        self.artifacts[(task_id, str(key))] = _clone(value)

    def load_artifact(self, task_id: str, key: str, *, conn: Any = None) -> Any | None:
        if (task_id, str(key)) not in self.artifacts:
            return None
        return _clone(self.artifacts[(task_id, str(key))])

    def delete_artifact(self, task_id: str, key: str, *, conn: Any = None) -> None:
        self.artifacts.pop((task_id, str(key)), None)

    # ---------- isolated worker tasks ----------
    @staticmethod
    def _worker_row(
        task_id: str,
        scene_id: str,
        overlay_id: int,
        smooth: bool,
        config: Mapping[str, Any],
        zones: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        now = time.time()
        return {
            "task_id": task_id,
            "scene_id": scene_id,
            "overlay_id": int(overlay_id or 0),
            "smooth": bool(smooth),
            "config": _clone(dict(config or {})),
            "zones": _clone([dict(zone) for zone in (zones or [])]),
            "state": "pending",
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }

    def create_bar_task(
        self, task_id: str, *, scene_id: str, overlay_id: int, smooth: bool,
        config: Mapping[str, Any], zones: Sequence[Mapping[str, Any]], conn: Any = None,
    ) -> None:
        self.bar_tasks[task_id] = self._worker_row(task_id, scene_id, overlay_id, smooth, config, zones)

    def get_bar_task(self, task_id: str, *, conn: Any = None) -> dict[str, Any] | None:
        row = self.bar_tasks.get(task_id)
        return None if row is None else _clone(row)

    def set_bar_task(
        self, task_id: str, *, state: str, result: Any = None, error: Any = None, conn: Any = None
    ) -> None:
        self._set_worker_row(self.bar_tasks, task_id, state, result, error)

    def create_verification_task(
        self, task_id: str, *, scene_id: str, overlay_id: int, smooth: bool,
        config: Mapping[str, Any], zones: Sequence[Mapping[str, Any]], conn: Any = None,
    ) -> None:
        self.verification_tasks[task_id] = self._worker_row(
            task_id, scene_id, overlay_id, smooth, config, zones
        )

    def get_verification_task(self, task_id: str, *, conn: Any = None) -> dict[str, Any] | None:
        row = self.verification_tasks.get(task_id)
        return None if row is None else _clone(row)

    def set_verification_task(
        self, task_id: str, *, state: str, result: Any = None, error: Any = None, conn: Any = None
    ) -> None:
        self._set_worker_row(self.verification_tasks, task_id, state, result, error)

    @staticmethod
    def _set_worker_row(
        table: dict[str, dict[str, Any]], task_id: str, state: str, result: Any, error: Any
    ) -> None:
        row = table.get(task_id)
        if row is None:
            return
        row["state"] = str(state)
        row["result"] = _clone(result)
        row["error"] = None if error is None else str(error)
        row["updated_at"] = time.time()
