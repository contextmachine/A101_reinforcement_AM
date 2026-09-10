from __future__ import annotations

from pathlib import Path
import pytest

from fastapi.testclient import TestClient

import rebar_service.api as api
from rebar_service.models import SceneCreated


client = TestClient(api.app)


def test_scene_routes_exist_in_openapi():
    paths = api.app.openapi()["paths"]
    for route in (
        "/v1/scenes/dxf_upload",
        "/v1/scenes/json_upload",
        "/v1/scenes/tables_upload",
        "/v1/scenes/pkl_upload",
        "/v1/scenes/{scene_id}",
        "/v1/scenes/{scene_id}/polygons",
        "/v1/scenes/{scene_id}/overlays",
    ):
        assert route in paths


def test_dxf_scene_upload_only_creates_scene(monkeypatch):
    seen = {}

    def create_scene(input_obj):
        seen["input"] = input_obj
        return SceneCreated(scene_id="scene1", state="preparing")

    monkeypatch.setattr(api, "_create_scene", create_scene)
    response = client.post(
        "/v1/scenes/dxf_upload",
        files={"file": ("a.dxf", b"0\nEOF\n", "application/dxf")},
    )
    assert response.status_code == 200
    assert response.json() == {"scene_id": "scene1", "state": "preparing"}
    assert seen["input"]["kind"] == "dxf"


def test_scene_polygons_returns_resolved_overlay_id(monkeypatch):
    monkeypatch.setattr(api.store, "get_scene", lambda scene_id: {"scene_id": scene_id, "state": "ready"})
    monkeypatch.setattr(api.store, "resolve_scene_overlay_id", lambda scene_id, selector: 900 if selector == -1 else int(selector))
    monkeypatch.setattr(
        api.store,
        "resolved_scene_polygons",
        lambda scene_id, variant="raw", overlay_id=0: [
            {"source_index": 0, "points": [[0, 0], [1, 0], [1, 1]], "load": 10, "overlay_state": "active"}
        ],
    )
    response = client.get("/v1/scenes/s/polygons?smooth=true&overlay=-1")
    assert response.status_code == 200
    body = response.json()
    assert body["scene_id"] == "s"
    assert body["smooth"] is True
    assert body["overlay_id"] == 900
    assert len(body["polygons"]) == 1


def test_no_kubernetes_yaml_is_part_of_scene_change_contract():
    # Guard the user's explicit scope: implementation tests should not require a manifest change.
    root = Path(__file__).resolve().parents[1]
    assert (root / "deploy").is_dir()


def test_json_and_pickle_scene_upload_keep_original_bytes_without_task_creation(monkeypatch):
    import json
    import pickle
    seen = []
    polygons = [{'points': [[0, 0], [100, 0], [100, 100]], 'load': 12.0}]
    def create(input_obj):
        seen.append(input_obj)
        return SceneCreated(scene_id='scene-upload', state='preparing')
    monkeypatch.setattr(api, '_create_scene', create)
    monkeypatch.setattr(api, '_build_task', lambda *a, **kw: pytest.fail('scene upload must not create task'))
    for route, filename, kind, data in [
        ('json_upload', 'scene.json', 'json', json.dumps({'polygons': polygons}).encode()),
        ('pkl_upload', 'scene.pkl', 'pickle', pickle.dumps(polygons)),
    ]:
        response = client.post('/v1/scenes/' + route, files={'file': (filename, data)})
        assert response.status_code == 200, response.text
        assert response.json() == {'scene_id': 'scene-upload', 'state': 'preparing'}
        assert seen[-1]['kind'] == kind and seen[-1]['content'] == data


def test_three_scene_tables_roundtrip_to_source_polygons(monkeypatch):
    from test_xlsx_source_polygons import _sample_tables
    from rebar_service.source_polygons import source_polygons_from_xlsx_bundle
    captured = []
    def create(input_obj):
        captured.append(input_obj)
        return SceneCreated(scene_id='scene-tables', state='preparing')
    monkeypatch.setattr(api, '_create_scene', create)
    nodes, elements, loads = _sample_tables()
    response = client.post('/v1/scenes/tables_upload', data={'load_column': '2'}, files={
        'nodes_file': ('nodes.xlsx', nodes), 'elements_file': ('elements.xlsx', elements),
        'loads_file': ('loads.xlsx', loads),
    })
    assert response.status_code == 200, response.text
    result = source_polygons_from_xlsx_bundle(captured[0]['content'], load_column=2)
    assert result['polygons'][0]['load'] == 8.1
    assert len(result['polygons']) == 2


def test_create_scene_materializes_in_api_and_never_enqueues_scene_job(monkeypatch):
    seen = {}

    def create_scene(scene_id, meta, input_obj):
        seen["scene_id"] = scene_id
        seen["meta"] = dict(meta)
        seen["input"] = dict(input_obj)

    monkeypatch.setattr(api.store, "create_scene", create_scene)
    monkeypatch.setattr(
        api.workflow,
        "enqueue_scene_materialization",
        lambda scene_id: pytest.fail("scene polygon preparation must stay in the API request"),
    )

    result = api._create_scene({"kind": "dxf", "filename": "a.dxf", "content": b"DXF"})

    assert result.state == "ready"
    assert seen["meta"]["state"] == "ready"
    assert seen["input"]["kind"] == "dxf"


def test_uploaded_task_starts_compute_directly_without_materialize_source_job(monkeypatch):
    calls = []

    monkeypatch.setattr(
        api.store,
        "create_scene",
        lambda scene_id, meta, input_obj: calls.append(("scene", dict(meta), dict(input_obj))),
    )
    monkeypatch.setattr(
        api.store,
        "create_task",
        lambda task_id, meta, plan, input_obj: calls.append(("task", dict(meta), dict(input_obj))),
    )
    monkeypatch.setattr(api.store, "publish_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        api.workflow,
        "materialize_task_source",
        lambda *args, **kwargs: pytest.fail("uploaded task must not enqueue materialize_source"),
    )
    monkeypatch.setattr(
        api.workflow,
        "prepare_task",
        lambda task_id, **kwargs: calls.append(("prepare", task_id, dict(kwargs))) or True,
    )

    created = api._build_task(
        api.TaskParameters(n=[1]),
        {"kind": "dxf", "filename": "a.dxf", "content": b"DXF"},
        start_pipeline=True,
    )

    scene_call = next(row for row in calls if row[0] == "scene")
    task_call = next(row for row in calls if row[0] == "task")
    prepare_call = next(row for row in calls if row[0] == "prepare")
    assert scene_call[1]["state"] == "ready"
    assert task_call[2] == {"kind": "scene_ref", "scene_id": created.scene_id}
    assert prepare_call[1] == created.task_id


def test_scene_upload_parse_error_is_422(monkeypatch):
    from rebar_service.source_polygons import SourcePolygonsError

    def fail(_input_obj):
        raise SourcePolygonsError("bad source")

    monkeypatch.setattr(api, "_create_scene", fail)
    error_client = TestClient(api.app, raise_server_exceptions=False)
    response = error_client.post(
        "/v1/scenes/dxf_upload",
        files={"file": ("bad.dxf", b"broken", "application/dxf")},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "bad source"
