"""PostgreSQL access layer for the /v2 whole-field pipeline.

Mirrors :mod:`rebar_service.postgres_store`: raw SQLAlchemy ``text()`` against the
``v2_*`` tables created by migration ``0005_v2_tasks``, JSON through
``_json_param``/``_json_value`` and binary artifacts through
``rebar_service.codec`` with a sha256 integrity check on load.

Every task/N method accepts an optional ``conn``. When it is given the statement
runs on that connection instead of opening its own transaction, so a caller
holding :meth:`V2Store.schedule_lock` can read and write inside the lock.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Sequence

from sqlalchemy import text

from ..codec import decode_object, encode_object, sha256
from ..database import Database
from ..postgres_store import _epoch, _json_param, _json_value, json_safe_value

# Distinguishes "field omitted" from "field explicitly set to NULL".
_UNSET: Any = object()

# States a task N can still leave on its own; cancellation only touches these.
_LIVE_N_STATES = ("pending", "preparing", "solving", "fitting", "bars")

_TASK_FIELDS = {
    "state": "state=:state",
    "error": "error=:error",
    "max_useful_n": "max_useful_n=:max_useful_n",
    "min_useful_n": "min_useful_n=:min_useful_n",
    "cancelled": "cancelled=:cancelled",
    "prepare_info": "prepare_info=CAST(:prepare_info AS jsonb)",
}

_JSON_TASK_FIELDS = {"prepare_info"}


def _jsonb(value: Any) -> str | None:
    """JSON text for a nullable jsonb parameter (``None`` stays SQL NULL)."""

    return None if value is None else _json_param(value)


class V2Store:
    """Durable storage for v2 tasks, per-N rows, artifacts and worker tasks."""

    def __init__(self, database: Database, *, artifact_backend: Any = None):
        self.database = database
        # Optional FileArtifactBackend: bytes go to a shared directory, Postgres keeps a reference.
        self.artifact_backend = artifact_backend

    # ---------- connection helpers ----------
    @contextmanager
    def _write(self, conn: Any = None) -> Iterator[Any]:
        if conn is not None:
            yield conn
            return
        with self.database.begin() as owned:
            yield owned

    @contextmanager
    def _read(self, conn: Any = None) -> Iterator[Any]:
        if conn is not None:
            yield conn
            return
        with self.database.connect() as owned:
            yield owned

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
        with self._write(conn) as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO v2_tasks (id, scene_id, overlay_id, smooth, config, state)
                    VALUES (:task_id, :scene_id, :overlay_id, :smooth, CAST(:config AS jsonb), 'created')
                    """
                ),
                {
                    "task_id": task_id,
                    "scene_id": scene_id,
                    "overlay_id": int(overlay_id or 0),
                    "smooth": bool(smooth),
                    "config": _json_param(dict(config or {})),
                },
            )
            for n in requested:
                connection.execute(
                    text(
                        """
                        INSERT INTO v2_task_ns (task_id, n, state)
                        VALUES (:task_id, :n, 'pending')
                        ON CONFLICT (task_id, n) DO NOTHING
                        """
                    ),
                    {"task_id": task_id, "n": n},
                )

    def get_task(self, task_id: str, *, conn: Any = None) -> dict[str, Any] | None:
        with self._read(conn) as connection:
            row = connection.execute(
                text(
                    """
                    SELECT id, scene_id, overlay_id, smooth, config, state, error,
                           max_useful_n, min_useful_n, prepare_info, cancelled, created_at, updated_at
                    FROM v2_tasks WHERE id=:task_id
                    """
                ),
                {"task_id": task_id},
            ).mappings().first()
        if row is None:
            return None
        return {
            "task_id": str(row["id"]),
            "scene_id": str(row["scene_id"]),
            "overlay_id": int(row["overlay_id"] or 0),
            "smooth": bool(row["smooth"]),
            "config": _json_value(row["config"], {}) or {},
            "state": str(row["state"]),
            "error": row["error"],
            "max_useful_n": None if row["max_useful_n"] is None else int(row["max_useful_n"]),
            "min_useful_n": None if row["min_useful_n"] is None else int(row["min_useful_n"]),
            "prepare_info": _json_value(row["prepare_info"], None),
            "cancelled": bool(row["cancelled"]),
            "created_at": _epoch(row["created_at"]),
            "updated_at": _epoch(row["updated_at"]),
        }

    def set_task(self, task_id: str, *, conn: Any = None, **fields: Any) -> None:
        unknown = sorted(set(fields) - set(_TASK_FIELDS))
        if unknown:
            raise ValueError(f"неизвестные поля задачи v2: {unknown}")
        if not fields:
            return
        assignments = [_TASK_FIELDS[name] for name in fields]
        params: dict[str, Any] = {"task_id": task_id}
        for name, value in fields.items():
            if name in _JSON_TASK_FIELDS:
                params[name] = _jsonb(value)
            elif name == "cancelled":
                params[name] = bool(value)
            elif name in ("max_useful_n", "min_useful_n"):
                params[name] = None if value is None else int(value)
            else:
                params[name] = value
        with self._write(conn) as connection:
            connection.execute(
                text(
                    "UPDATE v2_tasks SET "
                    + ", ".join(assignments)
                    + ", updated_at=now() WHERE id=:task_id"
                ),
                params,
            )

    # ---------- per-N rows ----------
    @staticmethod
    def _n_row(row: Mapping[str, Any], *, with_result: bool) -> dict[str, Any]:
        value = {
            "n": int(row["n"]),
            "state": str(row["state"]),
            "status": None if row["status"] is None else str(row["status"]),
            "fun": None if row["fun"] is None else float(row["fun"]),
            "mass_metrics": _json_value(row["mass_metrics"], None),
            "error": row["error"],
            "updated_at": _epoch(row["updated_at"]),
        }
        if with_result:
            value["result"] = _json_value(row["result"], None)
        return value

    def list_ns(self, task_id: str, *, conn: Any = None) -> list[dict[str, Any]]:
        with self._read(conn) as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT n, state, status, fun, mass_metrics, error, updated_at
                    FROM v2_task_ns WHERE task_id=:task_id ORDER BY n
                    """
                ),
                {"task_id": task_id},
            ).mappings().all()
        return [self._n_row(row, with_result=False) for row in rows]

    def get_n(self, task_id: str, n: int, *, conn: Any = None) -> dict[str, Any] | None:
        with self._read(conn) as connection:
            row = connection.execute(
                text(
                    """
                    SELECT n, state, status, fun, mass_metrics, result, error, updated_at
                    FROM v2_task_ns WHERE task_id=:task_id AND n=:n
                    """
                ),
                {"task_id": task_id, "n": int(n)},
            ).mappings().first()
        if row is None:
            return None
        return self._n_row(row, with_result=True)

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
        assignments: list[str] = []
        params: dict[str, Any] = {"task_id": task_id, "n": int(n)}
        if state is not _UNSET:
            if state is None:
                raise ValueError("state не может быть NULL")
            assignments.append("state=:state")
            params["state"] = str(state)
        if status is not _UNSET:
            assignments.append("status=:status")
            params["status"] = None if status is None else str(status)
        if fun is not _UNSET:
            assignments.append("fun=:fun")
            params["fun"] = None if fun is None else float(fun)
        if mass_metrics is not _UNSET:
            assignments.append("mass_metrics=CAST(:mass_metrics AS jsonb)")
            params["mass_metrics"] = _jsonb(mass_metrics)
        if result is not _UNSET:
            assignments.append("result=CAST(:result AS jsonb)")
            params["result"] = _jsonb(result)
        if error is not _UNSET:
            assignments.append("error=:error")
            params["error"] = None if error is None else str(error)
        if not assignments:
            return
        with self._write(conn) as connection:
            connection.execute(
                text(
                    "UPDATE v2_task_ns SET "
                    + ", ".join(assignments)
                    + ", updated_at=now() WHERE task_id=:task_id AND n=:n"
                ),
                params,
            )

    def add_ns(self, task_id: str, ns: Sequence[int], *, conn: Any = None) -> list[int]:
        requested = list(dict.fromkeys(int(n) for n in ns))
        invalid = [n for n in requested if n <= 0]
        if invalid:
            raise ValueError(f"N должен быть положительным: {invalid}")
        if not requested:
            return []
        added: list[int] = []
        with self._write(conn) as connection:
            for n in requested:
                row = connection.execute(
                    text(
                        """
                        INSERT INTO v2_task_ns (task_id, n, state)
                        VALUES (:task_id, :n, 'pending')
                        ON CONFLICT (task_id, n) DO NOTHING
                        RETURNING n
                        """
                    ),
                    {"task_id": task_id, "n": n},
                ).mappings().first()
                if row is not None:
                    added.append(int(row["n"]))
        return added

    @contextmanager
    def schedule_lock(self, task_id: str) -> Iterator[Any]:
        """Serialise scheduling decisions for one task on its ``v2_tasks`` row."""

        with self.database.begin() as conn:
            conn.execute(text("SELECT id FROM v2_tasks WHERE id=:task_id FOR UPDATE"), {"task_id": task_id})
            yield conn

    def ns_in_states(self, task_id: str, states: Sequence[str], conn: Any = None) -> list[int]:
        wanted = [str(state) for state in states]
        if not wanted:
            return []
        with self._read(conn) as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT n FROM v2_task_ns
                    WHERE task_id=:task_id AND state = ANY(:states) ORDER BY n
                    """
                ),
                {"task_id": task_id, "states": wanted},
            ).mappings().all()
        return [int(row["n"]) for row in rows]

    def cancel(self, task_id: str, ns: Sequence[int] | None, *, conn: Any = None) -> list[int]:
        """Cancel the given N (or every live N) and return what was cancelled."""

        params: dict[str, Any] = {"task_id": task_id, "live": list(_LIVE_N_STATES)}
        clause = ""
        if ns is not None:
            requested = list(dict.fromkeys(int(n) for n in ns))
            if not requested:
                return []
            clause = " AND n = ANY(:ns)"
            params["ns"] = requested
        with self._write(conn) as connection:
            # Same lock order as schedule_lock(): task row first, then its N rows.
            connection.execute(text("SELECT id FROM v2_tasks WHERE id=:task_id FOR UPDATE"), {"task_id": task_id})
            rows = connection.execute(
                text(
                    "UPDATE v2_task_ns SET state='cancelled', updated_at=now() "
                    "WHERE task_id=:task_id AND state = ANY(:live)" + clause + " RETURNING n"
                ),
                params,
            ).mappings().all()
            cancelled = sorted(int(row["n"]) for row in rows)
            if ns is None:
                connection.execute(
                    text("UPDATE v2_tasks SET cancelled=true, updated_at=now() WHERE id=:task_id"),
                    {"task_id": task_id},
                )
            else:
                connection.execute(
                    text(
                        """
                        UPDATE v2_tasks SET cancelled=true, updated_at=now()
                        WHERE id=:task_id AND NOT EXISTS (
                            SELECT 1 FROM v2_task_ns WHERE task_id=:task_id AND state <> 'cancelled'
                        )
                        """
                    ),
                    {"task_id": task_id},
                )
        return cancelled

    def is_cancelled(self, task_id: str, n: int, *, conn: Any = None) -> bool:
        with self._read(conn) as connection:
            row = connection.execute(
                text(
                    """
                    SELECT (t.cancelled OR COALESCE(r.state, '') = 'cancelled') AS cancelled
                    FROM v2_tasks t LEFT JOIN v2_task_ns r ON r.task_id = t.id AND r.n = :n
                    WHERE t.id = :task_id
                    """
                ),
                {"task_id": task_id, "n": int(n)},
            ).mappings().first()
        return bool(row["cancelled"]) if row is not None else False

    # ---------- artifacts ----------
    FILE_CODEC = "fileref"

    def inline_artifact_keys(self, *, conn: Any = None) -> list[tuple[str, str]]:
        """(task_id, key) of artifacts still stored as bytea rows, largest first."""
        with self._read(conn) as connection:
            rows = connection.execute(
                text(
                    "SELECT task_id, key FROM v2_artifacts WHERE codec <> :codec "
                    "ORDER BY pg_column_size(payload) DESC"
                ),
                {"codec": self.FILE_CODEC},
            ).mappings().all()
        return [(str(row["task_id"]), str(row["key"])) for row in rows]

    def save_artifact(self, task_id: str, key: str, value: Any, *, conn: Any = None) -> None:
        payload, codec = encode_object(value)
        digest = sha256(payload)
        if self.artifact_backend is not None:
            relative = self.artifact_backend.write(task_id, str(key), payload)
            payload, codec = relative.encode("utf-8"), self.FILE_CODEC
        with self._write(conn) as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO v2_artifacts (task_id, key, codec, payload, sha256)
                    VALUES (:task_id, :key, :codec, :payload, :sha256)
                    ON CONFLICT (task_id, key) DO UPDATE SET
                        codec=EXCLUDED.codec, payload=EXCLUDED.payload,
                        sha256=EXCLUDED.sha256, updated_at=now()
                    """
                ),
                {"task_id": task_id, "key": str(key), "codec": codec, "payload": payload, "sha256": digest},
            )

    def load_artifact(self, task_id: str, key: str, *, conn: Any = None) -> Any | None:
        with self._read(conn) as connection:
            row = connection.execute(
                text("SELECT codec, payload, sha256 FROM v2_artifacts WHERE task_id=:task_id AND key=:key"),
                {"task_id": task_id, "key": str(key)},
            ).mappings().first()
        if row is None:
            return None
        payload = bytes(row["payload"])
        if str(row["codec"]) == self.FILE_CODEC:
            if self.artifact_backend is None:
                raise IOError(f"артефакт {key} хранится в файле, но REBAR_ARTIFACT_DIR не задан")
            payload = self.artifact_backend.read(payload.decode("utf-8"), str(row["sha256"]))
        elif sha256(payload) != str(row["sha256"]):
            raise IOError(f"артефакт {key} повреждён")
        return decode_object(payload)

    def delete_artifact(self, task_id: str, key: str, *, conn: Any = None) -> None:
        with self._write(conn) as connection:
            row = connection.execute(
                text(
                    "DELETE FROM v2_artifacts WHERE task_id=:task_id AND key=:key "
                    "RETURNING codec, payload, sha256"
                ),
                {"task_id": task_id, "key": str(key)},
            ).mappings().first()
        if row is not None and str(row["codec"]) == self.FILE_CODEC and self.artifact_backend is not None:
            self.artifact_backend.delete(bytes(row["payload"]).decode("utf-8"), str(row["sha256"]))

    # ---------- isolated worker tasks ----------
    def _create_worker_task(
        self,
        table: str,
        task_id: str,
        *,
        scene_id: str,
        overlay_id: int,
        smooth: bool,
        config: Mapping[str, Any],
        zones: Sequence[Mapping[str, Any]],
        conn: Any = None,
    ) -> None:
        with self._write(conn) as connection:
            connection.execute(
                text(
                    f"""
                    INSERT INTO {table} (id, scene_id, overlay_id, smooth, config, zones, state)
                    VALUES (:task_id, :scene_id, :overlay_id, :smooth,
                            CAST(:config AS jsonb), CAST(:zones AS jsonb), 'pending')
                    """
                ),
                {
                    "task_id": task_id,
                    "scene_id": scene_id,
                    "overlay_id": int(overlay_id or 0),
                    "smooth": bool(smooth),
                    "config": _json_param(dict(config or {})),
                    "zones": _json_param([json_safe_value(dict(zone)) for zone in (zones or [])]),
                },
            )

    def _get_worker_task(self, table: str, task_id: str, *, conn: Any = None) -> dict[str, Any] | None:
        with self._read(conn) as connection:
            row = connection.execute(
                text(
                    f"""
                    SELECT id, scene_id, overlay_id, smooth, config, zones, state, result, error,
                           created_at, updated_at
                    FROM {table} WHERE id=:task_id
                    """
                ),
                {"task_id": task_id},
            ).mappings().first()
        if row is None:
            return None
        return {
            "task_id": str(row["id"]),
            "scene_id": str(row["scene_id"]),
            "overlay_id": int(row["overlay_id"] or 0),
            "smooth": bool(row["smooth"]),
            "config": _json_value(row["config"], {}) or {},
            "zones": _json_value(row["zones"], []) or [],
            "state": str(row["state"]),
            "result": _json_value(row["result"], None),
            "error": row["error"],
            "created_at": _epoch(row["created_at"]),
            "updated_at": _epoch(row["updated_at"]),
        }

    def _set_worker_task(
        self,
        table: str,
        task_id: str,
        *,
        state: str,
        result: Any = None,
        error: Any = None,
        conn: Any = None,
    ) -> None:
        with self._write(conn) as connection:
            connection.execute(
                text(
                    f"""
                    UPDATE {table} SET state=:state, result=CAST(:result AS jsonb),
                        error=:error, updated_at=now()
                    WHERE id=:task_id
                    """
                ),
                {
                    "task_id": task_id,
                    "state": str(state),
                    "result": _jsonb(result),
                    "error": None if error is None else str(error),
                },
            )

    def create_bar_task(
        self,
        task_id: str,
        *,
        scene_id: str,
        overlay_id: int,
        smooth: bool,
        config: Mapping[str, Any],
        zones: Sequence[Mapping[str, Any]],
        conn: Any = None,
    ) -> None:
        self._create_worker_task(
            "v2_bar_tasks", task_id, scene_id=scene_id, overlay_id=overlay_id,
            smooth=smooth, config=config, zones=zones, conn=conn,
        )

    def get_bar_task(self, task_id: str, *, conn: Any = None) -> dict[str, Any] | None:
        return self._get_worker_task("v2_bar_tasks", task_id, conn=conn)

    def set_bar_task(
        self, task_id: str, *, state: str, result: Any = None, error: Any = None, conn: Any = None
    ) -> None:
        self._set_worker_task("v2_bar_tasks", task_id, state=state, result=result, error=error, conn=conn)

    def create_verification_task(
        self,
        task_id: str,
        *,
        scene_id: str,
        overlay_id: int,
        smooth: bool,
        config: Mapping[str, Any],
        zones: Sequence[Mapping[str, Any]],
        conn: Any = None,
    ) -> None:
        self._create_worker_task(
            "v2_verification_tasks", task_id, scene_id=scene_id, overlay_id=overlay_id,
            smooth=smooth, config=config, zones=zones, conn=conn,
        )

    def get_verification_task(self, task_id: str, *, conn: Any = None) -> dict[str, Any] | None:
        return self._get_worker_task("v2_verification_tasks", task_id, conn=conn)

    def set_verification_task(
        self, task_id: str, *, state: str, result: Any = None, error: Any = None, conn: Any = None
    ) -> None:
        self._set_worker_task(
            "v2_verification_tasks", task_id, state=state, result=result, error=error, conn=conn
        )
