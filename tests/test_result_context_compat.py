from rebar_service import api


def test_new_task_context_is_authoritative(monkeypatch):
    meta = {
        "scene_id": "scene-new",
        "analysis_variant": "smooth",
        "analysis_overlay_id": 900,
        "initial_variant": "smooth",
    }
    monkeypatch.setattr(api.store, "resolve_scene_overlay_id", lambda *args, **kwargs: 123)
    assert api._task_context("task-new", meta, smooth=False, overlay=0) == ("smooth", True, 900)
    assert api._task_context("task-new", meta, smooth=True, overlay=-1) == ("smooth", True, 900)


def test_migrated_legacy_task_keeps_old_query_semantics_and_relative_overlay(monkeypatch):
    meta = {
        "scene_id": "legacy-task",
        "analysis_variant": "raw",
        "analysis_overlay_id": 0,
        "initial_variant": "smooth",
    }
    seen = []
    def resolve(scene_id, selector):
        seen.append((scene_id, selector))
        return 777 if selector == -1 else selector
    monkeypatch.setattr(api.store, "resolve_scene_overlay_id", resolve)

    assert api._task_context("legacy-task", meta) == ("raw", False, 0)
    assert api._task_context("legacy-task", meta, smooth=True, overlay=-1) == ("smooth", True, 777)
    assert seen[-1] == ("legacy-task", -1)
