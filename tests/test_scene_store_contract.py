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
