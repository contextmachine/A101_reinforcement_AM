from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import text

from .codec import decode_object, encode_object, sha256
from .config import Settings
from .database import Database
from .jsonutil import dumps, loads, to_jsonable
from .overlays import normalize_overlay_id, resolve_overlay
from .polygon_storage import build_polygon_variants, geometry_polygons


def json_safe_value(value: Any) -> Any:
    """Convert algorithm values to JSON without stringifying geometries."""

    if hasattr(value, "geom_type"):
        from shapely.geometry import mapping

        return mapping(value)
    if isinstance(value, Mapping):
        return {str(key): json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe_value(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "tolist"):
        return json_safe_value(value.tolist())
    if hasattr(value, "item"):
        try:
            return json_safe_value(value.item())
        except Exception:
            pass
    return to_jsonable(value)


def _json_param(value: Any) -> str:
    return dumps(json_safe_value(value))


def _json_value(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    if isinstance(value, (str, bytes)):
        return loads(value, default)
    return value


def _utc_from_epoch(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc)


def _epoch(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    return float(value or 0.0)


class PostgresStore:
    """Durable storage for tasks, variants, artifacts, frontiers, solutions and events."""

    _TASK_COLUMNS = {
        "state",
        "parameters",
        "n_mode",
        "n_source",
        "scan_mode",
        "whole",
        "component_result_top_k",
        "validate_results",
        "max_concurrent_jobs",
        "manual_mode",
        "initial_variant",
        "paused",
        "generation",
    }
    _JSON_TASK_COLUMNS = {"parameters", "n_source"}

    def __init__(self, settings: Settings, database: Database | None = None):
        self.settings = settings
        self.database = database or Database(settings)

    def ping(self) -> bool:
        return self.database.ping()

    @staticmethod
    def _variant(variant: str | None = "raw") -> str:
        value = str(variant or "raw").lower()
        if value not in {"raw", "smooth"}:
            raise ValueError(f"Unknown analysis variant: {variant}")
        return value

    @staticmethod
    def component_db_id(component_id: Any) -> int:
        if str(component_id) == "whole":
            return -1
        return int(component_id)

    @staticmethod
    def component_public_id(component_id: int) -> str:
        return "whole" if int(component_id) == -1 else str(int(component_id))

    # ---------- task creation / source / variants ----------
    def create_task(
        self,
        task_id: str,
        meta: Mapping[str, Any],
        plan: Mapping[str, Any],
        input_obj: Mapping[str, Any],
    ) -> None:
        variants = build_polygon_variants(input_obj)
        initial_variant = self._variant(str(meta.get("initial_variant", "raw")))
        requested = list(dict.fromkeys(int(n) for n in plan.get("order", meta.get("requested_n", []))))
        kind = str(input_obj.get("kind", "polygons"))
        filename = str(input_obj.get("filename")) if input_obj.get("filename") else None
        content = input_obj.get("content") if kind == "dxf" else None
        source_bytes = bytes(content) if isinstance(content, (bytes, bytearray, memoryview)) else None
        source_sha256 = sha256(source_bytes if source_bytes is not None else _json_param(variants["raw"]).encode("utf-8"))
        source_meta = {
            key: json_safe_value(value)
            for key, value in input_obj.items()
            if key not in {"content", "polygons"}
        }
        now = _utc_from_epoch(meta.get("created_at", time.time()))
        try:
            with self.database.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO tasks (
                            id, state, parameters, n_mode, n_source, scan_mode, whole,
                            component_result_top_k, validate_results, max_concurrent_jobs,
                            manual_mode, initial_variant, paused, cancelled_at, generation,
                            created_at, updated_at
                        ) VALUES (
                            :id, :state, CAST(:parameters AS jsonb), :n_mode, CAST(:n_source AS jsonb),
                            :scan_mode, :whole, :top_k, :validate_results, :max_jobs, :manual_mode,
                            :initial_variant, :paused, NULL, 0, :created_at, :updated_at
                        )
                        """
                    ),
                    {
                        "id": task_id,
                        "state": str(meta.get("state", "uploaded")),
                        "parameters": _json_param(meta.get("parameters", {})),
                        "n_mode": str(meta.get("n_mode", plan.get("mode", "list"))),
                        "n_source": _json_param(meta.get("n_source", requested)),
                        "scan_mode": str(meta.get("scan_mode", "requested")),
                        "whole": bool(meta.get("whole", False)),
                        "top_k": int(meta.get("component_result_top_k", self.settings.frontier_top_k)),
                        "validate_results": bool(meta.get("validate_results", False)),
                        "max_jobs": int(meta.get("max_concurrent_jobs", self.settings.max_jobs_per_task)),
                        "manual_mode": bool(meta.get("manual_mode", False)),
                        "initial_variant": initial_variant,
                        "paused": bool(meta.get("paused", False)),
                        "created_at": now,
                        "updated_at": _utc_from_epoch(meta.get("updated_at", now)),
                    },
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO task_sources (task_id, kind, filename, content, sha256, metadata)
                        VALUES (:task_id, :kind, :filename, :content, :sha256, CAST(:metadata AS jsonb))
                        """
                    ),
                    {
                        "task_id": task_id,
                        "kind": kind,
                        "filename": filename,
                        "content": source_bytes,
                        "sha256": source_sha256,
                        "metadata": _json_param(source_meta),
                    },
                )
                for variant, polygons in variants.items():
                    smoothing = (
                        {"algorithm": "smooth_load", "version": 1, "threshold": 0.6}
                        if variant == "smooth"
                        else None
                    )
                    conn.execute(
                        text(
                            """
                            INSERT INTO task_variants (
                                task_id, variant, polygons, smoothing_metadata, preparation_state,
                                created_at, updated_at
                            ) VALUES (
                                :task_id, :variant, CAST(:polygons AS jsonb), CAST(:smoothing AS jsonb),
                                'stored', :created_at, :created_at
                            )
                            """
                        ),
                        {
                            "task_id": task_id,
                            "variant": variant,
                            "polygons": _json_param(polygons),
                            "smoothing": None if smoothing is None else _json_param(smoothing),
                            "created_at": now,
                        },
                    )
                    for position, n in enumerate(requested):
                        conn.execute(
                            text(
                                """
                                INSERT INTO task_n_requests (task_id, variant, n, position, status, requested_at, updated_at)
                                VALUES (:task_id, :variant, :n, :position, 'requested', :created_at, :created_at)
                                ON CONFLICT (task_id, variant, n) DO NOTHING
                                """
                            ),
                            {
                                "task_id": task_id,
                                "variant": variant,
                                "n": int(n),
                                "position": position,
                                "created_at": now,
                            },
                        )
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise ValueError(f"Задача {task_id} уже существует") from exc
            raise

    def load_variant_polygons(self, task_id: str, *, variant: str = "raw") -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT polygons, preparation_state FROM task_variants "
                    "WHERE task_id=:task_id AND variant=:variant"
                ),
                {"task_id": task_id, "variant": self._variant(variant)},
            ).mappings().first()
        if row is None:
            raise KeyError(f"variant={variant} for task={task_id} not found")
        if str(row["preparation_state"]) == "source_pending":
            raise KeyError(f"variant={variant} for task={task_id} is not materialized yet")
        return list(_json_value(row["polygons"], []))

    def get_object(self, task_id: str, name: str) -> Any:
        if name == "input":
            with self.database.connect() as conn:
                row = conn.execute(
                    text("SELECT kind, filename, content, sha256, metadata FROM task_sources WHERE task_id=:task_id"),
                    {"task_id": task_id},
                ).mappings().first()
            if row is None:
                raise KeyError(f"source input for task={task_id} not found")
            kind = str(row["kind"])
            metadata = dict(_json_value(row["metadata"], {}) or {})
            if kind == "dxf":
                content = bytes(row["content"] or b"")
                if sha256(content) != str(row["sha256"]):
                    raise IOError(f"source input for task={task_id} повреждён")
                return {"kind": "dxf", "filename": row["filename"] or "input.dxf", "content": content}
            return {
                "kind": "polygons",
                "units": str(metadata.get("units", "mm")),
                "polygons": self.load_variant_polygons(task_id, variant="raw"),
            }
        value = self._load_artifact(task_id, "raw", name)
        if value is None:
            raise KeyError(f"object {name} for task={task_id} not found")
        return value

    def put_object(self, task_id: str, name: str, value: Any) -> dict[str, Any]:
        return self._save_artifact(task_id, "raw", name, "generic", value)

    def put_blob(self, task_id: str, name: str, payload: bytes, codec: str = "bytes") -> dict[str, Any]:
        with self.database.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO runtime_artifacts (task_id, variant, artifact_key, artifact_type, codec, payload, sha256)
                    VALUES (:task_id, 'raw', :key, 'blob', :codec, :payload, :sha)
                    ON CONFLICT (task_id, variant, artifact_key) DO UPDATE SET
                        artifact_type=EXCLUDED.artifact_type, codec=EXCLUDED.codec, payload=EXCLUDED.payload,
                        sha256=EXCLUDED.sha256, updated_at=now()
                    """
                ),
                {"task_id": task_id, "key": name, "codec": codec, "payload": payload, "sha": sha256(payload)},
            )
        return {"bytes": len(payload), "sha256": sha256(payload), "codec": codec}

    def get_blob(self, task_id: str, name: str) -> bytes:
        with self.database.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT payload, sha256 FROM runtime_artifacts WHERE task_id=:task_id AND variant='raw' AND artifact_key=:key"
                ),
                {"task_id": task_id, "key": name},
            ).mappings().first()
        if row is None:
            raise KeyError(f"blob {name} for task={task_id} not found")
        payload = bytes(row["payload"])
        if sha256(payload) != row["sha256"]:
            raise IOError(f"blob {name} повреждён")
        return payload

    def delete_blob(self, task_id: str, name: str) -> None:
        self._delete_artifact(task_id, "raw", name)

    # ---------- task metadata / plan ----------
    def get_meta(self, task_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute(text("SELECT * FROM tasks WHERE id=:task_id"), {"task_id": task_id}).mappings().first()
            if row is None:
                return None
            variant_rows = conn.execute(
                text("SELECT variant, effective_rebar_config FROM task_variants WHERE task_id=:task_id"),
                {"task_id": task_id},
            ).mappings().all()
        initial_variant = str(row["initial_variant"])
        meta: dict[str, Any] = {}
        meta.update(
            {
                "task_id": str(row["id"]),
                "state": str(row["state"]),
                "created_at": _epoch(row["created_at"]),
                "updated_at": _epoch(row["updated_at"]),
                "cancelled": row["cancelled_at"] is not None,
                "paused": bool(row["paused"]),
                "parameters": dict(_json_value(row["parameters"], {}) or {}),
                "requested_n": self.requested_ns(task_id, variant=initial_variant),
                "n_mode": str(row["n_mode"]),
                "n_source": _json_value(row["n_source"], []),
                "scan_mode": str(row["scan_mode"]),
                "whole": bool(row["whole"]),
                "component_result_top_k": int(row["component_result_top_k"]),
                "validate_results": bool(row["validate_results"]),
                "max_concurrent_jobs": int(row["max_concurrent_jobs"]),
                "manual_mode": bool(row["manual_mode"]),
                "initial_variant": initial_variant,
                "initial_smooth": initial_variant == "smooth",
                "generation": int(row["generation"]),
            }
        )
        effective = {
            str(vrow["variant"]): _json_value(vrow["effective_rebar_config"], {})
            for vrow in variant_rows
            if vrow["effective_rebar_config"] is not None
        }
        if effective:
            meta["effective_rebar_config"] = effective
        return meta

    def task_meta(self, task_id: str) -> dict[str, Any]:
        return dict(self.get_meta(task_id) or {})

    def set_meta(self, task_id: str, meta: Mapping[str, Any]) -> None:
        current = self.get_meta(task_id)
        if current is None:
            raise KeyError(task_id)
        changes = {key: value for key, value in meta.items() if current.get(key) != value and key != "task_id"}
        if changes:
            self.patch_meta(task_id, **changes)

    def patch_meta(self, task_id: str, **changes: Any) -> dict[str, Any]:
        if self.get_meta(task_id) is None:
            raise KeyError(task_id)
        effective = changes.pop("effective_rebar_config", None)
        requested = changes.pop("requested_n", None)
        cancelled_marker = changes.pop("cancelled", None)
        changes.pop("expires_at", None)
        changes.pop("initial_smooth", None)
        changes.pop("created_at", None)
        changes.pop("updated_at", None)

        assignments: list[str] = []
        params: dict[str, Any] = {"task_id": task_id}
        unknown = sorted(key for key in changes if key not in self._TASK_COLUMNS)
        if unknown:
            raise ValueError(f"Неизвестные task metadata fields: {', '.join(unknown)}")
        for key, value in changes.items():
            bind = f"value_{key}"
            if key in self._JSON_TASK_COLUMNS:
                assignments.append(f"{key}=CAST(:{bind} AS jsonb)")
                params[bind] = _json_param(value)
            else:
                assignments.append(f"{key}=:{bind}")
                params[bind] = value
        if cancelled_marker is not None:
            assignments.append("cancelled_at=" + ("now()" if bool(cancelled_marker) else "NULL"))
        if assignments:
            assignments.append("updated_at=now()")
            with self.database.begin() as conn:
                conn.execute(text(f"UPDATE tasks SET {', '.join(assignments)} WHERE id=:task_id"), params)
        if isinstance(effective, Mapping):
            with self.database.begin() as conn:
                for variant, cfg in effective.items():
                    conn.execute(
                        text(
                            """
                            UPDATE task_variants SET effective_rebar_config=CAST(:cfg AS jsonb), updated_at=now()
                            WHERE task_id=:task_id AND variant=:variant
                            """
                        ),
                        {"task_id": task_id, "variant": self._variant(str(variant)), "cfg": _json_param(cfg)},
                    )
        if requested is not None:
            meta = self.get_meta(task_id) or {}
            self.add_requested_ns(task_id, list(requested), variant=str(meta.get("initial_variant", "raw")))
        return dict(self.get_meta(task_id) or {})

    def update_task_meta(self, task_id: str, **changes: Any) -> dict[str, Any]:
        return self.patch_meta(task_id, **changes)

    def requested_ns(self, task_id: str, *, variant: str | None = None) -> list[int]:
        if variant is None:
            with self.database.connect() as conn:
                variant = conn.execute(text("SELECT initial_variant FROM tasks WHERE id=:task_id"), {"task_id": task_id}).scalar_one_or_none()
            if variant is None:
                raise KeyError(task_id)
        with self.database.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT n FROM task_n_requests
                    WHERE task_id=:task_id AND variant=:variant
                    ORDER BY position, n
                    """
                ),
                {"task_id": task_id, "variant": self._variant(variant)},
            ).scalars().all()
        return [int(n) for n in rows]

    def get_plan(self, task_id: str, *, variant: str | None = None) -> dict[str, Any]:
        meta = self.get_meta(task_id)
        if meta is None:
            raise KeyError(task_id)
        selected = self._variant(variant or str(meta.get("initial_variant", "raw")))
        return {
            "mode": meta.get("n_mode", "list"),
            "order": self.requested_ns(task_id, variant=selected),
            "paused": bool(meta.get("paused")),
            "exhausted": False,
            "window": max(1, int(meta.get("max_concurrent_jobs") or self.settings.max_jobs_per_task)),
            "variant": selected,
        }

    def set_plan(self, task_id: str, plan: Mapping[str, Any]) -> None:
        if self.get_meta(task_id) is None:
            raise KeyError(task_id)
        if "paused" in plan:
            self.patch_meta(task_id, paused=bool(plan["paused"]))
        if "order" in plan:
            variant = str(plan.get("variant") or (self.get_meta(task_id) or {}).get("initial_variant", "raw"))
            self.add_requested_ns(task_id, list(plan["order"]), variant=variant)

    def add_requested_ns(self, task_id: str, ns: list[int], *, variant: str | None = None) -> dict[str, Any]:
        meta = self.get_meta(task_id)
        if meta is None:
            raise KeyError(task_id)
        selected = self._variant(variant or str(meta.get("initial_variant", "raw")))
        values = list(dict.fromkeys(int(n) for n in ns))
        if not values or any(n < 1 for n in values):
            raise ValueError("N должен быть положительным")
        if any(n > self.settings.max_n_value for n in values):
            raise ValueError(f"N превышает серверный лимит {self.settings.max_n_value}")
        with self.database.begin() as conn:
            current = conn.execute(
                text("SELECT COUNT(*) FROM task_n_requests WHERE task_id=:task_id AND variant=:variant"),
                {"task_id": task_id, "variant": selected},
            ).scalar_one()
            existing = set(
                int(n)
                for n in conn.execute(
                    text("SELECT n FROM task_n_requests WHERE task_id=:task_id AND variant=:variant"),
                    {"task_id": task_id, "variant": selected},
                ).scalars().all()
            )
            new_values = [n for n in values if n not in existing]
            if int(current) + len(new_values) > self.settings.max_planned_n_values:
                raise ValueError(f"План превысит лимит {self.settings.max_planned_n_values} значений N")
            for n in values:
                if n in existing:
                    conn.execute(
                        text(
                            """
                            UPDATE task_n_requests
                            SET cancelled_at=NULL, status='requested', detail='{}'::jsonb, updated_at=now()
                            WHERE task_id=:task_id AND variant=:variant AND n=:n AND cancelled_at IS NOT NULL
                            """
                        ),
                        {"task_id": task_id, "variant": selected, "n": n},
                    )
            max_position = conn.execute(
                text("SELECT COALESCE(MAX(position), -1) FROM task_n_requests WHERE task_id=:task_id AND variant=:variant"),
                {"task_id": task_id, "variant": selected},
            ).scalar_one()
            for offset, n in enumerate(new_values, start=1):
                conn.execute(
                    text(
                        """
                        INSERT INTO task_n_requests (task_id, variant, n, position, status)
                        VALUES (:task_id, :variant, :n, :position, 'requested')
                        ON CONFLICT (task_id, variant, n) DO UPDATE SET cancelled_at=NULL, updated_at=now()
                        """
                    ),
                    {"task_id": task_id, "variant": selected, "n": n, "position": int(max_position) + offset},
                )
            conn.execute(text("UPDATE tasks SET paused=false, state='running', updated_at=now() WHERE id=:task_id"), {"task_id": task_id})
        return self.get_plan(task_id, variant=selected)

    # ---------- events ----------
    @staticmethod
    def _event_after_id(after: str | int | None) -> int:
        if after is None:
            return 0
        text_value = str(after)
        if "-" in text_value:
            text_value = text_value.split("-", 1)[0]
        try:
            return max(0, int(text_value))
        except ValueError:
            return 0

    def publish_event(self, task_id: str, event_type: str, payload: Mapping[str, Any]) -> str:
        if self.get_meta(task_id) is None:
            raise KeyError(task_id)
        body = dict(json_safe_value(payload))
        with self.database.begin() as conn:
            event_id = conn.execute(
                text(
                    """
                    INSERT INTO task_events (task_id, event_type, payload)
                    VALUES (:task_id, :event_type, CAST(:payload AS jsonb))
                    RETURNING id
                    """
                ),
                {"task_id": task_id, "event_type": event_type, "payload": _json_param(body)},
            ).scalar_one()
        return str(int(event_id))

    def read_events(self, task_id: str, after: str = "0-0", count: int = 200) -> list[dict[str, Any]]:
        after_id = self._event_after_id(after)
        with self.database.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT id, event_type, payload, created_at FROM task_events
                    WHERE task_id=:task_id AND id > :after_id
                    ORDER BY id
                    LIMIT :count
                    """
                ),
                {"task_id": task_id, "after_id": after_id, "count": max(1, int(count))},
            ).mappings().all()
        return [
            {
                "id": str(int(row["id"])),
                "type": str(row["event_type"]),
                "task_id": task_id,
                "time": _epoch(row["created_at"]),
                **dict(_json_value(row["payload"], {}) or {}),
            }
            for row in rows
        ]

    def all_events(self, task_id: str, start: int = 0, limit: int = 10_000) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT id, event_type, payload, created_at FROM task_events WHERE task_id=:task_id
                    ORDER BY id OFFSET :offset LIMIT :limit
                    """
                ),
                {"task_id": task_id, "offset": max(0, int(start)), "limit": max(1, int(limit))},
            ).mappings().all()
        return [
            {
                "id": str(int(row["id"])),
                "type": str(row["event_type"]),
                "task_id": task_id,
                "time": _epoch(row["created_at"]),
                **dict(_json_value(row["payload"], {}) or {}),
            }
            for row in rows
        ]

    # ---------- N status / cancellation ----------
    def set_n_status(
        self,
        task_id: str,
        n: int,
        status: str,
        *,
        variant: str | None = None,
        **extra: Any,
    ) -> None:
        meta = self.get_meta(task_id)
        if meta is None:
            raise KeyError(task_id)
        selected = self._variant(variant or str(meta.get("initial_variant", "raw")))
        with self.database.begin() as conn:
            position = conn.execute(
                text("SELECT COALESCE(MAX(position), -1) + 1 FROM task_n_requests WHERE task_id=:task_id AND variant=:variant"),
                {"task_id": task_id, "variant": selected},
            ).scalar_one()
            conn.execute(
                text(
                    """
                    INSERT INTO task_n_requests (task_id, variant, n, position, status, detail)
                    VALUES (:task_id, :variant, :n, :position, :status, CAST(:detail AS jsonb))
                    ON CONFLICT (task_id, variant, n) DO UPDATE SET
                        status=EXCLUDED.status, detail=EXCLUDED.detail, updated_at=now()
                    """
                ),
                {
                    "task_id": task_id,
                    "variant": selected,
                    "n": int(n),
                    "position": int(position),
                    "status": str(status),
                    "detail": _json_param(extra),
                },
            )

    def get_n_statuses(self, task_id: str, *, variant: str | None = None) -> dict[str, dict[str, Any]]:
        meta = self.get_meta(task_id) if variant is None else None
        selected = self._variant(variant or str((meta or {}).get("initial_variant", "raw")))
        with self.database.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT n, status, detail, updated_at, cancelled_at
                    FROM task_n_requests WHERE task_id=:task_id AND variant=:variant ORDER BY position, n
                    """
                ),
                {"task_id": task_id, "variant": selected},
            ).mappings().all()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            detail = dict(_json_value(row["detail"], {}) or {})
            status = "cancelled" if row["cancelled_at"] is not None else str(row["status"])
            out[str(int(row["n"]))] = {
                "n": int(row["n"]),
                "status": status,
                "variant": selected,
                "updated_at": _epoch(row["updated_at"]),
                **detail,
            }
        return out

    def generation(self, task_id: str) -> int:
        with self.database.connect() as conn:
            value = conn.execute(text("SELECT generation FROM tasks WHERE id=:task_id"), {"task_id": task_id}).scalar_one_or_none()
        return int(value or 0)

    def bump_generation(self, task_id: str) -> int:
        with self.database.begin() as conn:
            value = conn.execute(
                text("UPDATE tasks SET generation=generation+1, updated_at=now() WHERE id=:task_id RETURNING generation"),
                {"task_id": task_id},
            ).scalar_one_or_none()
        if value is None:
            raise KeyError(task_id)
        return int(value)

    def cancel_ns(self, task_id: str, ns: list[int], *, variant: str | None = None) -> None:
        if self.get_meta(task_id) is None:
            raise KeyError(task_id)
        targets = sorted({int(n) for n in ns if int(n) > 0})
        variants = [self._variant(variant)] if variant is not None else ["raw", "smooth"]
        with self.database.begin() as conn:
            for selected in variants:
                for n in targets:
                    conn.execute(
                        text(
                            """
                            UPDATE task_n_requests SET cancelled_at=now(), status='cancelled', updated_at=now()
                            WHERE task_id=:task_id AND variant=:variant AND n=:n
                            """
                        ),
                        {"task_id": task_id, "variant": selected, "n": n},
                    )
        for n in targets:
            self.publish_event(task_id, "n_cancelled", {"n": n, "variant": variant or "all"})

    def is_n_cancelled(self, task_id: str, n: int, *, variant: str = "raw") -> bool:
        with self.database.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT t.cancelled_at, r.cancelled_at AS n_cancelled_at
                    FROM tasks t
                    LEFT JOIN task_n_requests r
                      ON r.task_id=t.id AND r.variant=:variant AND r.n=:n
                    WHERE t.id=:task_id
                    """
                ),
                {"task_id": task_id, "variant": self._variant(variant), "n": int(n)},
            ).mappings().first()
        return False if row is None else bool(row["cancelled_at"] is not None or row["n_cancelled_at"] is not None)

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        with self.database.begin() as conn:
            row = conn.execute(
                text(
                    """
                    UPDATE tasks SET cancelled_at=now(), state='cancelled', generation=generation+1, updated_at=now()
                    WHERE id=:task_id RETURNING generation
                    """
                ),
                {"task_id": task_id},
            ).first()
        if row is None:
            raise KeyError(task_id)
        generation = int(row[0])
        self.publish_event(task_id, "task_cancelled", {"generation": generation})
        return dict(self.get_meta(task_id) or {})

    def set_paused(self, task_id: str, paused: bool) -> dict[str, Any]:
        if self.get_meta(task_id) is None:
            raise KeyError(task_id)
        meta = self.patch_meta(task_id, paused=bool(paused), state="paused" if paused else "running")
        self.publish_event(task_id, "range_paused" if paused else "range_resumed", {})
        return meta

    # ---------- runtime artifacts ----------
    def _save_artifact(
        self,
        task_id: str,
        variant: str,
        artifact_key: str,
        artifact_type: str,
        value: Any,
        *,
        component_id: int | None = None,
        n: int | None = None,
    ) -> dict[str, Any]:
        payload, codec = encode_object(value)
        digest = sha256(payload)
        with self.database.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO runtime_artifacts (
                        task_id, variant, artifact_key, artifact_type, component_id, n, codec, payload, sha256
                    ) VALUES (
                        :task_id, :variant, :artifact_key, :artifact_type, :component_id, :n, :codec, :payload, :sha256
                    )
                    ON CONFLICT (task_id, variant, artifact_key) DO UPDATE SET
                        artifact_type=EXCLUDED.artifact_type,
                        component_id=EXCLUDED.component_id,
                        n=EXCLUDED.n,
                        codec=EXCLUDED.codec,
                        payload=EXCLUDED.payload,
                        sha256=EXCLUDED.sha256,
                        updated_at=now()
                    """
                ),
                {
                    "task_id": task_id,
                    "variant": self._variant(variant),
                    "artifact_key": artifact_key,
                    "artifact_type": artifact_type,
                    "component_id": component_id,
                    "n": n,
                    "codec": codec,
                    "payload": payload,
                    "sha256": digest,
                },
            )
        return {"bytes": len(payload), "sha256": digest, "codec": codec}

    def _load_artifact(self, task_id: str, variant: str, artifact_key: str) -> Any | None:
        with self.database.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT payload, sha256 FROM runtime_artifacts
                    WHERE task_id=:task_id AND variant=:variant AND artifact_key=:artifact_key
                    """
                ),
                {"task_id": task_id, "variant": self._variant(variant), "artifact_key": artifact_key},
            ).mappings().first()
        if row is None:
            return None
        payload = bytes(row["payload"])
        if sha256(payload) != str(row["sha256"]):
            raise IOError(f"artifact {artifact_key} повреждён")
        return decode_object(payload)

    def _delete_artifact(self, task_id: str, variant: str, artifact_key: str) -> None:
        with self.database.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM runtime_artifacts WHERE task_id=:task_id AND variant=:variant AND artifact_key=:artifact_key"
                ),
                {"task_id": task_id, "variant": self._variant(variant), "artifact_key": artifact_key},
            )

    def _find_artifact(self, task_id: str, artifact_key: str) -> Any | None:
        with self.database.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT variant, payload, sha256 FROM runtime_artifacts
                    WHERE task_id=:task_id AND artifact_key=:artifact_key
                    ORDER BY updated_at DESC LIMIT 1
                    """
                ),
                {"task_id": task_id, "artifact_key": artifact_key},
            ).mappings().first()
        if row is None:
            return None
        payload = bytes(row["payload"])
        if sha256(payload) != str(row["sha256"]):
            raise IOError(f"artifact {artifact_key} повреждён")
        return decode_object(payload)

    # ---------- field / components / problems ----------
    def save_field(self, task_id: str, value: Mapping[str, Any], *, variant: str = "raw") -> None:
        selected = self._variant(variant)
        record = dict(value)
        record.pop("start_polygons", None)
        decomposition = dict(record.get("decomposition", {}) or {})
        decomposition.pop("components", None)
        variant_indices = {
            key: [int(x) for x in decomposition.pop(key, [])]
            for key in ("active_indices", "background_only_indices", "degenerate_indices")
        }
        record["decomposition"] = decomposition
        self._save_artifact(task_id, selected, "field", "field", record)
        with self.database.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE task_variants SET
                        preparation_state='prepared',
                        active_indices=:active_indices,
                        background_only_indices=:background_only_indices,
                        degenerate_indices=:degenerate_indices,
                        prepared_at=now(), updated_at=now()
                    WHERE task_id=:task_id AND variant=:variant
                    """
                ),
                {
                    "task_id": task_id,
                    "variant": selected,
                    "active_indices": variant_indices["active_indices"],
                    "background_only_indices": variant_indices["background_only_indices"],
                    "degenerate_indices": variant_indices["degenerate_indices"],
                },
            )

    def load_field(self, task_id: str, *, variant: str = "raw") -> dict[str, Any]:
        selected = self._variant(variant)
        value = self._load_artifact(task_id, selected, "field")
        if value is None:
            return {}
        with self.database.connect() as conn:
            variant_row = conn.execute(
                text(
                    """
                    SELECT active_indices, background_only_indices, degenerate_indices
                    FROM task_variants WHERE task_id=:task_id AND variant=:variant
                    """
                ),
                {"task_id": task_id, "variant": selected},
            ).mappings().first()
        row = dict(value)
        decomposition = dict(row.get("decomposition", {}) or {})
        if variant_row is not None:
            decomposition.update(
                active_indices=[int(x) for x in variant_row["active_indices"] or []],
                background_only_indices=[int(x) for x in variant_row["background_only_indices"] or []],
                degenerate_indices=[int(x) for x in variant_row["degenerate_indices"] or []],
            )
        row["decomposition"] = decomposition
        row["start_polygons"] = geometry_polygons(self.load_variant_polygons(task_id, variant=selected))
        return row

    def save_component(self, task_id: str, component_id: Any, value: Mapping[str, Any], *, variant: str = "raw") -> None:
        selected = self._variant(variant)
        cid = self.component_db_id(component_id)
        row = dict(value)
        component = dict(row.get("component", {}) or {})
        info = dict(row.get("info", {}) or {})
        if not info and component:
            info = {
                "id": cid,
                "polygon_indices": component.get("polygon_indices", []),
                "classes": component.get("classes", []),
                "loads": component.get("loads", []),
                "bounds": component.get("bounds"),
                "demand_bounds": component.get("demand_bounds"),
                "max_useful_n": row.get("max_useful_n"),
                "state": row.get("state", "created"),
            }
        n_bounds = row.get("bounds") if isinstance(row.get("bounds"), Mapping) else None
        with self.database.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO components (
                        task_id, variant, component_id, state, polygon_indices, classes, loads,
                        bounds, demand_bounds, max_useful_n, n_bounds, planned_ns, force_single_box
                    ) VALUES (
                        :task_id, :variant, :component_id, :state, :polygon_indices, :classes, :loads,
                        :bounds, :demand_bounds, :max_useful_n, CAST(:n_bounds AS jsonb), :planned_ns, :force_single_box
                    )
                    ON CONFLICT (task_id, variant, component_id) DO UPDATE SET
                        state=EXCLUDED.state,
                        polygon_indices=EXCLUDED.polygon_indices,
                        classes=EXCLUDED.classes,
                        loads=EXCLUDED.loads,
                        bounds=EXCLUDED.bounds,
                        demand_bounds=EXCLUDED.demand_bounds,
                        max_useful_n=EXCLUDED.max_useful_n,
                        n_bounds=EXCLUDED.n_bounds,
                        planned_ns=EXCLUDED.planned_ns,
                        force_single_box=EXCLUDED.force_single_box,
                        updated_at=now()
                    """
                ),
                {
                    "task_id": task_id,
                    "variant": selected,
                    "component_id": cid,
                    "state": str(row.get("state", info.get("state", "created"))),
                    "polygon_indices": [int(x) for x in info.get("polygon_indices", component.get("polygon_indices", []))],
                    "classes": [int(x) for x in info.get("classes", component.get("classes", []))],
                    "loads": [float(x) for x in info.get("loads", component.get("loads", []))],
                    "bounds": [float(x) for x in info.get("bounds", [])] if info.get("bounds") else None,
                    "demand_bounds": [float(x) for x in info.get("demand_bounds", [])] if info.get("demand_bounds") else None,
                    "max_useful_n": None if row.get("max_useful_n") is None else int(row["max_useful_n"]),
                    "n_bounds": None if n_bounds is None else _json_param(n_bounds),
                    "planned_ns": [int(x) for x in row.get("plan", [])],
                    "force_single_box": bool(row.get("force_single_box", False)),
                },
            )
        if component:
            self._save_artifact(task_id, selected, f"component:{cid}", "component", component, component_id=cid)

    def load_component(self, task_id: str, component_id: Any, *, variant: str = "raw") -> dict[str, Any] | None:
        selected = self._variant(variant)
        cid = self.component_db_id(component_id)
        with self.database.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT * FROM components
                    WHERE task_id=:task_id AND variant=:variant AND component_id=:component_id
                    """
                ),
                {"task_id": task_id, "variant": selected, "component_id": cid},
            ).mappings().first()
        if row is None:
            return None
        component = self._load_artifact(task_id, selected, f"component:{cid}") or {}
        info = {
            "id": cid,
            "polygon_indices": list(row["polygon_indices"] or []),
            "classes": list(row["classes"] or []),
            "loads": [float(x) for x in row["loads"] or []],
            "bounds": list(row["bounds"]) if row["bounds"] is not None else None,
            "demand_bounds": list(row["demand_bounds"]) if row["demand_bounds"] is not None else None,
            "max_useful_n": None if row["max_useful_n"] is None else int(row["max_useful_n"]),
            "prepared": row["max_useful_n"] is not None,
            "state": str(row["state"]),
        }
        result: dict[str, Any] = {
            "component": dict(component),
            "info": info,
            "state": str(row["state"]),
            "plan": [int(x) for x in row["planned_ns"] or []],
            "force_single_box": bool(row["force_single_box"]),
            "max_useful_n": None if row["max_useful_n"] is None else int(row["max_useful_n"]),
            "variant": selected,
            "smooth": selected == "smooth",
        }
        if row["n_bounds"] is not None:
            result["bounds"] = _json_value(row["n_bounds"], {})
        return result

    def component_ids(self, task_id: str, *, variant: str = "raw") -> list[str]:
        with self.database.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT component_id FROM components
                    WHERE task_id=:task_id AND variant=:variant
                    ORDER BY CASE WHEN component_id=-1 THEN 1 ELSE 0 END, component_id
                    """
                ),
                {"task_id": task_id, "variant": self._variant(variant)},
            ).scalars().all()
        return [self.component_public_id(int(cid)) for cid in rows]

    def components(self, task_id: str, *, variant: str = "raw") -> list[dict[str, Any]]:
        return [
            value
            for cid in self.component_ids(task_id, variant=variant)
            if (value := self.load_component(task_id, cid, variant=variant)) is not None
        ]

    def save_problem(self, task_id: str, component_id: Any, value: Mapping[str, Any], *, variant: str = "raw") -> None:
        cid = self.component_db_id(component_id)
        self._save_artifact(task_id, variant, f"problem:{cid}", "problem", dict(value), component_id=cid)

    def load_problem(self, task_id: str, component_id: Any, *, variant: str = "raw") -> dict[str, Any] | None:
        value = self._load_artifact(task_id, variant, f"problem:{self.component_db_id(component_id)}")
        return None if value is None else dict(value)

    def save_solver_result(
        self, task_id: str, component_id: Any, n: int, value: Mapping[str, Any], *, variant: str = "raw"
    ) -> None:
        cid = self.component_db_id(component_id)
        self._save_artifact(
            task_id,
            variant,
            f"solver:{cid}:{int(n)}",
            "solver_result",
            dict(value),
            component_id=cid,
            n=int(n),
        )

    def load_solver_result(
        self, task_id: str, component_id: Any, n: int, *, variant: str = "raw"
    ) -> dict[str, Any] | None:
        value = self._load_artifact(task_id, variant, f"solver:{self.component_db_id(component_id)}:{int(n)}")
        return None if value is None else dict(value)

    # ---------- component frontier ----------
    def save_frontier_result(
        self, task_id: str, component_id: Any, n: int, value: Mapping[str, Any], *, variant: str = "raw"
    ) -> None:
        selected = self._variant(variant)
        cid = self.component_db_id(component_id)
        result = dict(json_safe_value(dict(value)))
        status = str(result.get("status", result.get("solve_state", "feasible" if result.get("is_feasible") else "failed")))
        proxy = result.get("proxy_mass", result.get("total_cost"))
        with self.database.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO component_results (
                        task_id, variant, component_id, n, status, is_feasible, is_optimal, proxy_mass, result
                    ) VALUES (
                        :task_id, :variant, :component_id, :n, :status, :is_feasible, :is_optimal,
                        :proxy_mass, CAST(:result AS jsonb)
                    )
                    ON CONFLICT (task_id, variant, component_id, n) DO UPDATE SET
                        status=EXCLUDED.status,
                        is_feasible=EXCLUDED.is_feasible,
                        is_optimal=EXCLUDED.is_optimal,
                        proxy_mass=EXCLUDED.proxy_mass,
                        result=EXCLUDED.result,
                        updated_at=now()
                    """
                ),
                {
                    "task_id": task_id,
                    "variant": selected,
                    "component_id": cid,
                    "n": int(n),
                    "status": status,
                    "is_feasible": bool(result.get("is_feasible", False)),
                    "is_optimal": bool(result.get("is_optimal", False)),
                    "proxy_mass": None if proxy is None else float(proxy),
                    "result": _json_param(result),
                },
            )
            conn.execute(
                text(
                    """
                    UPDATE task_variants SET frontier_version=frontier_version+1, updated_at=now()
                    WHERE task_id=:task_id AND variant=:variant
                    """
                ),
                {"task_id": task_id, "variant": selected},
            )
        self._delete_artifact(task_id, selected, f"solver:{cid}:{int(n)}")

    def frontier_version(self, task_id: str, *, variant: str = "raw") -> int:
        with self.database.connect() as conn:
            value = conn.execute(
                text("SELECT frontier_version FROM task_variants WHERE task_id=:task_id AND variant=:variant"),
                {"task_id": task_id, "variant": self._variant(variant)},
            ).scalar_one_or_none()
        return int(value or 0)

    def load_frontier(self, task_id: str, component_id: Any, *, variant: str = "raw") -> dict[int, dict[str, Any]]:
        with self.database.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT n, result FROM component_results
                    WHERE task_id=:task_id AND variant=:variant AND component_id=:component_id
                    ORDER BY n
                    """
                ),
                {
                    "task_id": task_id,
                    "variant": self._variant(variant),
                    "component_id": self.component_db_id(component_id),
                },
            ).mappings().all()
        return {int(row["n"]): dict(_json_value(row["result"], {}) or {}) for row in rows}

    def all_frontiers(
        self, task_id: str, include_whole: bool = False, *, variant: str = "raw"
    ) -> dict[Any, dict[int, dict[str, Any]]]:
        out: dict[Any, dict[int, dict[str, Any]]] = {}
        for cid in self.component_ids(task_id, variant=variant):
            if cid == "whole" and not include_whole:
                continue
            frontier = self.load_frontier(task_id, cid, variant=variant)
            if frontier:
                key: Any = "whole" if cid == "whole" else int(cid)
                out[key] = frontier
        return out

    # ---------- candidates / solutions ----------
    def save_candidate(self, task_id: str, candidate_id: str, value: Mapping[str, Any]) -> None:
        selected = self._variant(str(value.get("variant", "raw")))
        self._save_artifact(task_id, selected, f"candidate:{candidate_id}", "candidate", dict(value))

    def load_candidate(self, task_id: str, candidate_id: str) -> dict[str, Any] | None:
        value = self._find_artifact(task_id, f"candidate:{candidate_id}")
        return None if value is None else dict(value)

    def save_solution(self, task_id: str, solution: Mapping[str, Any]) -> None:
        row = dict(solution)
        sid = str(row["solution_id"])
        variant = self._variant(str(row.get("variant", "raw")))
        result = dict(json_safe_value(row))
        result["variant"] = variant
        result["smooth"] = variant == "smooth"
        proxy = result.get("proxy_mass")
        mass = result.get("actual_mass_kg")
        validation = result.get("validation")
        with self.database.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO solutions (
                        solution_id, task_id, variant, source, total_n, component_ns,
                        proxy_mass, actual_mass_kg, is_feasible, is_optimal, status, result, validation
                    ) VALUES (
                        :solution_id, :task_id, :variant, :source, :total_n, CAST(:component_ns AS jsonb),
                        :proxy_mass, :actual_mass_kg, :is_feasible, :is_optimal, :status,
                        CAST(:result AS jsonb), CAST(:validation AS jsonb)
                    )
                    ON CONFLICT (solution_id) DO UPDATE SET
                        source=EXCLUDED.source,
                        total_n=EXCLUDED.total_n,
                        component_ns=EXCLUDED.component_ns,
                        proxy_mass=EXCLUDED.proxy_mass,
                        actual_mass_kg=EXCLUDED.actual_mass_kg,
                        is_feasible=EXCLUDED.is_feasible,
                        is_optimal=EXCLUDED.is_optimal,
                        status=EXCLUDED.status,
                        result=EXCLUDED.result,
                        validation=EXCLUDED.validation,
                        updated_at=now()
                    """
                ),
                {
                    "solution_id": sid,
                    "task_id": task_id,
                    "variant": variant,
                    "source": str(result.get("source", "components")),
                    "total_n": int(result.get("total_N", result.get("total_n", 0))),
                    "component_ns": _json_param(result.get("component_ns", {})),
                    "proxy_mass": None if proxy is None else float(proxy),
                    "actual_mass_kg": None if mass is None or not math.isfinite(float(mass)) else float(mass),
                    "is_feasible": bool(result.get("is_feasible", False)),
                    "is_optimal": bool(result.get("is_optimal", False)),
                    "status": str(result.get("status", "unknown")),
                    "result": _json_param(result),
                    "validation": None if validation is None else _json_param(validation),
                },
            )
        candidate_id = result.get("candidate_id")
        if candidate_id:
            self._delete_artifact(task_id, variant, f"candidate:{candidate_id}")

    def load_solution(self, task_id: str, solution_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            value = conn.execute(
                text("SELECT result FROM solutions WHERE task_id=:task_id AND solution_id=:solution_id"),
                {"task_id": task_id, "solution_id": solution_id},
            ).scalar_one_or_none()
        return None if value is None else dict(_json_value(value, {}) or {})

    def solutions(
        self,
        task_id: str,
        total_n: int | None = None,
        source: str | None = None,
        variant: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["task_id=:task_id"]
        params: dict[str, Any] = {"task_id": task_id}
        if total_n is not None:
            clauses.append("total_n=:total_n")
            params["total_n"] = int(total_n)
        if source is not None:
            clauses.append("source=:source")
            params["source"] = str(source)
        if variant is not None:
            clauses.append("variant=:variant")
            params["variant"] = self._variant(variant)
        sql = f"""
            SELECT result FROM solutions WHERE {' AND '.join(clauses)}
            ORDER BY is_feasible DESC, actual_mass_kg ASC NULLS LAST, proxy_mass ASC NULLS LAST, created_at ASC
        """
        with self.database.connect() as conn:
            values = conn.execute(text(sql), params).scalars().all()
        return [dict(_json_value(value, {}) or {}) for value in values]

    def best_solution(self, task_id: str, total_n: int, *, variant: str | None = None) -> dict[str, Any] | None:
        rows = self.solutions(task_id, total_n=total_n, variant=variant)
        return rows[0] if rows else None

    @staticmethod
    def _result_rank(meta: Mapping[str, Any]) -> tuple[int, int, float, int]:
        cost = meta.get("total_cost")
        feasible = bool(meta.get("is_feasible"))
        postprocessed = bool(meta.get("postprocessed"))
        tier = 0 if feasible and postprocessed else (1 if feasible else 2)
        return (
            tier,
            0 if meta.get("is_optimal") else 1,
            float("inf") if cost is None else float(cost),
            0 if meta.get("kind") == "final" else 1,
        )

    def get_result(self, task_id: str, n: int) -> dict[str, Any] | None:
        solution = self.best_solution(task_id, int(n), variant="raw") or self.best_solution(task_id, int(n))
        if solution is None:
            return None
        from .pipeline import to_compat_result

        return to_compat_result(solution)

    def get_result_meta(self, task_id: str, n: int) -> dict[str, Any] | None:
        solution = self.best_solution(task_id, int(n), variant="raw") or self.best_solution(task_id, int(n))
        if solution is None:
            return None
        return {
            "n": int(n),
            "kind": "final",
            "is_feasible": bool(solution.get("is_feasible")),
            "is_optimal": bool(solution.get("is_optimal")),
            "total_cost": solution.get("actual_mass_kg", solution.get("proxy_mass")),
            "postprocessed": True,
            "solution_id": solution.get("solution_id"),
        }

    def get_result_metas(self, task_id: str) -> dict[str, dict[str, Any]]:
        rows = self.solutions(task_id, variant="raw")
        best: dict[int, dict[str, Any]] = {}
        for row in rows:
            n = int(row.get("total_N", row.get("total_n", -1)))
            if n < 0 or n in best:
                continue
            best[n] = {
                "n": n,
                "kind": "final",
                "is_feasible": bool(row.get("is_feasible")),
                "is_optimal": bool(row.get("is_optimal")),
                "total_cost": row.get("actual_mass_kg", row.get("proxy_mass")),
                "postprocessed": True,
                "solution_id": row.get("solution_id"),
            }
        return {str(n): value for n, value in sorted(best.items())}

    def save_best_result(
        self, task_id: str, n: int, result: Mapping[str, Any], kind: str = "final"
    ) -> tuple[bool, dict[str, Any]]:
        # Compatibility shim: canonical solutions are already persisted; no duplicate result row is written.
        meta = {
            "n": int(n),
            "kind": kind,
            "is_feasible": bool(result.get("is_feasible")),
            "is_optimal": bool(result.get("is_optimal")),
            "total_cost": result.get("total_cost"),
            "postprocessed": True,
            "updated_at": time.time(),
        }
        return True, meta

    # ---------- public snapshot ----------
    def refresh_pipeline_state(self, task_id: str) -> dict[str, Any] | None:
        meta = self.get_meta(task_id)
        if meta is None:
            return None
        if meta.get("cancelled") or meta.get("paused"):
            return meta
        pending = self.pending_jobs(task_id)  # supplied by Store facade
        if pending:
            state = "running"
        elif meta.get("manual_mode"):
            variant = str(meta.get("initial_variant", "raw"))
            try:
                field = self.load_field(task_id, variant=variant)
            except TypeError:  # compatibility with small test/dummy stores
                field = self.load_field(task_id)
            state = "ready" if field else "uploaded"
        else:
            state = "completed" if self.solutions(task_id) else "completed_with_errors"
        if state != meta.get("state"):
            meta = self.patch_meta(task_id, state=state)
            self.publish_event(task_id, "task_state", {"state": state})
        return meta

    def snapshot(self, task_id: str) -> dict[str, Any] | None:
        meta = self.get_meta(task_id)
        if meta is None:
            return None
        statuses = self.get_n_statuses(task_id, variant=str(meta.get("initial_variant", "raw")))
        counts: dict[str, int] = {}
        for value in statuses.values():
            status = str(value.get("status", "unknown"))
            counts[status] = counts.get(status, 0) + 1
        return {
            "task": meta,
            "plan": self.get_plan(task_id, variant=str(meta.get("initial_variant", "raw"))),
            "n": statuses,
            "status_counts": counts,
            "results": self.get_result_metas(task_id),
        }

    # ======================================================================
    # Overlay-aware storage overrides (schema revision 0002).
    # overlay_id=0 is the historical/base analysis with no overlay applied.
    # ======================================================================

    def create_task(
        self,
        task_id: str,
        meta: Mapping[str, Any],
        plan: Mapping[str, Any],
        input_obj: Mapping[str, Any],
    ) -> None:
        kind = str(input_obj.get("kind", "polygons"))
        deferred_source = kind == "dxf"
        variants = {"raw": [], "smooth": []} if deferred_source else build_polygon_variants(input_obj)
        initial_variant = self._variant(str(meta.get("initial_variant", "raw")))
        requested = list(dict.fromkeys(int(n) for n in plan.get("order", meta.get("requested_n", []))))
        filename = str(input_obj.get("filename")) if input_obj.get("filename") else None
        content = input_obj.get("content") if kind == "dxf" else None
        source_bytes = bytes(content) if isinstance(content, (bytes, bytearray, memoryview)) else None
        source_sha256 = sha256(source_bytes if source_bytes is not None else _json_param(variants["raw"]).encode("utf-8"))
        source_meta = {
            key: json_safe_value(value)
            for key, value in input_obj.items()
            if key not in {"content", "polygons"}
        }
        now = _utc_from_epoch(meta.get("created_at", time.time()))
        try:
            with self.database.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO tasks (
                            id, state, parameters, n_mode, n_source, scan_mode, whole,
                            component_result_top_k, validate_results, max_concurrent_jobs,
                            manual_mode, initial_variant, paused, cancelled_at, generation,
                            created_at, updated_at
                        ) VALUES (
                            :id, :state, CAST(:parameters AS jsonb), :n_mode, CAST(:n_source AS jsonb),
                            :scan_mode, :whole, :top_k, :validate_results, :max_jobs, :manual_mode,
                            :initial_variant, :paused, NULL, 0, :created_at, :updated_at
                        )
                        """
                    ),
                    {
                        "id": task_id,
                        "state": str(meta.get("state", "uploaded")),
                        "parameters": _json_param(meta.get("parameters", {})),
                        "n_mode": str(meta.get("n_mode", plan.get("mode", "list"))),
                        "n_source": _json_param(meta.get("n_source", requested)),
                        "scan_mode": str(meta.get("scan_mode", "requested")),
                        "whole": bool(meta.get("whole", False)),
                        "top_k": int(meta.get("component_result_top_k", self.settings.frontier_top_k)),
                        "validate_results": bool(meta.get("validate_results", False)),
                        "max_jobs": int(meta.get("max_concurrent_jobs", self.settings.max_jobs_per_task)),
                        "manual_mode": bool(meta.get("manual_mode", False)),
                        "initial_variant": initial_variant,
                        "paused": bool(meta.get("paused", False)),
                        "created_at": now,
                        "updated_at": _utc_from_epoch(meta.get("updated_at", now)),
                    },
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO task_sources (task_id, kind, filename, content, sha256, metadata)
                        VALUES (:task_id, :kind, :filename, :content, :sha256, CAST(:metadata AS jsonb))
                        """
                    ),
                    {
                        "task_id": task_id,
                        "kind": kind,
                        "filename": filename,
                        "content": source_bytes,
                        "sha256": source_sha256,
                        "metadata": _json_param(source_meta),
                    },
                )
                for variant, polygons in variants.items():
                    smoothing = (
                        {"algorithm": "smooth_load", "version": 1, "threshold": 0.6}
                        if variant == "smooth" and not deferred_source
                        else None
                    )
                    variant_state = "source_pending" if deferred_source else "stored"
                    conn.execute(
                        text(
                            """
                            INSERT INTO task_variants (
                                task_id, variant, polygons, smoothing_metadata, preparation_state,
                                created_at, updated_at
                            ) VALUES (
                                :task_id, :variant, CAST(:polygons AS jsonb), CAST(:smoothing AS jsonb),
                                :preparation_state, :created_at, :created_at
                            )
                            """
                        ),
                        {
                            "task_id": task_id,
                            "variant": variant,
                            "polygons": _json_param(polygons),
                            "smoothing": None if smoothing is None else _json_param(smoothing),
                            "preparation_state": variant_state,
                            "created_at": now,
                        },
                    )
                    conn.execute(
                        text(
                            """
                            INSERT INTO task_analyses (task_id, variant, overlay_id, preparation_state, created_at, updated_at)
                            VALUES (:task_id, :variant, 0, 'stored', :created_at, :created_at)
                            """
                        ),
                        {"task_id": task_id, "variant": variant, "created_at": now},
                    )
                    for position, n in enumerate(requested):
                        conn.execute(
                            text(
                                """
                                INSERT INTO task_n_requests (
                                    task_id, variant, overlay_id, n, position, status, requested_at, updated_at
                                ) VALUES (
                                    :task_id, :variant, 0, :n, :position, 'requested', :created_at, :created_at
                                )
                                ON CONFLICT (task_id, variant, overlay_id, n) DO NOTHING
                                """
                            ),
                            {"task_id": task_id, "variant": variant, "n": int(n), "position": position, "created_at": now},
                        )
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise ValueError(f"Задача {task_id} уже существует") from exc
            raise

    def ensure_polygon_variants(self, task_id: str) -> bool:
        """Materialize deferred DXF raw/smooth variants exactly once in a worker.

        DXF uploads persist the source bytes and placeholder variant rows so the
        HTTP upload can return immediately.  The first preparation job acquires
        a PostgreSQL advisory transaction lock, parses the source, and replaces
        both placeholders atomically.
        """
        with self.database.begin() as conn:
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:task_id, 0))"),
                {"task_id": task_id},
            )
            states = conn.execute(
                text(
                    "SELECT variant, preparation_state FROM task_variants "
                    "WHERE task_id=:task_id ORDER BY variant"
                ),
                {"task_id": task_id},
            ).mappings().all()
            if not states:
                raise KeyError(f"variants for task={task_id} not found")
            if all(str(row["preparation_state"]) != "source_pending" for row in states):
                return False

            source = conn.execute(
                text(
                    "SELECT kind, filename, content, sha256 FROM task_sources "
                    "WHERE task_id=:task_id"
                ),
                {"task_id": task_id},
            ).mappings().first()
            if source is None:
                raise KeyError(f"source input for task={task_id} not found")
            if str(source["kind"]) != "dxf":
                raise ValueError(f"source_pending is supported only for DXF tasks: {task_id}")

            content = bytes(source["content"] or b"")
            if sha256(content) != str(source["sha256"]):
                raise IOError(f"source input for task={task_id} повреждён")
            input_obj = {
                "kind": "dxf",
                "filename": source["filename"] or "input.dxf",
                "content": content,
            }
            variants = build_polygon_variants(input_obj)
            for variant, polygons in variants.items():
                smoothing = (
                    {"algorithm": "smooth_load", "version": 1, "threshold": 0.6}
                    if variant == "smooth"
                    else None
                )
                conn.execute(
                    text(
                        """
                        UPDATE task_variants SET
                            polygons=CAST(:polygons AS jsonb),
                            smoothing_metadata=CAST(:smoothing AS jsonb),
                            preparation_state='stored',
                            updated_at=now()
                        WHERE task_id=:task_id AND variant=:variant
                        """
                    ),
                    {
                        "task_id": task_id,
                        "variant": variant,
                        "polygons": _json_param(polygons),
                        "smoothing": None if smoothing is None else _json_param(smoothing),
                    },
                )
        return True

    # ---------- overlay event log / analysis identity ----------
    def overlay_events(self, task_id: str) -> list[dict[str, Any]]:
        if self.get_meta(task_id) is None:
            raise KeyError(task_id)
        with self.database.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT seq, overlay_id, event_type, idxs, real, created_at
                    FROM task_overlay_events WHERE task_id=:task_id ORDER BY seq
                    """
                ),
                {"task_id": task_id},
            ).mappings().all()
        return [
            {
                "seq": int(row["seq"]),
                "id": int(row["overlay_id"]),
                "type": str(row["event_type"]),
                "idxs": [int(x) for x in row["idxs"] or []],
                "real": bool(row["real"]),
                "created_at": _epoch(row["created_at"]),
            }
            for row in rows
        ]

    def append_overlay_events(self, task_id: str, events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        if self.get_meta(task_id) is None:
            raise KeyError(task_id)
        polygon_count = len(self.load_variant_polygons(task_id, variant="raw"))
        rows: list[dict[str, Any]] = []
        seen: set[int] = set()
        for raw in events:
            event_type = str(raw.get("type", "")).lower()
            if event_type not in {"clean", "unclean"}:
                raise ValueError("overlay type must be 'clean' or 'unclean'")
            overlay_id = normalize_overlay_id(raw.get("id"))
            if overlay_id == 0:
                raise ValueError("overlay id 0 is reserved for the base analysis")
            if overlay_id in seen:
                raise ValueError(f"duplicate overlay id in request: {overlay_id}")
            seen.add(overlay_id)
            idxs = list(dict.fromkeys(int(x) for x in (raw.get("idxs", []) or [])))
            invalid = [idx for idx in idxs if idx < 0 or idx >= polygon_count]
            if invalid:
                raise ValueError(f"source polygon indices out of range: {invalid}")
            rows.append({"id": overlay_id, "type": event_type, "idxs": idxs, "real": bool(raw.get("real", False))})
        if not rows:
            return self.overlay_events(task_id)
        try:
            with self.database.begin() as conn:
                for row in rows:
                    conn.execute(
                        text(
                            """
                            INSERT INTO task_overlay_events (task_id, overlay_id, event_type, idxs, real)
                            VALUES (:task_id, :overlay_id, :event_type, :idxs, :real)
                            """
                        ),
                        {
                            "task_id": task_id,
                            "overlay_id": row["id"],
                            "event_type": row["type"],
                            "idxs": row["idxs"],
                            "real": row["real"],
                        },
                    )
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise ValueError("overlay id already exists for this task") from exc
            raise
        return self.overlay_events(task_id)

    def resolved_source_polygons(
        self, task_id: str, *, variant: str = "raw", overlay_id: int | None = 0
    ) -> list[dict[str, Any]]:
        selected_overlay = normalize_overlay_id(overlay_id)
        polygons = self.load_variant_polygons(task_id, variant=self._variant(variant))
        events = self.overlay_events(task_id) if selected_overlay else []
        return resolve_overlay(polygons, events, selected_overlay)

    def ensure_analysis(self, task_id: str, *, variant: str = "raw", overlay_id: int | None = 0) -> dict[str, Any]:
        selected = self._variant(variant)
        selected_overlay = normalize_overlay_id(overlay_id)
        if self.get_meta(task_id) is None:
            raise KeyError(task_id)
        if selected_overlay:
            with self.database.connect() as conn:
                exists = conn.execute(
                    text("SELECT 1 FROM task_overlay_events WHERE task_id=:task_id AND overlay_id=:overlay_id"),
                    {"task_id": task_id, "overlay_id": selected_overlay},
                ).scalar_one_or_none()
            if exists is None:
                raise KeyError(f"overlay={selected_overlay} not found")
        with self.database.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO task_analyses (task_id, variant, overlay_id, preparation_state)
                    VALUES (:task_id, :variant, :overlay_id, 'stored')
                    ON CONFLICT (task_id, variant, overlay_id) DO NOTHING
                    """
                ),
                {"task_id": task_id, "variant": selected, "overlay_id": selected_overlay},
            )
        state = self.analysis_state(task_id, variant=selected, overlay_id=selected_overlay)
        if state is None:
            raise KeyError(f"analysis {task_id}/{selected}/{selected_overlay} not found")
        return state

    def analysis_state(self, task_id: str, *, variant: str = "raw", overlay_id: int | None = 0) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT * FROM task_analyses
                    WHERE task_id=:task_id AND variant=:variant AND overlay_id=:overlay_id
                    """
                ),
                {"task_id": task_id, "variant": self._variant(variant), "overlay_id": normalize_overlay_id(overlay_id)},
            ).mappings().first()
        if row is None:
            return None
        return {
            "task_id": task_id,
            "variant": str(row["variant"]),
            "overlay_id": int(row["overlay_id"]),
            "preparation_state": str(row["preparation_state"]),
            "frontier_version": int(row["frontier_version"] or 0),
            "active_indices": [int(x) for x in row["active_indices"] or []],
            "background_only_indices": [int(x) for x in row["background_only_indices"] or []],
            "removed_indices": [int(x) for x in row["removed_indices"] or []],
            "degenerate_indices": [int(x) for x in row["degenerate_indices"] or []],
            "prepared_at": None if row["prepared_at"] is None else _epoch(row["prepared_at"]),
        }

    def mark_analysis_preparing(self, task_id: str, *, variant: str = "raw", overlay_id: int | None = 0) -> bool:
        selected = self._variant(variant)
        selected_overlay = normalize_overlay_id(overlay_id)
        self.ensure_analysis(task_id, variant=selected, overlay_id=selected_overlay)
        with self.database.begin() as conn:
            row = conn.execute(
                text(
                    """
                    UPDATE task_analyses SET preparation_state='preparing', updated_at=now()
                    WHERE task_id=:task_id AND variant=:variant AND overlay_id=:overlay_id
                      AND preparation_state NOT IN ('preparing','prepared')
                    RETURNING overlay_id
                    """
                ),
                {"task_id": task_id, "variant": selected, "overlay_id": selected_overlay},
            ).first()
        return row is not None

    def mark_analysis_prepared(self, task_id: str, *, variant: str = "raw", overlay_id: int | None = 0) -> None:
        with self.database.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE task_analyses SET preparation_state='prepared', prepared_at=COALESCE(prepared_at, now()), updated_at=now()
                    WHERE task_id=:task_id AND variant=:variant AND overlay_id=:overlay_id
                    """
                ),
                {"task_id": task_id, "variant": self._variant(variant), "overlay_id": normalize_overlay_id(overlay_id)},
            )

    # ---------- overlay-aware N plan ----------
    def requested_ns(self, task_id: str, *, variant: str | None = None, overlay_id: int | None = 0) -> list[int]:
        if variant is None:
            with self.database.connect() as conn:
                variant = conn.execute(text("SELECT initial_variant FROM tasks WHERE id=:task_id"), {"task_id": task_id}).scalar_one_or_none()
            if variant is None:
                raise KeyError(task_id)
        selected_overlay = normalize_overlay_id(overlay_id)
        with self.database.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT n FROM task_n_requests
                    WHERE task_id=:task_id AND variant=:variant AND overlay_id=:overlay_id
                    ORDER BY position, n
                    """
                ),
                {"task_id": task_id, "variant": self._variant(variant), "overlay_id": selected_overlay},
            ).scalars().all()
        return [int(n) for n in rows]

    def get_plan(self, task_id: str, *, variant: str | None = None, overlay_id: int | None = 0) -> dict[str, Any]:
        meta = self.get_meta(task_id)
        if meta is None:
            raise KeyError(task_id)
        selected = self._variant(variant or str(meta.get("initial_variant", "raw")))
        selected_overlay = normalize_overlay_id(overlay_id)
        return {
            "mode": meta.get("n_mode", "list"),
            "order": self.requested_ns(task_id, variant=selected, overlay_id=selected_overlay),
            "paused": bool(meta.get("paused")),
            "exhausted": False,
            "window": max(1, int(meta.get("max_concurrent_jobs") or self.settings.max_jobs_per_task)),
            "variant": selected,
            "overlay_id": selected_overlay,
        }

    def set_plan(self, task_id: str, plan: Mapping[str, Any]) -> None:
        if self.get_meta(task_id) is None:
            raise KeyError(task_id)
        if "paused" in plan:
            self.patch_meta(task_id, paused=bool(plan["paused"]))
        if "order" in plan:
            variant = str(plan.get("variant") or (self.get_meta(task_id) or {}).get("initial_variant", "raw"))
            self.add_requested_ns(task_id, list(plan["order"]), variant=variant, overlay_id=plan.get("overlay_id", 0))

    def add_requested_ns(
        self, task_id: str, ns: list[int], *, variant: str | None = None, overlay_id: int | None = 0
    ) -> dict[str, Any]:
        meta = self.get_meta(task_id)
        if meta is None:
            raise KeyError(task_id)
        selected = self._variant(variant or str(meta.get("initial_variant", "raw")))
        selected_overlay = normalize_overlay_id(overlay_id)
        if selected_overlay:
            self.ensure_analysis(task_id, variant=selected, overlay_id=selected_overlay)
        values = list(dict.fromkeys(int(n) for n in ns))
        if not values or any(n < 1 for n in values):
            raise ValueError("N должен быть положительным")
        if any(n > self.settings.max_n_value for n in values):
            raise ValueError(f"N превышает серверный лимит {self.settings.max_n_value}")
        params_base = {"task_id": task_id, "variant": selected, "overlay_id": selected_overlay}
        with self.database.begin() as conn:
            current = conn.execute(
                text("SELECT COUNT(*) FROM task_n_requests WHERE task_id=:task_id AND variant=:variant AND overlay_id=:overlay_id"),
                params_base,
            ).scalar_one()
            existing = set(
                int(n) for n in conn.execute(
                    text("SELECT n FROM task_n_requests WHERE task_id=:task_id AND variant=:variant AND overlay_id=:overlay_id"),
                    params_base,
                ).scalars().all()
            )
            new_values = [n for n in values if n not in existing]
            if int(current) + len(new_values) > self.settings.max_planned_n_values:
                raise ValueError(f"План превысит лимит {self.settings.max_planned_n_values} значений N")
            for n in values:
                if n in existing:
                    conn.execute(
                        text(
                            """
                            UPDATE task_n_requests
                            SET cancelled_at=NULL, status='requested', detail='{}'::jsonb, updated_at=now()
                            WHERE task_id=:task_id AND variant=:variant AND overlay_id=:overlay_id
                              AND n=:n AND cancelled_at IS NOT NULL
                            """
                        ),
                        {**params_base, "n": n},
                    )
            max_position = conn.execute(
                text("SELECT COALESCE(MAX(position), -1) FROM task_n_requests WHERE task_id=:task_id AND variant=:variant AND overlay_id=:overlay_id"),
                params_base,
            ).scalar_one()
            for offset, n in enumerate(new_values, start=1):
                conn.execute(
                    text(
                        """
                        INSERT INTO task_n_requests (task_id, variant, overlay_id, n, position, status)
                        VALUES (:task_id, :variant, :overlay_id, :n, :position, 'requested')
                        ON CONFLICT (task_id, variant, overlay_id, n) DO UPDATE SET
                            cancelled_at=NULL, status='requested', updated_at=now()
                        """
                    ),
                    {**params_base, "n": n, "position": int(max_position) + offset},
                )
            conn.execute(text("UPDATE tasks SET paused=false, state='running', updated_at=now() WHERE id=:task_id"), {"task_id": task_id})
        try:
            return self.get_plan(task_id, variant=selected, overlay_id=selected_overlay)
        except TypeError:  # compatibility with tiny historical test doubles
            return self.get_plan(task_id, variant=selected)

    # ---------- overlay-aware events ----------
    def publish_event(
        self, task_id: str, event_type: str, payload: Mapping[str, Any], *, overlay_id: int | None = 0
    ) -> str:
        if self.get_meta(task_id) is None:
            raise KeyError(task_id)
        selected_overlay = normalize_overlay_id(overlay_id)
        body = dict(json_safe_value(payload))
        with self.database.begin() as conn:
            event_id = conn.execute(
                text(
                    """
                    INSERT INTO task_events (task_id, overlay_id, event_type, payload)
                    VALUES (:task_id, :overlay_id, :event_type, CAST(:payload AS jsonb))
                    RETURNING id
                    """
                ),
                {"task_id": task_id, "overlay_id": selected_overlay, "event_type": event_type, "payload": _json_param(body)},
            ).scalar_one()
        return str(int(event_id))

    def read_events(
        self, task_id: str, after: str = "0-0", count: int = 200, *, overlay_id: int | None = None
    ) -> list[dict[str, Any]]:
        after_id = self._event_after_id(after)
        clauses = ["task_id=:task_id", "id > :after_id"]
        params: dict[str, Any] = {"task_id": task_id, "after_id": after_id, "count": max(1, int(count))}
        if overlay_id is not None:
            clauses.append("overlay_id=:overlay_id")
            params["overlay_id"] = normalize_overlay_id(overlay_id)
        with self.database.connect() as conn:
            rows = conn.execute(
                text(f"SELECT id, overlay_id, event_type, payload, created_at FROM task_events WHERE {' AND '.join(clauses)} ORDER BY id LIMIT :count"),
                params,
            ).mappings().all()
        return [
            {
                "id": str(int(row["id"])), "type": str(row["event_type"]), "task_id": task_id,
                "overlay_id": int(row["overlay_id"]), "time": _epoch(row["created_at"]),
                **dict(_json_value(row["payload"], {}) or {}),
            }
            for row in rows
        ]

    def all_events(
        self, task_id: str, start: int = 0, limit: int = 10_000, *, overlay_id: int | None = None
    ) -> list[dict[str, Any]]:
        clauses = ["task_id=:task_id"]
        params: dict[str, Any] = {"task_id": task_id, "offset": max(0, int(start)), "limit": max(1, int(limit))}
        if overlay_id is not None:
            clauses.append("overlay_id=:overlay_id")
            params["overlay_id"] = normalize_overlay_id(overlay_id)
        with self.database.connect() as conn:
            rows = conn.execute(
                text(f"SELECT id, overlay_id, event_type, payload, created_at FROM task_events WHERE {' AND '.join(clauses)} ORDER BY id OFFSET :offset LIMIT :limit"),
                params,
            ).mappings().all()
        return [
            {
                "id": str(int(row["id"])), "type": str(row["event_type"]), "task_id": task_id,
                "overlay_id": int(row["overlay_id"]), "time": _epoch(row["created_at"]),
                **dict(_json_value(row["payload"], {}) or {}),
            }
            for row in rows
        ]

    # ---------- overlay-aware N status / cancellation ----------
    def set_n_status(
        self, task_id: str, n: int, status: str, *, variant: str | None = None,
        overlay_id: int | None = 0, **extra: Any,
    ) -> None:
        meta = self.get_meta(task_id)
        if meta is None:
            raise KeyError(task_id)
        selected = self._variant(variant or str(meta.get("initial_variant", "raw")))
        selected_overlay = normalize_overlay_id(overlay_id)
        if selected_overlay:
            self.ensure_analysis(task_id, variant=selected, overlay_id=selected_overlay)
        base = {"task_id": task_id, "variant": selected, "overlay_id": selected_overlay}
        with self.database.begin() as conn:
            position = conn.execute(
                text("SELECT COALESCE(MAX(position), -1) + 1 FROM task_n_requests WHERE task_id=:task_id AND variant=:variant AND overlay_id=:overlay_id"),
                base,
            ).scalar_one()
            conn.execute(
                text(
                    """
                    INSERT INTO task_n_requests (task_id, variant, overlay_id, n, position, status, detail)
                    VALUES (:task_id, :variant, :overlay_id, :n, :position, :status, CAST(:detail AS jsonb))
                    ON CONFLICT (task_id, variant, overlay_id, n) DO UPDATE SET
                        status=EXCLUDED.status, detail=EXCLUDED.detail, updated_at=now()
                    """
                ),
                {**base, "n": int(n), "position": int(position), "status": str(status), "detail": _json_param(extra)},
            )

    def get_n_statuses(
        self, task_id: str, *, variant: str | None = None, overlay_id: int | None = 0
    ) -> dict[str, dict[str, Any]]:
        meta = self.get_meta(task_id) if variant is None else None
        selected = self._variant(variant or str((meta or {}).get("initial_variant", "raw")))
        selected_overlay = normalize_overlay_id(overlay_id)
        with self.database.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT n, status, detail, updated_at, cancelled_at
                    FROM task_n_requests
                    WHERE task_id=:task_id AND variant=:variant AND overlay_id=:overlay_id
                    ORDER BY position, n
                    """
                ),
                {"task_id": task_id, "variant": selected, "overlay_id": selected_overlay},
            ).mappings().all()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            detail = dict(_json_value(row["detail"], {}) or {})
            status = "cancelled" if row["cancelled_at"] is not None else str(row["status"])
            out[str(int(row["n"]))] = {
                "n": int(row["n"]), "status": status, "variant": selected, "overlay_id": selected_overlay,
                "updated_at": _epoch(row["updated_at"]), **detail,
            }
        return out

    def cancel_ns(
        self, task_id: str, ns: list[int], *, variant: str | None = None, overlay_id: int | None = 0
    ) -> None:
        if self.get_meta(task_id) is None:
            raise KeyError(task_id)
        targets = sorted({int(n) for n in ns if int(n) > 0})
        variants = [self._variant(variant)] if variant is not None else ["raw", "smooth"]
        selected_overlay = normalize_overlay_id(overlay_id)
        with self.database.begin() as conn:
            for selected in variants:
                for n in targets:
                    conn.execute(
                        text(
                            """
                            UPDATE task_n_requests SET cancelled_at=now(), status='cancelled', updated_at=now()
                            WHERE task_id=:task_id AND variant=:variant AND overlay_id=:overlay_id AND n=:n
                            """
                        ),
                        {"task_id": task_id, "variant": selected, "overlay_id": selected_overlay, "n": n},
                    )
        for n in targets:
            self.publish_event(task_id, "n_cancelled", {"n": n, "variant": variant or "all", "overlay_id": selected_overlay}, overlay_id=selected_overlay)

    def is_n_cancelled(
        self, task_id: str, n: int, *, variant: str = "raw", overlay_id: int | None = 0
    ) -> bool:
        with self.database.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT t.cancelled_at, r.cancelled_at AS n_cancelled_at
                    FROM tasks t
                    LEFT JOIN task_n_requests r
                      ON r.task_id=t.id AND r.variant=:variant AND r.overlay_id=:overlay_id AND r.n=:n
                    WHERE t.id=:task_id
                    """
                ),
                {"task_id": task_id, "variant": self._variant(variant), "overlay_id": normalize_overlay_id(overlay_id), "n": int(n)},
            ).mappings().first()
        return False if row is None else bool(row["cancelled_at"] is not None or row["n_cancelled_at"] is not None)

    # ---------- overlay-aware runtime artifacts ----------
    def _save_artifact(
        self, task_id: str, variant: str, artifact_key: str, artifact_type: str, value: Any, *,
        component_id: int | None = None, n: int | None = None, overlay_id: int | None = 0,
    ) -> dict[str, Any]:
        selected = self._variant(variant)
        selected_overlay = normalize_overlay_id(overlay_id)
        if selected_overlay:
            self.ensure_analysis(task_id, variant=selected, overlay_id=selected_overlay)
        payload, codec = encode_object(value)
        digest = sha256(payload)
        with self.database.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO runtime_artifacts (
                        task_id, variant, overlay_id, artifact_key, artifact_type, component_id, n, codec, payload, sha256
                    ) VALUES (
                        :task_id, :variant, :overlay_id, :artifact_key, :artifact_type, :component_id, :n, :codec, :payload, :sha256
                    )
                    ON CONFLICT (task_id, variant, overlay_id, artifact_key) DO UPDATE SET
                        artifact_type=EXCLUDED.artifact_type, component_id=EXCLUDED.component_id,
                        n=EXCLUDED.n, codec=EXCLUDED.codec, payload=EXCLUDED.payload,
                        sha256=EXCLUDED.sha256, updated_at=now()
                    """
                ),
                {
                    "task_id": task_id, "variant": selected, "overlay_id": selected_overlay,
                    "artifact_key": artifact_key, "artifact_type": artifact_type,
                    "component_id": component_id, "n": n, "codec": codec, "payload": payload, "sha256": digest,
                },
            )
        return {"bytes": len(payload), "sha256": digest, "codec": codec}

    def _load_artifact(self, task_id: str, variant: str, artifact_key: str, *, overlay_id: int | None = 0) -> Any | None:
        with self.database.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT payload, sha256 FROM runtime_artifacts
                    WHERE task_id=:task_id AND variant=:variant AND overlay_id=:overlay_id AND artifact_key=:artifact_key
                    """
                ),
                {"task_id": task_id, "variant": self._variant(variant), "overlay_id": normalize_overlay_id(overlay_id), "artifact_key": artifact_key},
            ).mappings().first()
        if row is None:
            return None
        payload = bytes(row["payload"])
        if sha256(payload) != str(row["sha256"]):
            raise IOError(f"artifact {artifact_key} повреждён")
        return decode_object(payload)

    def _delete_artifact(self, task_id: str, variant: str, artifact_key: str, *, overlay_id: int | None = 0) -> None:
        with self.database.begin() as conn:
            conn.execute(
                text("DELETE FROM runtime_artifacts WHERE task_id=:task_id AND variant=:variant AND overlay_id=:overlay_id AND artifact_key=:artifact_key"),
                {"task_id": task_id, "variant": self._variant(variant), "overlay_id": normalize_overlay_id(overlay_id), "artifact_key": artifact_key},
            )

    def _find_artifact(self, task_id: str, artifact_key: str, *, overlay_id: int | None = None) -> Any | None:
        clauses = ["task_id=:task_id", "artifact_key=:artifact_key"]
        params: dict[str, Any] = {"task_id": task_id, "artifact_key": artifact_key}
        if overlay_id is not None:
            clauses.append("overlay_id=:overlay_id")
            params["overlay_id"] = normalize_overlay_id(overlay_id)
        with self.database.connect() as conn:
            row = conn.execute(
                text(f"SELECT variant, payload, sha256 FROM runtime_artifacts WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT 1"),
                params,
            ).mappings().first()
        if row is None:
            return None
        payload = bytes(row["payload"])
        if sha256(payload) != str(row["sha256"]):
            raise IOError(f"artifact {artifact_key} повреждён")
        return decode_object(payload)

    def put_blob(self, task_id: str, name: str, payload: bytes, codec: str = "bytes") -> dict[str, Any]:
        return self._save_artifact(task_id, "raw", name, "blob", bytes(payload), overlay_id=0)

    def get_blob(self, task_id: str, name: str) -> bytes:
        value = self._load_artifact(task_id, "raw", name, overlay_id=0)
        if value is None:
            raise KeyError(f"blob {name} for task={task_id} not found")
        return bytes(value)

    def delete_blob(self, task_id: str, name: str) -> None:
        self._delete_artifact(task_id, "raw", name, overlay_id=0)

    # ---------- overlay-aware field / components / problems ----------
    def save_field(
        self, task_id: str, value: Mapping[str, Any], *, variant: str = "raw", overlay_id: int | None = 0
    ) -> None:
        selected = self._variant(variant)
        selected_overlay = normalize_overlay_id(overlay_id)
        record = dict(value)
        record.pop("start_polygons", None)
        decomposition = dict(record.get("decomposition", {}) or {})
        decomposition.pop("components", None)
        indices = {
            key: [int(x) for x in decomposition.pop(key, [])]
            for key in ("active_indices", "background_only_indices", "removed_indices", "degenerate_indices")
        }
        record["decomposition"] = decomposition
        self._save_artifact(task_id, selected, "field", "field", record, overlay_id=selected_overlay)
        self.ensure_analysis(task_id, variant=selected, overlay_id=selected_overlay)
        with self.database.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE task_analyses SET
                        preparation_state='preparing', active_indices=:active_indices,
                        background_only_indices=:background_only_indices, removed_indices=:removed_indices,
                        degenerate_indices=:degenerate_indices, updated_at=now()
                    WHERE task_id=:task_id AND variant=:variant AND overlay_id=:overlay_id
                    """
                ),
                {
                    "task_id": task_id, "variant": selected, "overlay_id": selected_overlay,
                    "active_indices": indices["active_indices"],
                    "background_only_indices": indices["background_only_indices"],
                    "removed_indices": indices["removed_indices"],
                    "degenerate_indices": indices["degenerate_indices"],
                },
            )

    def load_field(
        self, task_id: str, *, variant: str = "raw", overlay_id: int | None = 0
    ) -> dict[str, Any]:
        selected = self._variant(variant)
        selected_overlay = normalize_overlay_id(overlay_id)
        value = self._load_artifact(task_id, selected, "field", overlay_id=selected_overlay)
        if value is None:
            return {}
        analysis = self.analysis_state(task_id, variant=selected, overlay_id=selected_overlay) or {}
        row = dict(value)
        decomposition = dict(row.get("decomposition", {}) or {})
        for key in ("active_indices", "background_only_indices", "removed_indices", "degenerate_indices"):
            decomposition[key] = list(analysis.get(key, []))
        row["decomposition"] = decomposition
        resolved = self.resolved_source_polygons(task_id, variant=selected, overlay_id=selected_overlay)
        physical = [item for item in resolved if item.get("overlay_state") != "removed"]
        geometry_rows = geometry_polygons(physical)
        for target, source in zip(geometry_rows, physical):
            target.update(
                source_index=int(source["source_index"]), overlay_state=str(source["overlay_state"]),
                active=bool(source["active"]), real=bool(source["real"]),
            )
        row["start_polygons"] = geometry_rows
        row["overlay_id"] = selected_overlay
        return row

    def save_component(
        self, task_id: str, component_id: Any, value: Mapping[str, Any], *,
        variant: str = "raw", overlay_id: int | None = 0,
    ) -> None:
        selected = self._variant(variant)
        selected_overlay = normalize_overlay_id(overlay_id)
        if selected_overlay:
            self.ensure_analysis(task_id, variant=selected, overlay_id=selected_overlay)
        cid = self.component_db_id(component_id)
        row = dict(value)
        component = dict(row.get("component", {}) or {})
        info = dict(row.get("info", {}) or {})
        if not info and component:
            info = {
                "id": cid, "polygon_indices": component.get("polygon_indices", []), "classes": component.get("classes", []),
                "loads": component.get("loads", []), "bounds": component.get("bounds"),
                "demand_bounds": component.get("demand_bounds"), "max_useful_n": row.get("max_useful_n"),
                "state": row.get("state", "created"),
            }
        n_bounds = row.get("bounds") if isinstance(row.get("bounds"), Mapping) else None
        with self.database.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO components (
                        task_id, variant, overlay_id, component_id, state, polygon_indices, classes, loads,
                        bounds, demand_bounds, max_useful_n, n_bounds, planned_ns, force_single_box
                    ) VALUES (
                        :task_id, :variant, :overlay_id, :component_id, :state, :polygon_indices, :classes, :loads,
                        :bounds, :demand_bounds, :max_useful_n, CAST(:n_bounds AS jsonb), :planned_ns, :force_single_box
                    )
                    ON CONFLICT (task_id, variant, overlay_id, component_id) DO UPDATE SET
                        state=EXCLUDED.state, polygon_indices=EXCLUDED.polygon_indices,
                        classes=EXCLUDED.classes, loads=EXCLUDED.loads, bounds=EXCLUDED.bounds,
                        demand_bounds=EXCLUDED.demand_bounds, max_useful_n=EXCLUDED.max_useful_n,
                        n_bounds=EXCLUDED.n_bounds, planned_ns=EXCLUDED.planned_ns,
                        force_single_box=EXCLUDED.force_single_box, updated_at=now()
                    """
                ),
                {
                    "task_id": task_id, "variant": selected, "overlay_id": selected_overlay, "component_id": cid,
                    "state": str(row.get("state", info.get("state", "created"))),
                    "polygon_indices": [int(x) for x in info.get("polygon_indices", component.get("polygon_indices", []))],
                    "classes": [int(x) for x in info.get("classes", component.get("classes", []))],
                    "loads": [float(x) for x in info.get("loads", component.get("loads", []))],
                    "bounds": [float(x) for x in info.get("bounds", [])] if info.get("bounds") else None,
                    "demand_bounds": [float(x) for x in info.get("demand_bounds", [])] if info.get("demand_bounds") else None,
                    "max_useful_n": None if row.get("max_useful_n") is None else int(row["max_useful_n"]),
                    "n_bounds": None if n_bounds is None else _json_param(n_bounds),
                    "planned_ns": [int(x) for x in row.get("plan", [])], "force_single_box": bool(row.get("force_single_box", False)),
                },
            )
        if component:
            self._save_artifact(task_id, selected, f"component:{cid}", "component", component, component_id=cid, overlay_id=selected_overlay)

    def load_component(
        self, task_id: str, component_id: Any, *, variant: str = "raw", overlay_id: int | None = 0
    ) -> dict[str, Any] | None:
        selected = self._variant(variant)
        selected_overlay = normalize_overlay_id(overlay_id)
        cid = self.component_db_id(component_id)
        with self.database.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT * FROM components
                    WHERE task_id=:task_id AND variant=:variant AND overlay_id=:overlay_id AND component_id=:component_id
                    """
                ),
                {"task_id": task_id, "variant": selected, "overlay_id": selected_overlay, "component_id": cid},
            ).mappings().first()
        if row is None:
            return None
        component = self._load_artifact(task_id, selected, f"component:{cid}", overlay_id=selected_overlay) or {}
        info = {
            "id": cid, "polygon_indices": list(row["polygon_indices"] or []), "classes": list(row["classes"] or []),
            "loads": [float(x) for x in row["loads"] or []], "bounds": list(row["bounds"]) if row["bounds"] is not None else None,
            "demand_bounds": list(row["demand_bounds"]) if row["demand_bounds"] is not None else None,
            "max_useful_n": None if row["max_useful_n"] is None else int(row["max_useful_n"]),
            "prepared": row["max_useful_n"] is not None, "state": str(row["state"]),
        }
        result: dict[str, Any] = {
            "component": dict(component), "info": info, "state": str(row["state"]),
            "plan": [int(x) for x in row["planned_ns"] or []], "force_single_box": bool(row["force_single_box"]),
            "max_useful_n": None if row["max_useful_n"] is None else int(row["max_useful_n"]),
            "variant": selected, "smooth": selected == "smooth", "overlay_id": selected_overlay,
        }
        if row["n_bounds"] is not None:
            result["bounds"] = _json_value(row["n_bounds"], {})
        return result

    def component_ids(self, task_id: str, *, variant: str = "raw", overlay_id: int | None = 0) -> list[str]:
        with self.database.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT component_id FROM components
                    WHERE task_id=:task_id AND variant=:variant AND overlay_id=:overlay_id
                    ORDER BY CASE WHEN component_id=-1 THEN 1 ELSE 0 END, component_id
                    """
                ),
                {"task_id": task_id, "variant": self._variant(variant), "overlay_id": normalize_overlay_id(overlay_id)},
            ).scalars().all()
        return [self.component_public_id(int(cid)) for cid in rows]

    def components(
        self, task_id: str, *, variant: str = "raw", overlay_id: int | None = 0
    ) -> list[dict[str, Any]]:
        return [
            value for cid in self.component_ids(task_id, variant=variant, overlay_id=overlay_id)
            if (value := self.load_component(task_id, cid, variant=variant, overlay_id=overlay_id)) is not None
        ]

    def save_problem(
        self, task_id: str, component_id: Any, value: Mapping[str, Any], *,
        variant: str = "raw", overlay_id: int | None = 0,
    ) -> None:
        cid = self.component_db_id(component_id)
        self._save_artifact(task_id, variant, f"problem:{cid}", "problem", dict(value), component_id=cid, overlay_id=overlay_id)

    def load_problem(
        self, task_id: str, component_id: Any, *, variant: str = "raw", overlay_id: int | None = 0
    ) -> dict[str, Any] | None:
        value = self._load_artifact(task_id, variant, f"problem:{self.component_db_id(component_id)}", overlay_id=overlay_id)
        return None if value is None else dict(value)

    def save_solver_result(
        self, task_id: str, component_id: Any, n: int, value: Mapping[str, Any], *,
        variant: str = "raw", overlay_id: int | None = 0,
    ) -> None:
        cid = self.component_db_id(component_id)
        self._save_artifact(
            task_id, variant, f"solver:{cid}:{int(n)}", "solver_result", dict(value),
            component_id=cid, n=int(n), overlay_id=overlay_id,
        )

    def load_solver_result(
        self, task_id: str, component_id: Any, n: int, *, variant: str = "raw", overlay_id: int | None = 0
    ) -> dict[str, Any] | None:
        value = self._load_artifact(
            task_id, variant, f"solver:{self.component_db_id(component_id)}:{int(n)}", overlay_id=overlay_id
        )
        return None if value is None else dict(value)

    # ---------- overlay-aware component frontier ----------
    def save_frontier_result(
        self, task_id: str, component_id: Any, n: int, value: Mapping[str, Any], *,
        variant: str = "raw", overlay_id: int | None = 0,
    ) -> None:
        selected = self._variant(variant)
        selected_overlay = normalize_overlay_id(overlay_id)
        cid = self.component_db_id(component_id)
        result = dict(json_safe_value(dict(value)))
        is_feasible = bool(result.get("is_feasible", False))
        is_optimal = bool(result.get("is_optimal", False))
        if is_feasible and is_optimal:
            status = "optimal"
        elif is_feasible:
            status = "feasible"
        else:
            status = str(result.get("status", result.get("solve_state", "infeasible"))).lower()
        result["status"] = status
        result["overlay_id"] = selected_overlay
        proxy = result.get("proxy_mass", result.get("total_cost"))
        with self.database.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO component_results (
                        task_id, variant, overlay_id, component_id, n, status, is_feasible, is_optimal, proxy_mass, result
                    ) VALUES (
                        :task_id, :variant, :overlay_id, :component_id, :n, :status, :is_feasible, :is_optimal,
                        :proxy_mass, CAST(:result AS jsonb)
                    )
                    ON CONFLICT (task_id, variant, overlay_id, component_id, n) DO UPDATE SET
                        status=EXCLUDED.status, is_feasible=EXCLUDED.is_feasible,
                        is_optimal=EXCLUDED.is_optimal, proxy_mass=EXCLUDED.proxy_mass,
                        result=EXCLUDED.result, updated_at=now()
                    """
                ),
                {
                    "task_id": task_id, "variant": selected, "overlay_id": selected_overlay,
                    "component_id": cid, "n": int(n), "status": status,
                    "is_feasible": is_feasible, "is_optimal": is_optimal,
                    "proxy_mass": None if proxy is None else float(proxy), "result": _json_param(result),
                },
            )
            conn.execute(
                text(
                    """
                    UPDATE task_analyses SET frontier_version=frontier_version+1, updated_at=now()
                    WHERE task_id=:task_id AND variant=:variant AND overlay_id=:overlay_id
                    """
                ),
                {"task_id": task_id, "variant": selected, "overlay_id": selected_overlay},
            )
        self._delete_artifact(task_id, selected, f"solver:{cid}:{int(n)}", overlay_id=selected_overlay)

    def frontier_version(self, task_id: str, *, variant: str = "raw", overlay_id: int | None = 0) -> int:
        with self.database.connect() as conn:
            value = conn.execute(
                text("SELECT frontier_version FROM task_analyses WHERE task_id=:task_id AND variant=:variant AND overlay_id=:overlay_id"),
                {"task_id": task_id, "variant": self._variant(variant), "overlay_id": normalize_overlay_id(overlay_id)},
            ).scalar_one_or_none()
        return int(value or 0)

    def load_frontier(
        self, task_id: str, component_id: Any, *, variant: str = "raw", overlay_id: int | None = 0
    ) -> dict[int, dict[str, Any]]:
        with self.database.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT n, result FROM component_results
                    WHERE task_id=:task_id AND variant=:variant AND overlay_id=:overlay_id AND component_id=:component_id
                    ORDER BY n
                    """
                ),
                {
                    "task_id": task_id, "variant": self._variant(variant), "overlay_id": normalize_overlay_id(overlay_id),
                    "component_id": self.component_db_id(component_id),
                },
            ).mappings().all()
        return {int(row["n"]): dict(_json_value(row["result"], {}) or {}) for row in rows}

    def all_frontiers(
        self, task_id: str, include_whole: bool = False, *, variant: str = "raw", overlay_id: int | None = 0
    ) -> dict[Any, dict[int, dict[str, Any]]]:
        out: dict[Any, dict[int, dict[str, Any]]] = {}
        for cid in self.component_ids(task_id, variant=variant, overlay_id=overlay_id):
            if cid == "whole" and not include_whole:
                continue
            frontier = self.load_frontier(task_id, cid, variant=variant, overlay_id=overlay_id)
            if frontier:
                key: Any = "whole" if cid == "whole" else int(cid)
                out[key] = frontier
        return out

    # ---------- overlay-aware candidates / solutions ----------
    def save_candidate(self, task_id: str, candidate_id: str, value: Mapping[str, Any]) -> None:
        selected = self._variant(str(value.get("variant", "raw")))
        selected_overlay = normalize_overlay_id(value.get("overlay_id", 0))
        self._save_artifact(
            task_id, selected, f"candidate:{candidate_id}", "candidate", dict(value), overlay_id=selected_overlay
        )

    def load_candidate(
        self, task_id: str, candidate_id: str, *, overlay_id: int | None = None
    ) -> dict[str, Any] | None:
        value = self._find_artifact(task_id, f"candidate:{candidate_id}", overlay_id=overlay_id)
        return None if value is None else dict(value)

    def save_solution(self, task_id: str, solution: Mapping[str, Any]) -> None:
        row = dict(solution)
        sid = str(row["solution_id"])
        variant = self._variant(str(row.get("variant", "raw")))
        selected_overlay = normalize_overlay_id(row.get("overlay_id", 0))
        if selected_overlay:
            self.ensure_analysis(task_id, variant=variant, overlay_id=selected_overlay)
        result = dict(json_safe_value(row))
        result["variant"] = variant
        result["smooth"] = variant == "smooth"
        result["overlay_id"] = selected_overlay
        proxy = result.get("proxy_mass")
        mass = result.get("actual_mass_kg")
        validation = result.get("validation")
        with self.database.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO solutions (
                        solution_id, task_id, variant, overlay_id, source, total_n, component_ns,
                        proxy_mass, actual_mass_kg, is_feasible, is_optimal, status, result, validation
                    ) VALUES (
                        :solution_id, :task_id, :variant, :overlay_id, :source, :total_n, CAST(:component_ns AS jsonb),
                        :proxy_mass, :actual_mass_kg, :is_feasible, :is_optimal, :status,
                        CAST(:result AS jsonb), CAST(:validation AS jsonb)
                    )
                    ON CONFLICT (solution_id) DO UPDATE SET
                        source=EXCLUDED.source, total_n=EXCLUDED.total_n, component_ns=EXCLUDED.component_ns,
                        proxy_mass=EXCLUDED.proxy_mass, actual_mass_kg=EXCLUDED.actual_mass_kg,
                        is_feasible=EXCLUDED.is_feasible, is_optimal=EXCLUDED.is_optimal,
                        status=EXCLUDED.status, result=EXCLUDED.result, validation=EXCLUDED.validation,
                        updated_at=now()
                    """
                ),
                {
                    "solution_id": sid, "task_id": task_id, "variant": variant, "overlay_id": selected_overlay,
                    "source": str(result.get("source", "components")),
                    "total_n": int(result.get("total_N", result.get("total_n", 0))),
                    "component_ns": _json_param(result.get("component_ns", {})),
                    "proxy_mass": None if proxy is None else float(proxy),
                    "actual_mass_kg": None if mass is None or not math.isfinite(float(mass)) else float(mass),
                    "is_feasible": bool(result.get("is_feasible", False)), "is_optimal": bool(result.get("is_optimal", False)),
                    "status": str(result.get("status", "unknown")), "result": _json_param(result),
                    "validation": None if validation is None else _json_param(validation),
                },
            )
        candidate_id = result.get("candidate_id")
        if candidate_id:
            self._delete_artifact(task_id, variant, f"candidate:{candidate_id}", overlay_id=selected_overlay)

    def load_solution(
        self, task_id: str, solution_id: str, *, overlay_id: int | None = None
    ) -> dict[str, Any] | None:
        clauses = ["task_id=:task_id", "solution_id=:solution_id"]
        params: dict[str, Any] = {"task_id": task_id, "solution_id": solution_id}
        if overlay_id is not None:
            clauses.append("overlay_id=:overlay_id")
            params["overlay_id"] = normalize_overlay_id(overlay_id)
        with self.database.connect() as conn:
            value = conn.execute(text(f"SELECT result FROM solutions WHERE {' AND '.join(clauses)}"), params).scalar_one_or_none()
        return None if value is None else dict(_json_value(value, {}) or {})

    def solutions(
        self, task_id: str, total_n: int | None = None, source: str | None = None,
        variant: str | None = None, overlay_id: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["task_id=:task_id"]
        params: dict[str, Any] = {"task_id": task_id}
        if total_n is not None:
            clauses.append("total_n=:total_n")
            params["total_n"] = int(total_n)
        if source is not None:
            clauses.append("source=:source")
            params["source"] = str(source)
        if variant is not None:
            clauses.append("variant=:variant")
            params["variant"] = self._variant(variant)
        if overlay_id is not None:
            clauses.append("overlay_id=:overlay_id")
            params["overlay_id"] = normalize_overlay_id(overlay_id)
        sql = f"""
            SELECT result FROM solutions WHERE {' AND '.join(clauses)}
            ORDER BY is_feasible DESC, is_optimal DESC, actual_mass_kg ASC NULLS LAST,
                     proxy_mass ASC NULLS LAST, created_at ASC
        """
        with self.database.connect() as conn:
            values = conn.execute(text(sql), params).scalars().all()
        return [dict(_json_value(value, {}) or {}) for value in values]

    def best_solution(
        self, task_id: str, total_n: int, *, variant: str | None = None, overlay_id: int | None = 0
    ) -> dict[str, Any] | None:
        rows = self.solutions(task_id, total_n=total_n, variant=variant, overlay_id=normalize_overlay_id(overlay_id))
        return rows[0] if rows else None

    def get_result(self, task_id: str, n: int, *, variant: str | None = None, overlay_id: int | None = 0) -> dict[str, Any] | None:
        selected_variant = variant or str((self.get_meta(task_id) or {}).get("initial_variant", "raw"))
        solution = self.best_solution(task_id, int(n), variant=selected_variant, overlay_id=overlay_id)
        if solution is None:
            return None
        from .pipeline import to_compat_result
        return to_compat_result(solution)

    def get_result_meta(
        self, task_id: str, n: int, *, variant: str | None = None, overlay_id: int | None = 0
    ) -> dict[str, Any] | None:
        selected_variant = variant or str((self.get_meta(task_id) or {}).get("initial_variant", "raw"))
        solution = self.best_solution(task_id, int(n), variant=selected_variant, overlay_id=overlay_id)
        if solution is None:
            return None
        return {
            "n": int(n), "kind": "final", "is_feasible": bool(solution.get("is_feasible")),
            "is_optimal": bool(solution.get("is_optimal")),
            "total_cost": solution.get("actual_mass_kg", solution.get("proxy_mass")),
            "postprocessed": True, "solution_id": solution.get("solution_id"),
            "variant": selected_variant, "overlay_id": normalize_overlay_id(overlay_id),
        }

    def get_result_metas(
        self, task_id: str, *, variant: str | None = None, overlay_id: int | None = 0
    ) -> dict[str, dict[str, Any]]:
        selected_variant = variant or str((self.get_meta(task_id) or {}).get("initial_variant", "raw"))
        rows = self.solutions(task_id, variant=selected_variant, overlay_id=normalize_overlay_id(overlay_id))
        best: dict[int, dict[str, Any]] = {}
        for row in rows:
            n = int(row.get("total_N", row.get("total_n", -1)))
            if n < 0 or n in best:
                continue
            best[n] = {
                "n": n, "kind": "final", "is_feasible": bool(row.get("is_feasible")),
                "is_optimal": bool(row.get("is_optimal")),
                "total_cost": row.get("actual_mass_kg", row.get("proxy_mass")),
                "postprocessed": True, "solution_id": row.get("solution_id"),
                "variant": selected_variant, "overlay_id": normalize_overlay_id(overlay_id),
            }
        return {str(n): value for n, value in sorted(best.items())}

    def mark_analysis_failed(
        self, task_id: str, *, variant: str = "raw", overlay_id: int | None = 0
    ) -> None:
        with self.database.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE task_analyses
                    SET preparation_state='failed', updated_at=now()
                    WHERE task_id=:task_id AND variant=:variant AND overlay_id=:overlay_id
                      AND preparation_state <> 'prepared'
                    """
                ),
                {
                    "task_id": task_id,
                    "variant": self._variant(variant),
                    "overlay_id": normalize_overlay_id(overlay_id),
                },
            )


    def has_prepared_analysis(self, task_id: str) -> bool:
        with self.database.connect() as conn:
            value = conn.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM task_analyses
                        WHERE task_id=:task_id AND preparation_state='prepared'
                    )
                    """
                ),
                {"task_id": task_id},
            ).scalar_one()
        return bool(value)

    def refresh_pipeline_state(self, task_id: str) -> dict[str, Any] | None:
        meta = self.get_meta(task_id)
        if meta is None:
            return None
        if meta.get("cancelled") or meta.get("paused"):
            return meta
        pending = self.pending_jobs(task_id)
        if pending:
            state = "running"
        elif meta.get("manual_mode"):
            prepared_check = getattr(self, "has_prepared_analysis", None)
            if callable(prepared_check):
                prepared = bool(prepared_check(task_id))
            else:
                variant = str(meta.get("initial_variant", "raw"))
                try:
                    prepared = bool(self.load_field(task_id, variant=variant))
                except TypeError:
                    prepared = bool(self.load_field(task_id))
            state = "ready" if prepared else "uploaded"
        else:
            state = "completed" if self.solutions(task_id) else "completed_with_errors"
        if state != meta.get("state"):
            meta = self.patch_meta(task_id, state=state)
            self.publish_event(task_id, "task_state", {"state": state})
        return meta
