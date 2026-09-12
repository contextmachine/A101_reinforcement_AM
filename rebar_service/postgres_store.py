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
from .overlays import normalize_overlay_id, resolve_overlay, resolve_overlay_selector
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


_GEOJSON_GEOMETRY_TYPES = {
    "Point",
    "LineString",
    "Polygon",
    "MultiPoint",
    "MultiLineString",
    "MultiPolygon",
    "GeometryCollection",
}


def restore_json_geometries(value: Any) -> Any:
    """Rehydrate GeoJSON geometries stored inside algorithm JSON results."""

    if isinstance(value, Mapping):
        geometry_type = value.get("type")
        if (
            geometry_type in _GEOJSON_GEOMETRY_TYPES
            and ("coordinates" in value or "geometries" in value)
        ):
            from shapely.geometry import shape

            try:
                return shape(dict(value))
            except (TypeError, ValueError):
                pass
        return {str(key): restore_json_geometries(item) for key, item in value.items()}
    if isinstance(value, list):
        return [restore_json_geometries(item) for item in value]
    return value


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
        "scene_id",
        "analysis_variant",
        "analysis_overlay_id",
        "component_selection",
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
        filename = (
            str(input_obj.get("filename"))
            if input_obj.get("filename")
            else None
        )

        content = input_obj.get("content")

        source_bytes = (
            bytes(content)
            if isinstance(content, (bytes, bytearray, memoryview))
            else None
        )

        if deferred_source and source_bytes is None:
            raise ValueError(
                f"source content is required for deferred source kind={kind}"
            )

        source_sha256 = sha256(
            source_bytes
            if source_bytes is not None
            else _json_param(variants["raw"]).encode("utf-8")
        )
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
            if str(row["kind"]) == "scene_ref":
                scene_id = dict(_json_value(row["metadata"], {}) or {}).get("scene_id")
                with self.database.connect() as conn:
                    row = conn.execute(
                        text("SELECT kind, filename, content, sha256, metadata FROM scene_sources WHERE scene_id=:scene_id"),
                        {"scene_id": scene_id},
                    ).mappings().first()
                if row is None:
                    raise KeyError(f"source scene for task={task_id} not found")
            kind = str(row["kind"])
            metadata = dict(_json_value(row["metadata"], {}) or {})
            if kind in {"dxf", "xlsx_tables", "json", "pickle"}:
                content = bytes(row["content"] or b"")
                if sha256(content) != str(row["sha256"]):
                    raise IOError(f"source input for task={task_id} повреждён")
                return {
                    "kind": kind,
                    "filename": row["filename"] or f"input.{kind}",
                    "content": content,
                    **metadata,
                }
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
        analysis_variant = str(row.get("analysis_variant") or initial_variant)
        analysis_overlay_id = int(row.get("analysis_overlay_id") or 0)
        scene_id = str(row.get("scene_id") or task_id)
        component_selection = [
            int(x) for x in (row.get("component_selection") or ([-2] if bool(row["whole"]) else [-3]))
        ]
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
                "requested_n": self.requested_ns(
                    task_id, variant=analysis_variant, overlay_id=analysis_overlay_id
                ),
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
                "scene_id": scene_id,
                "analysis_variant": analysis_variant,
                "analysis_smooth": analysis_variant == "smooth",
                "analysis_overlay_id": analysis_overlay_id,
                "overlay_id": analysis_overlay_id,
                "component_selection": component_selection,
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

        scene_id = str(meta.get("scene_id") or task_id)
        legacy = scene_id == str(task_id)
        if legacy:
            # Revision-0003 canonicalizes historical frontend reads to raw/base.
            variant = "raw"
            overlay_id = 0
        else:
            variant = self._variant(
                str(meta.get("analysis_variant") or meta.get("initial_variant") or "raw")
            )
            overlay_id = int(meta.get("analysis_overlay_id", 0) or 0)

        statuses = self.get_n_statuses(
            task_id, variant=variant, overlay_id=overlay_id
        )
        counts: dict[str, int] = {}
        for value in statuses.values():
            status = str(value.get("status", "unknown"))
            counts[status] = counts.get(status, 0) + 1
        return {
            "task": meta,
            "scene_id": scene_id,
            "variant": variant,
            "smooth": variant == "smooth",
            "overlay_id": overlay_id,
            "plan": self.get_plan(task_id, variant=variant, overlay_id=overlay_id),
            "n": statuses,
            "status_counts": counts,
            "results": self.get_result_metas(
                task_id, variant=variant, overlay_id=overlay_id
            ),
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
        deferred_source = kind in {"dxf", "xlsx_tables", "json", "pickle"}
        scene_ref = kind == "scene_ref"
        if scene_ref:
            ref = str(input_obj["scene_id"])
            scene = self.get_scene(ref)
            if scene is None or str(scene.get("state")) != "ready":
                raise ValueError("Scene is not ready")
            # Source parsing/smoothing belongs to scene materialization, not PUT.
            variants = {"raw": [], "smooth": []}  # SQL copies immutable scene rows below.
        else:
            variants = {"raw": [], "smooth": []} if deferred_source else build_polygon_variants(input_obj)
        initial_variant = self._variant(str(meta.get("initial_variant", "raw")))
        scene_id = str(meta.get("scene_id") or task_id)
        analysis_variant = self._variant(str(meta.get("analysis_variant") or initial_variant))
        analysis_overlay_id = normalize_overlay_id(meta.get("analysis_overlay_id", 0))
        component_selection = [
            int(x) for x in (meta.get("component_selection") or ([-2] if bool(meta.get("whole", False)) else [-3]))
        ]
        requested = list(dict.fromkeys(int(n) for n in plan.get("order", meta.get("requested_n", []))))
        filename = (
            str(input_obj.get("filename"))
            if input_obj.get("filename")
            else None
        )

        content = input_obj.get("content")

        source_bytes = (
            bytes(content)
            if isinstance(content, (bytes, bytearray, memoryview))
            else None
        )

        if deferred_source and source_bytes is None:
            raise ValueError(
                f"source content is required for deferred source kind={kind}"
            )

        source_sha256 = sha256(
            source_bytes
            if source_bytes is not None
            else _json_param(variants["raw"]).encode("utf-8")
        )
        source_meta = {
            key: json_safe_value(value)
            for key, value in input_obj.items()
            if key not in {"content", "polygons"}
        }
        # Compatibility: production callers that predate scene-aware API still
        # get a scene. Lightweight unit-test databases may expose only begin().
        if hasattr(self.database, "connect") and self.get_scene(scene_id) is None:
            self.create_scene(
                scene_id,
                {"state": "preparing" if deferred_source else "ready", "created_at": meta.get("created_at", time.time())},
                input_obj,
            )

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
                            scene_id, analysis_variant, analysis_overlay_id, component_selection,
                            created_at, updated_at
                        ) VALUES (
                            :id, :state, CAST(:parameters AS jsonb), :n_mode, CAST(:n_source AS jsonb),
                            :scan_mode, :whole, :top_k, :validate_results, :max_jobs, :manual_mode,
                            :initial_variant, :paused, NULL, 0,
                            :scene_id, :analysis_variant, :analysis_overlay_id, :component_selection,
                            :created_at, :updated_at
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
                        "scene_id": scene_id,
                        "analysis_variant": analysis_variant,
                        "analysis_overlay_id": analysis_overlay_id,
                        "component_selection": component_selection,
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
                    if scene_ref:
                        conn.execute(text("""
                            INSERT INTO task_variants (task_id, variant, polygons, smoothing_metadata,
                                                       preparation_state, created_at, updated_at)
                            SELECT :task_id, variant, polygons, smoothing_metadata, 'stored', :created_at, :created_at
                            FROM scene_variants WHERE scene_id=:scene_id AND variant=:variant
                        """), {"task_id": task_id, "scene_id": scene_id, "variant": variant, "created_at": now})
                    else:
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
                    if variant != analysis_variant:
                        continue
                    conn.execute(
                        text(
                            """
                            INSERT INTO task_analyses (task_id, variant, overlay_id, preparation_state, created_at, updated_at)
                            VALUES (:task_id, :variant, :overlay_id, 'stored', :created_at, :created_at)
                            """
                        ),
                        {"task_id": task_id, "variant": variant, "overlay_id": analysis_overlay_id, "created_at": now},
                    )
                    for position, n in enumerate(requested):
                        conn.execute(
                            text(
                                """
                                INSERT INTO task_n_requests (
                                    task_id, variant, overlay_id, n, position, status, requested_at, updated_at
                                ) VALUES (
                                    :task_id, :variant, :overlay_id, :n, :position, 'requested', :created_at, :created_at
                                )
                                ON CONFLICT (task_id, variant, overlay_id, n) DO NOTHING
                                """
                            ),
                            {"task_id": task_id, "variant": variant, "overlay_id": analysis_overlay_id, "n": int(n), "position": position, "created_at": now},
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
                    "SELECT kind, filename, content, sha256, metadata FROM task_sources "
                    "WHERE task_id=:task_id"
                ),
                {"task_id": task_id},
            ).mappings().first()
            if source is None:
                raise KeyError(f"source input for task={task_id} not found")
            kind = str(source["kind"])
            if kind not in {"dxf", "xlsx_tables", "json", "pickle"}:
                raise ValueError(f"source_pending is not supported for source kind={kind}: {task_id}")

            content = bytes(source["content"] or b"")
            if sha256(content) != str(source["sha256"]):
                raise IOError(f"source input for task={task_id} повреждён")
            metadata = dict(_json_value(source["metadata"], {}) or {})
            if kind == "dxf":
                input_obj = {"kind": "dxf", "filename": source["filename"] or "input.dxf", "content": content}
            elif kind == "xlsx_tables":
                from .source_polygons import source_polygons_from_xlsx_bundle
                input_obj = source_polygons_from_xlsx_bundle(content, load_column=int(metadata["load_column"]))
            elif kind == "json":
                from .source_polygons import source_polygons_from_json_bytes
                input_obj = source_polygons_from_json_bytes(content)
            else:
                from .safe_pickle import load_source_polygons_pickle
                input_obj = load_source_polygons_pickle(content, max_polygons=int(self.settings.max_source_polygons))
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

    # ---------- reusable scene source / variants / stable components ----------
    def create_scene(
        self,
        scene_id: str,
        meta: Mapping[str, Any],
        input_obj: Mapping[str, Any],
    ) -> None:
        """Persist reusable source data independently from any analysis task."""

        kind = str(input_obj.get("kind", "polygons"))
        source_requires_content = kind in {"dxf", "xlsx_tables", "json", "pickle"}
        materialized_input: Mapping[str, Any] = input_obj
        if kind == "xlsx_tables":
            from .source_polygons import source_polygons_from_xlsx_bundle

            materialized_input = source_polygons_from_xlsx_bundle(
                bytes(input_obj.get("content") or b""),
                load_column=int(input_obj.get("load_column", 1)),
            )
        elif kind == "json":
            from .source_polygons import source_polygons_from_json_bytes

            materialized_input = source_polygons_from_json_bytes(bytes(input_obj.get("content") or b""))
        elif kind == "pickle":
            from .safe_pickle import load_source_polygons_pickle

            materialized_input = load_source_polygons_pickle(
                bytes(input_obj.get("content") or b""),
                max_polygons=int(self.settings.max_source_polygons),
            )
        variants = build_polygon_variants(materialized_input)
        content = input_obj.get("content")
        source_bytes = bytes(content) if isinstance(content, (bytes, bytearray, memoryview)) else None
        if source_requires_content and source_bytes is None:
            raise ValueError(f"source content is required for uploaded scene kind={kind}")

        source_sha256 = sha256(
            source_bytes if source_bytes is not None else _json_param(variants["raw"]).encode("utf-8")
        )
        source_meta = {
            key: json_safe_value(value)
            for key, value in input_obj.items()
            if key not in {"content", "polygons"}
        }
        metadata = dict(meta.get("metadata", {}) or {})
        now = _utc_from_epoch(meta.get("created_at", time.time()))
        state = str(meta.get("state") or "ready")

        try:
            with self.database.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO scenes (id, state, metadata, created_at, updated_at)
                        VALUES (:id, :state, CAST(:metadata AS jsonb), :created_at, :created_at)
                        """
                    ),
                    {
                        "id": scene_id,
                        "state": state,
                        "metadata": _json_param(metadata),
                        "created_at": now,
                    },
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO scene_sources (scene_id, kind, filename, content, sha256, metadata)
                        VALUES (:scene_id, :kind, :filename, :content, :sha256, CAST(:metadata AS jsonb))
                        """
                    ),
                    {
                        "scene_id": scene_id,
                        "kind": kind,
                        "filename": str(input_obj.get("filename")) if input_obj.get("filename") else None,
                        "content": source_bytes,
                        "sha256": source_sha256,
                        "metadata": _json_param(source_meta),
                    },
                )
                for variant in ("raw", "smooth"):
                    smoothing = (
                        {"algorithm": "smooth_load", "version": 1, "threshold": 0.6}
                        if variant == "smooth"
                        else None
                    )
                    conn.execute(
                        text(
                            """
                            INSERT INTO scene_variants (
                                scene_id, variant, polygons, smoothing_metadata, created_at, updated_at
                            ) VALUES (
                                :scene_id, :variant, CAST(:polygons AS jsonb), CAST(:smoothing AS jsonb),
                                :created_at, :created_at
                            )
                            """
                        ),
                        {
                            "scene_id": scene_id,
                            "variant": variant,
                            "polygons": _json_param(variants[variant]),
                            "smoothing": None if smoothing is None else _json_param(smoothing),
                            "created_at": now,
                        },
                    )
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise ValueError(f"Сцена {scene_id} уже существует") from exc
            raise

    def get_scene(self, scene_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute(
                text("SELECT id, state, metadata, created_at, updated_at FROM scenes WHERE id=:scene_id"),
                {"scene_id": scene_id},
            ).mappings().first()
        if row is None:
            return None
        return {
            "scene_id": str(row["id"]),
            "state": str(row["state"]),
            "metadata": dict(_json_value(row["metadata"], {}) or {}),
            "created_at": _epoch(row["created_at"]),
            "updated_at": _epoch(row["updated_at"]),
            "components": self.scene_components(scene_id),
        }

    def set_scene_state(self, scene_id: str, state: str, **metadata: Any) -> dict[str, Any]:
        if self.get_scene(scene_id) is None:
            raise KeyError(scene_id)
        with self.database.begin() as conn:
            if metadata:
                conn.execute(
                    text(
                        """
                        UPDATE scenes
                        SET state=:state,
                            metadata=metadata || CAST(:metadata AS jsonb),
                            updated_at=now()
                        WHERE id=:scene_id
                        """
                    ),
                    {"scene_id": scene_id, "state": str(state), "metadata": _json_param(metadata)},
                )
            else:
                conn.execute(
                    text("UPDATE scenes SET state=:state, updated_at=now() WHERE id=:scene_id"),
                    {"scene_id": scene_id, "state": str(state)},
                )
        return dict(self.get_scene(scene_id) or {})

    def load_scene_variant_polygons(self, scene_id: str, *, variant: str = "raw") -> list[dict[str, Any]]:
        selected = self._variant(variant)
        with self.database.connect() as conn:
            row = conn.execute(
                text("SELECT polygons FROM scene_variants WHERE scene_id=:scene_id AND variant=:variant"),
                {"scene_id": scene_id, "variant": selected},
            ).first()
        if row is None:
            raise KeyError(f"scene variant {scene_id}/{selected} not found")
        return [dict(item) for item in (_json_value(row[0], []) or [])]

    def save_scene_components(self, scene_id: str, components: Sequence[Mapping[str, Any]]) -> None:
        if self.get_scene(scene_id) is None:
            raise KeyError(scene_id)
        with self.database.begin() as conn:
            conn.execute(text("DELETE FROM scene_components WHERE scene_id=:scene_id"), {"scene_id": scene_id})
            for raw in components:
                component = dict(raw)
                conn.execute(
                    text(
                        """
                        INSERT INTO scene_components (scene_id, component_id, polygon_indices, bounds)
                        VALUES (:scene_id, :component_id, :polygon_indices, :bounds)
                        """
                    ),
                    {
                        "scene_id": scene_id,
                        "component_id": int(component.get("id", component.get("component_id"))),
                        "polygon_indices": [int(x) for x in component.get("polygon_indices", [])],
                        "bounds": component.get("bounds"),
                    },
                )

    def scene_components(self, scene_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT component_id, polygon_indices, bounds
                    FROM scene_components WHERE scene_id=:scene_id ORDER BY component_id
                    """
                ),
                {"scene_id": scene_id},
            ).mappings().all()
        return [
            {
                "id": int(row["component_id"]),
                "polygon_indices": [int(x) for x in row["polygon_indices"] or []],
                "bounds": None if row["bounds"] is None else [float(x) for x in row["bounds"]],
            }
            for row in rows
        ]

    def ensure_scene_variants(self, scene_id: str) -> bool:
        """Materialize deferred source, both variants and stable components once."""

        with self.database.begin() as conn:
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
                {"lock_name": f"scene:{scene_id}"},
            )
            scene = conn.execute(
                text("SELECT state, metadata FROM scenes WHERE id=:scene_id FOR UPDATE"),
                {"scene_id": scene_id},
            ).mappings().first()
            if scene is None:
                raise KeyError(scene_id)

            variants_now = conn.execute(
                text("SELECT variant, polygons FROM scene_variants WHERE scene_id=:scene_id ORDER BY variant"),
                {"scene_id": scene_id},
            ).mappings().all()
            existing = {str(r["variant"]): list(_json_value(r["polygons"], []) or []) for r in variants_now}
            scene_metadata = dict(_json_value(scene.get("metadata"), {}) or {})
            existing_ready = bool(existing.get("raw") and existing.get("smooth"))
            if str(scene["state"]) == "ready" and existing_ready and not scene_metadata.get("needs_component_backfill"):
                return False

            source = conn.execute(
                text("SELECT kind, filename, content, sha256, metadata FROM scene_sources WHERE scene_id=:scene_id"),
                {"scene_id": scene_id},
            ).mappings().first()
            if source is None:
                raise KeyError(f"source input for scene={scene_id} not found")
            kind = str(source["kind"])
            metadata = dict(_json_value(source["metadata"], {}) or {})
            content = bytes(source["content"] or b"")

            if existing_ready:
                variants = existing
            elif kind in {"dxf", "xlsx_tables", "json", "pickle"}:
                if sha256(content) != str(source["sha256"]):
                    raise IOError(f"source input for scene={scene_id} повреждён")
                if kind == "dxf":
                    input_obj = {"kind": "dxf", "filename": source["filename"] or "input.dxf", "content": content}
                elif kind == "xlsx_tables":
                    from .source_polygons import source_polygons_from_xlsx_bundle

                    input_obj = source_polygons_from_xlsx_bundle(content, load_column=int(metadata["load_column"]))
                elif kind == "json":
                    from .source_polygons import source_polygons_from_json_bytes

                    input_obj = source_polygons_from_json_bytes(content)
                else:
                    from .safe_pickle import load_source_polygons_pickle

                    input_obj = load_source_polygons_pickle(content, max_polygons=int(self.settings.max_source_polygons))
                variants = build_polygon_variants(input_obj)
            elif kind == "polygons":
                variants = {
                    str(row["variant"]): [dict(x) for x in (_json_value(row["polygons"], []) or [])]
                    for row in variants_now
                }
                if not variants.get("raw"):
                    raise ValueError(f"scene={scene_id} has no raw polygons")
                if not variants.get("smooth"):
                    from A101.read_dxf import smooth_load
                    from .polygon_storage import canonicalize_polygons, geometry_polygons

                    variants["smooth"] = canonicalize_polygons(smooth_load(geometry_polygons(variants["raw"])))
            else:
                raise ValueError(f"unsupported scene source kind={kind}")

            from .scene_geometry import build_stable_scene_components

            components = build_stable_scene_components(variants["raw"])
            for variant in ("raw", "smooth"):
                smoothing = {"algorithm": "smooth_load", "version": 1, "threshold": 0.6} if variant == "smooth" else None
                conn.execute(
                    text(
                        """
                        INSERT INTO scene_variants (scene_id, variant, polygons, smoothing_metadata)
                        VALUES (:scene_id, :variant, CAST(:polygons AS jsonb), CAST(:smoothing AS jsonb))
                        ON CONFLICT (scene_id, variant) DO UPDATE SET
                            polygons=EXCLUDED.polygons,
                            smoothing_metadata=EXCLUDED.smoothing_metadata,
                            updated_at=now()
                        """
                    ),
                    {
                        "scene_id": scene_id,
                        "variant": variant,
                        "polygons": _json_param(variants[variant]),
                        "smoothing": None if smoothing is None else _json_param(smoothing),
                    },
                )
            conn.execute(text("DELETE FROM scene_components WHERE scene_id=:scene_id"), {"scene_id": scene_id})
            for component in components:
                conn.execute(
                    text(
                        """
                        INSERT INTO scene_components (scene_id, component_id, polygon_indices, bounds)
                        VALUES (:scene_id, :component_id, :polygon_indices, :bounds)
                        """
                    ),
                    {
                        "scene_id": scene_id,
                        "component_id": int(component["id"]),
                        "polygon_indices": [int(x) for x in component.get("polygon_indices", [])],
                        "bounds": component.get("bounds"),
                    },
                )
            conn.execute(
                text("UPDATE scenes SET state='ready', metadata=metadata - 'needs_component_backfill', updated_at=now() WHERE id=:scene_id"),
                {"scene_id": scene_id},
            )
        return True

    def sync_task_variants_from_scene(self, task_id: str, scene_id: str) -> None:
        """Copy canonical scene variants into the task compatibility rows.

        Derived analysis state remains task-owned; this only keeps historical
        task storage APIs working without reparsing the uploaded source.
        """
        if self.get_scene(scene_id) is None:
            raise KeyError(scene_id)
        with self.database.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT variant, polygons, smoothing_metadata FROM scene_variants "
                    "WHERE scene_id=:scene_id ORDER BY variant"
                ),
                {"scene_id": scene_id},
            ).mappings().all()
            if not rows:
                raise KeyError(f"scene variants for {scene_id} not found")
            for row in rows:
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
                        "variant": str(row["variant"]),
                        "polygons": _json_param(_json_value(row["polygons"], []) or []),
                        "smoothing": None if row["smoothing_metadata"] is None else _json_param(_json_value(row["smoothing_metadata"], {})),
                    },
                )

    def scene_overlay_events(self, scene_id: str) -> list[dict[str, Any]]:
        if self.get_scene(scene_id) is None:
            raise KeyError(scene_id)
        with self.database.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT seq, overlay_id, event_type, idxs, real, created_at
                    FROM scene_overlay_events WHERE scene_id=:scene_id ORDER BY seq
                    """
                ),
                {"scene_id": scene_id},
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

    def append_scene_overlay_events(
        self,
        scene_id: str,
        events: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        if self.get_scene(scene_id) is None:
            raise KeyError(scene_id)
        polygon_count = len(self.load_scene_variant_polygons(scene_id, variant="raw"))
        rows: list[dict[str, Any]] = []
        seen: set[int] = set()
        for raw in events:
            event_type = str(raw.get("type", "")).lower()
            if event_type not in {"clean", "unclean"}:
                raise ValueError("overlay type must be 'clean' or 'unclean'")
            overlay_id = int(raw.get("id"))
            if overlay_id <= 0:
                raise ValueError("overlay id must be a positive integer; 0 is reserved for base state")
            if overlay_id in seen:
                raise ValueError(f"duplicate overlay id in request: {overlay_id}")
            seen.add(overlay_id)
            idxs = list(dict.fromkeys(int(x) for x in (raw.get("idxs", []) or [])))
            invalid = [idx for idx in idxs if idx < 0 or idx >= polygon_count]
            if invalid:
                raise ValueError(f"source polygon indices out of range: {invalid}")
            rows.append({"id": overlay_id, "type": event_type, "idxs": idxs, "real": bool(raw.get("real", False))})
        if not rows:
            return self.scene_overlay_events(scene_id)
        try:
            with self.database.begin() as conn:
                conn.execute(text("SELECT id FROM scenes WHERE id=:scene_id FOR UPDATE"), {"scene_id": scene_id})
                for row in rows:
                    conn.execute(
                        text(
                            """
                            INSERT INTO scene_overlay_events (scene_id, overlay_id, event_type, idxs, real)
                            VALUES (:scene_id, :overlay_id, :event_type, :idxs, :real)
                            """
                        ),
                        {
                            "scene_id": scene_id,
                            "overlay_id": row["id"],
                            "event_type": row["type"],
                            "idxs": row["idxs"],
                            "real": row["real"],
                        },
                    )
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise ValueError("overlay id already exists for this scene") from exc
            raise
        return self.scene_overlay_events(scene_id)

    def append_v2_scene_overlays(
        self, scene_id: str, events: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """Append v2 overlay mutations with server-assigned monotonic ids.

        Each mutation is an immutable event/snapshot step. The returned overlay_id
        is the id after the last mutation in this request.
        """
        if self.get_scene(scene_id) is None:
            raise KeyError(scene_id)
        polygon_count = len(self.load_scene_variant_polygons(scene_id, variant="raw"))
        normalized: list[dict[str, Any]] = []
        for raw in events:
            event_type = str(raw.get("type", "")).lower()
            if event_type not in {"clean", "unclean"}:
                raise ValueError("overlay type must be 'clean' or 'unclean'")
            idxs = list(dict.fromkeys(int(x) for x in (raw.get("idxs", []) or [])))
            invalid = [idx for idx in idxs if idx < 0 or idx >= polygon_count]
            if invalid:
                raise ValueError(f"source polygon indices out of range: {invalid}")
            normalized.append({
                "type": event_type,
                "idxs": idxs,
                "real": bool(raw.get("real", False)),
            })
        if not normalized:
            current = self.resolve_scene_overlay_id(scene_id, -1)
            return {"overlay_id": int(current), "rows": self.scene_overlay_events(scene_id)}

        with self.database.begin() as conn:
            scene = conn.execute(
                text("SELECT id FROM scenes WHERE id=:scene_id FOR UPDATE"),
                {"scene_id": scene_id},
            ).first()
            if scene is None:
                raise KeyError(scene_id)
            current = conn.execute(
                text("SELECT COALESCE(MAX(overlay_id), 0) FROM scene_overlay_events WHERE scene_id=:scene_id"),
                {"scene_id": scene_id},
            ).scalar_one()
            next_id = int(current or 0)
            for row in normalized:
                next_id += 1
                conn.execute(
                    text(
                        """
                        INSERT INTO scene_overlay_events (scene_id, overlay_id, event_type, idxs, real)
                        VALUES (:scene_id, :overlay_id, :event_type, :idxs, :real)
                        """
                    ),
                    {
                        "scene_id": scene_id,
                        "overlay_id": next_id,
                        "event_type": row["type"],
                        "idxs": row["idxs"],
                        "real": row["real"],
                    },
                )
        return {"overlay_id": next_id, "rows": self.scene_overlay_events(scene_id)}

    def resolve_scene_overlay_id(self, scene_id: str, selector: int | str | None = 0) -> int:
        if self.get_scene(scene_id) is None:
            raise KeyError(scene_id)
        return resolve_overlay_selector(self.scene_overlay_events(scene_id), selector)

    def resolved_scene_polygons(
        self,
        scene_id: str,
        *,
        variant: str = "raw",
        overlay_id: int | str | None = 0,
    ) -> list[dict[str, Any]]:
        selected = self.resolve_scene_overlay_id(scene_id, overlay_id)
        polygons = self.load_scene_variant_polygons(scene_id, variant=self._variant(variant))
        events = self.scene_overlay_events(scene_id) if selected else []
        return resolve_overlay(polygons, events, selected)

    def link_task_scene_context(
        self,
        task_id: str,
        *,
        scene_id: str,
        variant: str,
        overlay_id: int,
        component_selection: Sequence[int],
    ) -> None:
        selected_variant = self._variant(variant)
        selected_overlay = normalize_overlay_id(overlay_id)
        with self.database.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE tasks SET
                        scene_id=:scene_id,
                        analysis_variant=:variant,
                        analysis_overlay_id=:overlay_id,
                        component_selection=:component_selection,
                        initial_variant=:variant,
                        updated_at=now()
                    WHERE id=:task_id
                    """
                ),
                {
                    "task_id": task_id,
                    "scene_id": scene_id,
                    "variant": selected_variant,
                    "overlay_id": selected_overlay,
                    "component_selection": [int(x) for x in component_selection],
                },
            )
            if getattr(result, "rowcount", 1) == 0:
                raise KeyError(task_id)

    def task_analysis_context(self, task_id: str) -> dict[str, Any]:
        meta = self.get_meta(task_id)
        if meta is None:
            raise KeyError(task_id)
        return {
            "scene_id": str(meta.get("scene_id") or task_id),
            "variant": str(meta.get("analysis_variant") or meta.get("initial_variant") or "raw"),
            "smooth": str(meta.get("analysis_variant") or meta.get("initial_variant") or "raw") == "smooth",
            "overlay_id": int(meta.get("analysis_overlay_id", 0) or 0),
            "component_selection": [int(x) for x in (meta.get("component_selection") or ([-2] if meta.get("whole") else [-3]))],
        }

    # ---------- overlay event log / analysis identity ----------
    def overlay_events(self, task_id: str) -> list[dict[str, Any]]:
        """Compatibility alias: overlays are scene-owned from schema revision 0003."""
        meta = self.get_meta(task_id)
        if meta is None:
            raise KeyError(task_id)
        return self.scene_overlay_events(str(meta.get("scene_id") or task_id))

    def append_overlay_events(self, task_id: str, events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Compatibility alias that appends events to the task's reusable scene."""
        meta = self.get_meta(task_id)
        if meta is None:
            raise KeyError(task_id)
        return self.append_scene_overlay_events(str(meta.get("scene_id") or task_id), events)

    def resolved_source_polygons(
        self, task_id: str, *, variant: str = "raw", overlay_id: int | None = 0
    ) -> list[dict[str, Any]]:
        meta = self.get_meta(task_id)
        if meta is None:
            raise KeyError(task_id)
        scene_id = str(meta.get("scene_id") or task_id)
        selected_variant = self._variant(variant)
        selected_overlay = normalize_overlay_id(overlay_id)
        if scene_id == str(task_id) and selected_variant == "raw" and selected_overlay == 0:
            scene = self.get_scene(scene_id) or {}
            origin = dict(scene.get("metadata", {}).get("legacy_canonical_source", {}) or {})
            selected_variant = self._variant(str(origin.get("variant", selected_variant)))
            selected_overlay = normalize_overlay_id(origin.get("overlay_id", selected_overlay))
        return self.resolved_scene_polygons(scene_id, variant=selected_variant, overlay_id=selected_overlay)

    def ensure_analysis(self, task_id: str, *, variant: str = "raw", overlay_id: int | None = 0) -> dict[str, Any]:
        selected = self._variant(variant)
        selected_overlay = normalize_overlay_id(overlay_id)
        meta = self.get_meta(task_id)
        if meta is None:
            raise KeyError(task_id)
        scene_id = str(meta.get("scene_id") or task_id)
        if selected_overlay:
            # A positive analysis overlay must be an actual event id on the scene.
            resolved = self.resolve_scene_overlay_id(scene_id, selected_overlay)
            if resolved != selected_overlay:
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
            # Task creation historically seeds N requests at overlay 0.  Snapshot
            # those requests into the immutable analysis context when necessary.
            requested = conn.execute(
                text(
                    """
                    SELECT n, position FROM task_n_requests
                    WHERE task_id=:task_id AND variant=:variant AND overlay_id=0
                    ORDER BY position, n
                    """
                ),
                {"task_id": task_id, "variant": selected},
            ).mappings().all()
            for row in requested:
                conn.execute(
                    text(
                        """
                        INSERT INTO task_n_requests (task_id, variant, overlay_id, n, position, status)
                        VALUES (:task_id, :variant, :overlay_id, :n, :position, 'requested')
                        ON CONFLICT (task_id, variant, overlay_id, n) DO NOTHING
                        """
                    ),
                    {
                        "task_id": task_id,
                        "variant": selected,
                        "overlay_id": selected_overlay,
                        "n": int(row["n"]),
                        "position": int(row["position"]),
                    },
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
        row["variant"] = selected
        row["smooth"] = selected == "smooth"
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
        n_bounds = dict(row.get("bounds") or {}) if isinstance(row.get("bounds"), Mapping) else {}
        for key in ("max_n_state", "max_n_milp"):
            if key in row:
                n_bounds[key] = row[key]
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
            for key in ("max_n_state", "max_n_milp"):
                if key in result["bounds"]:
                    result[key] = result["bounds"][key]
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

    def delete_solver_result(
        self, task_id: str, component_id: Any, n: int, *, variant: str = "raw", overlay_id: int | None = 0
    ) -> None:
        cid = self.component_db_id(component_id)
        self._delete_artifact(
            task_id, self._variant(variant), f"solver:{cid}:{int(n)}", overlay_id=normalize_overlay_id(overlay_id)
        )

    def completed_frontier_ns(self, task_id: str, component_id: Any, *, variant: str = "raw", overlay_id: int = 0) -> set[int]:
        with self.database.connect() as conn:
            rows = conn.execute(text("SELECT n FROM component_results WHERE task_id=:task_id AND variant=:variant AND overlay_id=:overlay_id AND component_id=:component_id"),
                {"task_id": task_id, "variant": self._variant(variant), "overlay_id": normalize_overlay_id(overlay_id),
                 "component_id": self.component_db_id(component_id)}).scalars().all()
        return {int(n) for n in rows}

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
        return {
            int(row["n"]): dict(restore_json_geometries(_json_value(row["result"], {}) or {}))
            for row in rows
        }

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
                    WHERE
                        (EXCLUDED.is_feasible AND NOT solutions.is_feasible)
                        OR (
                            EXCLUDED.is_feasible AND solutions.is_feasible AND (
                                solutions.actual_mass_kg IS NULL
                                OR (EXCLUDED.actual_mass_kg IS NOT NULL AND EXCLUDED.actual_mass_kg < solutions.actual_mass_kg)
                                OR (
                                    EXCLUDED.actual_mass_kg = solutions.actual_mass_kg
                                    AND EXCLUDED.is_optimal AND NOT solutions.is_optimal
                                )
                            )
                        )
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
            ORDER BY is_feasible DESC, actual_mass_kg ASC NULLS LAST, is_optimal DESC,
                     proxy_mass ASC NULLS LAST, created_at ASC
        """
        with self.database.connect() as conn:
            values = conn.execute(text(sql), params).scalars().all()
        return [dict(_json_value(value, {}) or {}) for value in values]

    def solution_summaries(
        self, task_id: str, total_n: int | None = None, source: str | None = None,
        variant: str | None = None, overlay_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return lightweight solution rows without materializing the heavy result JSONB."""
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
            SELECT solution_id, variant, overlay_id, source, total_n, component_ns, proxy_mass,
                   actual_mass_kg, is_feasible, is_optimal, status, created_at
            FROM solutions WHERE {' AND '.join(clauses)}
            ORDER BY is_feasible DESC, actual_mass_kg ASC NULLS LAST, is_optimal DESC,
                     proxy_mass ASC NULLS LAST, created_at ASC
        """
        with self.database.connect() as conn:
            rows = conn.execute(text(sql), params).mappings().all()
        return [
            {
                "solution_id": str(row["solution_id"]),
                "variant": str(row["variant"]),
                "smooth": str(row["variant"]) == "smooth",
                "overlay_id": int(row["overlay_id"]),
                "source": str(row["source"]),
                "total_N": int(row["total_n"]),
                "component_ns": dict(_json_value(row["component_ns"], {}) or {}),
                "proxy_mass": None if row["proxy_mass"] is None else float(row["proxy_mass"]),
                "actual_mass_kg": None if row["actual_mass_kg"] is None else float(row["actual_mass_kg"]),
                "is_feasible": bool(row["is_feasible"]),
                "is_optimal": bool(row["is_optimal"]),
                "status": str(row["status"]),
                "created_at": _epoch(row["created_at"]),
            }
            for row in rows
        ]

    def best_solution(
        self, task_id: str, total_n: int, *, variant: str | None = None, overlay_id: int | None = 0
    ) -> dict[str, Any] | None:
        clauses = ["task_id=:task_id", "total_n=:total_n"]
        params: dict[str, Any] = {"task_id": task_id, "total_n": int(total_n)}
        if variant is not None:
            clauses.append("variant=:variant")
            params["variant"] = self._variant(variant)
        if overlay_id is not None:
            clauses.append("overlay_id=:overlay_id")
            params["overlay_id"] = normalize_overlay_id(overlay_id)
        sql = f"""
            SELECT result FROM solutions WHERE {' AND '.join(clauses)}
            ORDER BY is_feasible DESC, actual_mass_kg ASC NULLS LAST, is_optimal DESC,
                     proxy_mass ASC NULLS LAST, created_at ASC
            LIMIT 1
        """
        with self.database.connect() as conn:
            value = conn.execute(text(sql), params).scalar_one_or_none()
        return None if value is None else dict(_json_value(value, {}) or {})

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
        rows = self.solution_summaries(task_id, variant=selected_variant, overlay_id=normalize_overlay_id(overlay_id))
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

    def mark_analysis_infeasible(
        self, task_id: str, *, variant: str = "raw", overlay_id: int | None = 0, detail: Mapping[str, Any] | None = None
    ) -> None:
        with self.database.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE task_analyses
                    SET preparation_state='infeasible', updated_at=now()
                    WHERE task_id=:task_id AND variant=:variant AND overlay_id=:overlay_id
                    """
                ),
                {
                    "task_id": task_id,
                    "variant": self._variant(variant),
                    "overlay_id": normalize_overlay_id(overlay_id),
                },
            )

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


    def has_job_failures(self, task_id: str) -> bool:
        with self.database.connect() as conn:
            value = conn.execute(
                text(
                    """
                    SELECT
                        EXISTS (
                            SELECT 1 FROM task_events
                            WHERE task_id=:task_id AND event_type='job_failed'
                        )
                        OR EXISTS (
                            SELECT 1 FROM task_analyses
                            WHERE task_id=:task_id AND preparation_state='failed'
                        )
                    """
                ),
                {"task_id": task_id},
            ).scalar_one()
        return bool(value)

    def has_infeasible_analysis(self, task_id: str) -> bool:
        with self.database.connect() as conn:
            value = conn.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM task_analyses
                        WHERE task_id=:task_id AND preparation_state='infeasible'
                    )
                    """
                ),
                {"task_id": task_id},
            ).scalar_one()
        return bool(value)

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
            failure_check = getattr(self, "has_job_failures", None)
            has_failures = bool(failure_check(task_id)) if callable(failure_check) else False
            state = "completed_with_errors" if has_failures else "completed"
        if state != meta.get("state"):
            meta = self.patch_meta(task_id, state=state)
            self.publish_event(task_id, "task_state", {"state": state})
        return meta

    # ---------- API v2 durable whole-field pipeline ----------
    def create_v2_task(
        self,
        task_id: str,
        *,
        scene_id: str,
        overlay_id: int,
        smooth: bool,
        config: Mapping[str, Any],
        ns: Sequence[int],
    ) -> dict[str, Any]:
        values = list(dict.fromkeys(int(n) for n in ns if int(n) > 0))
        if not values:
            raise ValueError("v2 task requires at least one positive N")
        with self.database.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO v2_tasks (id, scene_id, overlay_id, smooth, config, preparation_state)
                    VALUES (:id, :scene_id, :overlay_id, :smooth, CAST(:config AS jsonb), 'pending')
                    """
                ),
                {
                    "id": str(task_id),
                    "scene_id": str(scene_id),
                    "overlay_id": int(overlay_id),
                    "smooth": bool(smooth),
                    "config": _json_param(config),
                },
            )
            for position, n in enumerate(values):
                conn.execute(
                    text(
                        """
                        INSERT INTO v2_task_n (task_id, n, position, attempt, state)
                        VALUES (:task_id, :n, :position, 1, 'preparing')
                        """
                    ),
                    {"task_id": str(task_id), "n": int(n), "position": int(position)},
                )
        return {
            "task_id": str(task_id), "scene_id": str(scene_id), "overlay_id": int(overlay_id),
            "smooth": bool(smooth), "preparation_state": "pending", "requested_n": values,
        }

    def get_v2_task(self, task_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            task = conn.execute(
                text(
                    """
                    SELECT id, scene_id, overlay_id, smooth, config, preparation_state,
                           max_useful_n, preparation_error, created_at, updated_at, prepared_at
                    FROM v2_tasks WHERE id=:task_id
                    """
                ),
                {"task_id": str(task_id)},
            ).mappings().first()
            if task is None:
                return None
            rows = conn.execute(
                text(
                    """
                    SELECT n, position, attempt, state, status, fun, mass_kg, mass_bg_kg,
                           result, error, created_at, updated_at
                    FROM v2_task_n WHERE task_id=:task_id ORDER BY position, n
                    """
                ),
                {"task_id": str(task_id)},
            ).mappings().all()
        result = {
            "task_id": str(task["id"]),
            "scene_id": str(task["scene_id"]),
            "overlay_id": int(task["overlay_id"]),
            "smooth": bool(task["smooth"]),
            "config": dict(_json_value(task["config"], {}) or {}),
            "preparation_state": str(task["preparation_state"]),
            "max_useful_n": None if task["max_useful_n"] is None else int(task["max_useful_n"]),
            "preparation_error": _json_value(task["preparation_error"]),
            "created_at": _epoch(task["created_at"]),
            "updated_at": _epoch(task["updated_at"]),
            "prepared_at": None if task["prepared_at"] is None else _epoch(task["prepared_at"]),
            "solutions": [],
        }
        for row in rows:
            item = {
                "n": int(row["n"]), "position": int(row["position"]), "attempt": int(row["attempt"]),
                "state": str(row["state"]), "updated_at": _epoch(row["updated_at"]),
            }
            for key in ("status", "fun", "mass_kg", "mass_bg_kg"):
                if row[key] is not None:
                    item[key] = row[key]
            if row["result"] is not None:
                item["result"] = _json_value(row["result"], {})
            if row["error"] is not None:
                item["error"] = _json_value(row["error"], {})
            result["solutions"].append(item)
        return result

    def set_v2_preparation_state(
        self,
        task_id: str,
        state: str,
        *,
        max_useful_n: int | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> None:
        from .v2_state import normalize_v2_state

        if state not in {"pending", "preparing", "success", "error", "cancelled"}:
            raise ValueError(f"invalid v2 preparation state: {state}")
        # Validate shared labels where they overlap public N state labels.
        if state != "success" or state in {"pending", "preparing", "error", "cancelled"}:
            normalize_v2_state(state)
        with self.database.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE v2_tasks SET
                        preparation_state=:state,
                        max_useful_n=:max_useful_n,
                        preparation_error=CAST(:error AS jsonb),
                        prepared_at=CASE WHEN :is_success THEN now() ELSE prepared_at END,
                        updated_at=now()
                    WHERE id=:task_id
                    """
                ),
                {
                    "task_id": str(task_id),
                    "state": str(state),
                    "is_success": state == "success",
                    "max_useful_n": None if max_useful_n is None else int(max_useful_n),
                    "error": _json_param(error) if error is not None else None,
                },
            )
            if state in {"preparing", "error", "cancelled"}:
                n_state = "preparing" if state == "preparing" else state
                conn.execute(
                    text(
                        """UPDATE v2_task_n SET state=:n_state, updated_at=now(),
                                error=CASE WHEN :is_error THEN CAST(:error AS jsonb) ELSE error END
                            WHERE task_id=:task_id AND state IN ('pending','preparing')
                        """
                    ),
                    {
                        "task_id": str(task_id),
                        "n_state": n_state,
                        "is_error": n_state == "error",
                        "error": _json_param(error) if error is not None else None,
                    },
                )
            elif state == "success":
                conn.execute(
                    text(
                        """
                        UPDATE v2_task_n SET state='pending', updated_at=now()
                        WHERE task_id=:task_id AND state='preparing'
                        """
                    ),
                    {"task_id": str(task_id)},
                )

    def add_v2_ns(self, task_id: str, ns: Sequence[int]) -> dict[str, list[int]]:
        from .v2_state import next_n_action

        requested = list(dict.fromkeys(int(n) for n in ns if int(n) > 0))
        if not requested:
            raise ValueError("n must contain positive values")
        created: list[int] = []
        retried: list[int] = []
        kept: list[int] = []
        with self.database.begin() as conn:
            task = conn.execute(
                text("SELECT preparation_state FROM v2_tasks WHERE id=:task_id FOR UPDATE"),
                {"task_id": str(task_id)},
            ).mappings().first()
            if task is None:
                raise KeyError(task_id)
            rows = conn.execute(
                text("SELECT n, state, attempt FROM v2_task_n WHERE task_id=:task_id FOR UPDATE"),
                {"task_id": str(task_id)},
            ).mappings().all()
            current = {int(row["n"]): row for row in rows}
            next_position = int(conn.execute(
                text("SELECT COALESCE(MAX(position), -1) + 1 FROM v2_task_n WHERE task_id=:task_id"),
                {"task_id": str(task_id)},
            ).scalar_one())
            target_state = "pending" if str(task["preparation_state"]) == "success" else "preparing"
            for n in requested:
                row = current.get(n)
                action = next_n_action(None if row is None else str(row["state"]))
                if action == "keep":
                    kept.append(n)
                    continue
                if action == "create":
                    conn.execute(
                        text(
                            """
                            INSERT INTO v2_task_n (task_id, n, position, attempt, state)
                            VALUES (:task_id, :n, :position, 1, :state)
                            """
                        ),
                        {"task_id": str(task_id), "n": n, "position": next_position, "state": target_state},
                    )
                    next_position += 1
                    created.append(n)
                    continue
                attempt = int(row["attempt"]) + 1
                conn.execute(
                    text(
                        """
                        UPDATE v2_task_n SET
                            attempt=:attempt, state=:state, status=NULL, fun=NULL, mass_kg=NULL,
                            mass_bg_kg=NULL, result=NULL, error=NULL, updated_at=now()
                        WHERE task_id=:task_id AND n=:n
                        """
                    ),
                    {"task_id": str(task_id), "n": n, "attempt": attempt, "state": target_state},
                )
                retried.append(n)
        return {"created": created, "retried": retried, "kept": kept}

    def set_v2_n_state(
        self,
        task_id: str,
        n: int,
        state: str,
        *,
        attempt: int | None = None,
        status: str | None = None,
        fun: float | None = None,
        mass_kg: float | None = None,
        mass_bg_kg: float | None = None,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> bool:
        from .v2_state import normalize_v2_state

        normalize_v2_state(state)
        if status is not None and status not in {"optimal", "feasible", "infeasible"}:
            raise ValueError(f"invalid v2 status: {status}")
        clauses = ["task_id=:task_id", "n=:n"]
        params: dict[str, Any] = {
            "task_id": str(task_id), "n": int(n), "state": str(state), "status": status,
            "fun": fun, "mass_kg": mass_kg, "mass_bg_kg": mass_bg_kg,
            "result": _json_param(result) if result is not None else None,
            "error": _json_param(error) if error is not None else None,
        }
        if attempt is not None:
            clauses.append("attempt=:attempt")
            params["attempt"] = int(attempt)
        with self.database.begin() as conn:
            response = conn.execute(
                text(
                    f"""
                    UPDATE v2_task_n SET
                        state=:state,
                        status=COALESCE(:status, status),
                        fun=COALESCE(:fun, fun),
                        mass_kg=COALESCE(:mass_kg, mass_kg),
                        mass_bg_kg=COALESCE(:mass_bg_kg, mass_bg_kg),
                        result=CASE WHEN :result IS NULL THEN result ELSE CAST(:result AS jsonb) END,
                        error=CASE WHEN :error IS NULL THEN error ELSE CAST(:error AS jsonb) END,
                        updated_at=now()
                    WHERE {' AND '.join(clauses)}
                    """
                ),
                params,
            )
        return bool(getattr(response, "rowcount", 1))

    def get_v2_n(self, task_id: str, n: int) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT n, position, attempt, state, status, fun, mass_kg, mass_bg_kg,
                           result, error, created_at, updated_at
                    FROM v2_task_n WHERE task_id=:task_id AND n=:n
                    """
                ),
                {"task_id": str(task_id), "n": int(n)},
            ).mappings().first()
        if row is None:
            return None
        out = {
            "n": int(row["n"]), "position": int(row["position"]), "attempt": int(row["attempt"]),
            "state": str(row["state"]), "created_at": _epoch(row["created_at"]), "updated_at": _epoch(row["updated_at"]),
        }
        for key in ("status", "fun", "mass_kg", "mass_bg_kg"):
            if row[key] is not None:
                out[key] = row[key]
        if row["result"] is not None:
            out["result"] = _json_value(row["result"], {})
        if row["error"] is not None:
            out["error"] = _json_value(row["error"], {})
        return out

    def cancel_v2_ns(self, task_id: str, ns: Sequence[int]) -> list[int]:
        targets = list(dict.fromkeys(int(n) for n in ns if int(n) > 0))
        if not targets:
            return []
        with self.database.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE v2_task_n SET state='cancelled', updated_at=now()
                    WHERE task_id=:task_id AND n = ANY(:ns)
                      AND state IN ('pending','preparing','solving','fitting','baring','error')
                    """
                ),
                {"task_id": str(task_id), "ns": targets},
            )
        return targets

    def save_v2_artifact(
        self,
        task_id: str,
        artifact_key: str,
        artifact_type: str,
        value: Any,
        *,
        n: int | None = None,
        attempt: int | None = None,
    ) -> None:
        payload, codec = encode_object(value)
        digest = sha256(payload)
        with self.database.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO v2_runtime_artifacts
                        (task_id, artifact_key, artifact_type, n, attempt, codec, payload, sha256)
                    VALUES (:task_id, :artifact_key, :artifact_type, :n, :attempt, :codec, :payload, :sha256)
                    ON CONFLICT (task_id, artifact_key) DO UPDATE SET
                        artifact_type=EXCLUDED.artifact_type, n=EXCLUDED.n, attempt=EXCLUDED.attempt,
                        codec=EXCLUDED.codec, payload=EXCLUDED.payload, sha256=EXCLUDED.sha256, updated_at=now()
                    """
                ),
                {
                    "task_id": str(task_id), "artifact_key": str(artifact_key), "artifact_type": str(artifact_type),
                    "n": None if n is None else int(n), "attempt": None if attempt is None else int(attempt),
                    "codec": codec, "payload": payload, "sha256": digest,
                },
            )

    def load_v2_artifact(self, task_id: str, artifact_key: str) -> Any:
        with self.database.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT codec, payload, sha256 FROM v2_runtime_artifacts
                    WHERE task_id=:task_id AND artifact_key=:artifact_key
                    """
                ),
                {"task_id": str(task_id), "artifact_key": str(artifact_key)},
            ).mappings().first()
        if row is None:
            return None
        payload = bytes(row["payload"])
        if sha256(payload) != str(row["sha256"]):
            raise ValueError(f"Corrupt v2 artifact {artifact_key}")
        return decode_object(payload, str(row["codec"]))

    @staticmethod
    def _v2_request_table(kind: str) -> str:
        if kind == "bars":
            return "v2_bars_requests"
        if kind == "verification":
            return "v2_verification_requests"
        raise ValueError(f"unknown v2 request kind: {kind}")

    def create_v2_request(
        self,
        kind: str,
        request_id: str,
        *,
        scene_id: str,
        overlay_id: int,
        smooth: bool,
        config: Mapping[str, Any],
        zones: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        table = self._v2_request_table(kind)
        with self.database.begin() as conn:
            conn.execute(
                text(
                    f"""
                    INSERT INTO {table} (id, scene_id, overlay_id, smooth, config, zones, state)
                    VALUES (:id, :scene_id, :overlay_id, :smooth, CAST(:config AS jsonb), CAST(:zones AS jsonb), 'pending')
                    """
                ),
                {
                    "id": str(request_id), "scene_id": str(scene_id), "overlay_id": int(overlay_id),
                    "smooth": bool(smooth), "config": _json_param(config), "zones": _json_param(list(zones)),
                },
            )
        return {f"{kind if kind == 'bars' else 'verification'}_id": str(request_id), "state": "pending"}

    def get_v2_request(self, kind: str, request_id: str) -> dict[str, Any] | None:
        table = self._v2_request_table(kind)
        with self.database.connect() as conn:
            row = conn.execute(
                text(
                    f"""
                    SELECT id, scene_id, overlay_id, smooth, config, zones, state, result, error,
                           created_at, updated_at
                    FROM {table} WHERE id=:id
                    """
                ),
                {"id": str(request_id)},
            ).mappings().first()
        if row is None:
            return None
        key = "bars_id" if kind == "bars" else "verification_id"
        out = {
            key: str(row["id"]), "scene_id": str(row["scene_id"]), "overlay_id": int(row["overlay_id"]),
            "smooth": bool(row["smooth"]), "config": _json_value(row["config"], {}),
            "zones": _json_value(row["zones"], []), "state": str(row["state"]),
            "created_at": _epoch(row["created_at"]), "updated_at": _epoch(row["updated_at"]),
        }
        if row["result"] is not None:
            out["result"] = _json_value(row["result"], {})
        if row["error"] is not None:
            out["error"] = _json_value(row["error"], {})
        return out

    def set_v2_request_state(
        self,
        kind: str,
        request_id: str,
        state: str,
        *,
        result: Mapping[str, Any] | Sequence[Any] | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> None:
        table = self._v2_request_table(kind)
        allowed = {"pending", "baring", "success", "error"} if kind == "bars" else {"pending", "validation", "success", "error"}
        if state not in allowed:
            raise ValueError(f"invalid {kind} state: {state}")
        with self.database.begin() as conn:
            conn.execute(
                text(
                    f"""
                    UPDATE {table} SET state=:state,
                        result=CASE WHEN :result IS NULL THEN result ELSE CAST(:result AS jsonb) END,
                        error=CASE WHEN :error IS NULL THEN error ELSE CAST(:error AS jsonb) END,
                        updated_at=now()
                    WHERE id=:id
                    """
                ),
                {
                    "id": str(request_id), "state": str(state),
                    "result": _json_param(result) if result is not None else None,
                    "error": _json_param(error) if error is not None else None,
                },
            )
