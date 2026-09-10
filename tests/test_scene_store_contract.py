from __future__ import annotations

import inspect

from rebar_service.postgres_store import PostgresStore


def test_store_exposes_scene_persistence_contract():
    expected = {
        "create_scene",
        "get_scene",
        "set_scene_state",
        "ensure_scene_variants",
        "load_scene_variant_polygons",
        "save_scene_components",
        "scene_components",
        "scene_overlay_events",
        "append_scene_overlay_events",
        "resolve_scene_overlay_id",
        "resolved_scene_polygons",
        "link_task_scene_context",
    }
    missing = sorted(name for name in expected if not hasattr(PostgresStore, name))
    assert not missing, missing


def test_scene_overlay_resolver_supports_relative_selectors():
    signature = inspect.signature(PostgresStore.resolve_scene_overlay_id)
    assert "scene_id" in signature.parameters
    assert "selector" in signature.parameters


def test_new_scene_persists_variants_without_scene_components():
    from contextlib import contextmanager
    from rebar_service.config import Settings

    class Result:
        pass

    class Connection:
        def __init__(self, calls):
            self.calls = calls

        def execute(self, statement, params=None):
            self.calls.append((str(statement), dict(params or {})))
            return Result()

    class Database:
        def __init__(self):
            self.calls = []

        @contextmanager
        def begin(self):
            yield Connection(self.calls)

    database = Database()
    store = PostgresStore(Settings(), database=database)
    store.create_scene(
        "scene-new",
        {"state": "ready"},
        {
            "kind": "polygons",
            "units": "mm",
            "polygons": [
                {"points": [[0, 0], [1000, 0], [1000, 1000], [0, 1000]], "load": 10.0}
            ],
        },
    )

    sql = "\n".join(statement for statement, _ in database.calls)
    assert "INSERT INTO scene_variants" in sql
    assert "INSERT INTO scene_components" not in sql
