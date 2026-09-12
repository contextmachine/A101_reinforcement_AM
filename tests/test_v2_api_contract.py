from types import SimpleNamespace

from fastapi.testclient import TestClient

import rebar_service.api as api


client = TestClient(api.app)


def _task_body():
    return {
        "scene_id": "scene",
        "overlay_id": 0,
        "smooth": False,
        "n": [2, 4],
        "config": {
            "max_layers": 2,
            "axis": "x",
            "anchor_factor": 40,
            "min_width_mm": 300,
            "max_snap_mm": 600,
            "min_bar_gap_mm": 50,
            "steel_density_kg_m3": 7850,
            "back_grid": {"d": 18.0, "step": 300.0},
            "stock": [{"d": 20.0, "step": 150.0}],
            "solver": {"solver_time_limit": None},
        },
    }


def test_v2_task_start_is_whole_field_and_only_enqueues_preparing(monkeypatch):
    created = {}
    jobs = []
    monkeypatch.setattr(api.store, "get_scene", lambda sid: {"scene_id": sid, "state": "ready"})
    monkeypatch.setattr(api.store, "resolve_scene_overlay_id", lambda sid, oid: 0)
    monkeypatch.setattr(api.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))

    def create(task_id, **kwargs):
        created.update(task_id=task_id, **kwargs)
        return {"task_id": task_id}

    monkeypatch.setattr(api.store, "create_v2_task", create)
    monkeypatch.setattr(api.store, "enqueue_v2_job", lambda row: jobs.append(row) or True)

    response = client.put("/v2/tasks", json=_task_body())
    assert response.status_code == 200, response.text
    assert response.json() == {"task_id": "a" * 32}
    assert created["ns"] == [2, 4]
    assert "components" not in created
    assert len(jobs) == 1
    assert jobs[0]["stage"] == "preparing"
    assert jobs[0]["kind"] == "prepare"

    invalid = _task_body()
    invalid["components"] = [-1]
    assert client.put("/v2/tasks", json=invalid).status_code == 422


def test_v2_get_task_returns_all_requested_n_with_real_persisted_states(monkeypatch):
    monkeypatch.setattr(api.store, "get_v2_task", lambda tid: {
        "task_id": tid,
        "scene_id": "scene",
        "smooth": False,
        "overlay_id": 0,
        "preparation_state": "success",
        "config": {"hidden": "not public"},
        "solutions": [
            {"n": 2, "position": 0, "attempt": 1, "state": "solving"},
            {"n": 4, "position": 1, "attempt": 2, "state": "success", "status": "feasible", "fun": 12.0, "mass_kg": 9.0, "mass_bg_kg": 3.0},
        ],
    })
    response = client.get("/v2/tasks/task")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "task_id": "task",
        "scene_id": "scene",
        "smooth": False,
        "overlay_id": 0,
        "solutions": [
            {"n": 2, "state": "solving"},
            {"n": 4, "state": "success", "status": "feasible", "fun": 12.0, "mass_kg": 9.0, "mass_bg_kg": 3.0},
        ],
    }


def test_v2_add_n_reuses_preparation_and_schedules_only_new_ready_rows(monkeypatch):
    calls = []
    monkeypatch.setattr(api.store, "get_v2_task", lambda tid: {"task_id": tid, "preparation_state": "success"})
    monkeypatch.setattr(api.store, "add_v2_ns", lambda tid, ns: calls.append(("add", tid, ns)) or {"created": [7], "retried": [], "kept": []})
    monkeypatch.setattr(api.v2_workflow, "schedule_ready_n", lambda tid: calls.append(("schedule", tid)))

    response = client.put("/v2/tasks/task/n", json={"task_id": "task", "n": [7]})
    assert response.status_code == 200
    assert calls == [("add", "task", [7]), ("schedule", "task")]


def test_v2_json_upload_accepts_documented_raw_polygon_array(monkeypatch):
    monkeypatch.setattr(api, "_create_scene", lambda obj: api.SceneCreated(scene_id="s" * 32, state="ready"))
    response = client.post("/v2/json_upload", json=[{
        "load": 5.7,
        "color": 181,
        "points": [[0, 0], [100, 0], [100, 100], [0, 100]],
    }])
    assert response.status_code == 200, response.text
    assert response.json() == {"scene_id": "s" * 32, "state": "ready"}


def test_v2_scene_polygons_returns_laconic_array(monkeypatch):
    monkeypatch.setattr(api.store, "get_scene", lambda sid: {"scene_id": sid, "state": "ready"})
    monkeypatch.setattr(api.store, "resolve_scene_overlay_id", lambda sid, oid: 12)
    monkeypatch.setattr(api.store, "resolved_scene_polygons", lambda *args, **kwargs: [{
        "load": 5.7,
        "color": 181,
        "points": [[0, 0], [1, 0], [1, 1]],
        "overlay_state": "active",
        "source_index": 3,
        "internal": "must not leak",
    }])
    response = client.get("/v2/scenes/scene/polygons?smooth=false&overlay_id=-1")
    assert response.status_code == 200
    assert response.json() == [{
        "load": 5.7,
        "color": 181,
        "points": [[0, 0], [1, 0], [1, 1]],
        "overlay_state": "active",
        "source_index": 3,
    }]


def test_v2_overlays_post_allocates_snapshot_id_and_get_returns_snapshot(monkeypatch):
    monkeypatch.setattr(api.store, "get_scene", lambda sid: {"scene_id": sid, "state": "ready"})
    monkeypatch.setattr(api.store, "append_v2_scene_overlays", lambda sid, rows: {
        "overlay_id": 22,
        "rows": [
            {"id": 21, "type": "clean", "idxs": [3], "real": True},
            {"id": 22, "type": "unclean", "idxs": [4], "real": False},
        ],
    })
    response = client.post("/v2/scenes/scene/overlays", json={
        "scene_id": "scene",
        "overlays": [
            {"type": "clean", "idxs": [3], "real": True, "time": 123},
            {"type": "unclean", "idxs": [4], "real": False, "time": 124},
        ],
    })
    assert response.status_code == 200, response.text
    assert response.json() == {"scene_id": "scene", "overlay_id": 22}

    monkeypatch.setattr(api.store, "resolve_scene_overlay_id", lambda sid, selector: 22)
    monkeypatch.setattr(api.store, "scene_overlay_events", lambda sid: [
        {"seq": 1, "id": 21, "type": "clean", "idxs": [3], "real": True, "created_at": 1},
        {"seq": 2, "id": 22, "type": "unclean", "idxs": [4], "real": False, "created_at": 2},
        {"seq": 3, "id": 23, "type": "clean", "idxs": [5], "real": True, "created_at": 3},
    ])
    got = client.get("/v2/scenes/scene/overalys/22")
    assert got.status_code == 200
    assert got.json() == [
        {"type": "clean", "idxs": [3], "id": 21, "real": True},
        {"type": "unclean", "idxs": [4], "id": 22, "real": False},
    ]


def _bars_body():
    return {
        "scene_id": "scene",
        "smooth": False,
        "overlay_id": 0,
        "config": {"axis": "x", "anchor_factor": 40, "min_bar_gap_mm": 50},
        "zones": [
            {"id": 0, "kind": "bg", "arm": {"d": 18.0, "step": 300.0}},
            {"id": 1, "kind": "additional", "arm": {"d": 20.0, "step": 150.0},
             "left": 0, "right": 0, "length": 1000.0,
             "start_anchorage": 800.0, "end_anchorage": 800.0,
             "origin": [0.0, 0.0], "direction": [0.0, -1.0]},
        ],
    }


def test_v2_bars_post_is_async_and_get_returns_persisted_result(monkeypatch):
    jobs = []
    monkeypatch.setattr(api.store, "get_scene", lambda sid: {"scene_id": sid, "state": "ready"})
    monkeypatch.setattr(api.store, "resolve_scene_overlay_id", lambda sid, oid: 0)
    monkeypatch.setattr(api.uuid, "uuid4", lambda: SimpleNamespace(hex="b" * 32))
    monkeypatch.setattr(api.store, "create_v2_request", lambda kind, rid, **kwargs: {"bars_id": rid, "state": "pending"})
    monkeypatch.setattr(api.store, "enqueue_v2_job", lambda row: jobs.append(row) or True)

    response = client.post("/v2/bars", json=_bars_body())
    assert response.status_code == 200, response.text
    assert response.json() == {"bars_id": "b" * 32, "state": "pending"}
    assert jobs[0]["stage"] == "baring" and jobs[0]["kind"] == "bars_request"

    monkeypatch.setattr(api.store, "get_v2_request", lambda kind, rid: {
        "bars_id": rid, "state": "success",
        "result": {"bar_layout": {"bars": []}, "mass_metrics": {
            "additional": {"with_anchorage_kg": 2.0, "without_anchorage_kg": 1.0},
            "bg": {"with_anchorage_kg": 3.0, "without_anchorage_kg": 3.0},
        }},
    })
    got = client.get("/v2/bars/" + "b" * 32)
    assert got.status_code == 200
    assert got.json() == {
        "bars_id": "b" * 32, "state": "success",
        "bar_layout": {"bars": []}, "mass_metrics": {
            "additional": {"with_anchorage_kg": 2.0, "without_anchorage_kg": 1.0},
            "bg": {"with_anchorage_kg": 3.0, "without_anchorage_kg": 3.0},
        },
    }


def test_v2_verification_post_is_async_and_get_returns_result(monkeypatch):
    jobs = []
    body = _bars_body()
    body["config"] = {
        "axis": "x", "anchor_factor": 40, "min_bar_gap_mm": 50,
        "steel_density_kg_m3": 7850, "t": 600,
    }
    monkeypatch.setattr(api.store, "get_scene", lambda sid: {"scene_id": sid, "state": "ready"})
    monkeypatch.setattr(api.store, "resolve_scene_overlay_id", lambda sid, oid: 0)
    monkeypatch.setattr(api.uuid, "uuid4", lambda: SimpleNamespace(hex="v" * 32))
    monkeypatch.setattr(api.store, "create_v2_request", lambda kind, rid, **kwargs: {"verification_id": rid, "state": "pending"})
    monkeypatch.setattr(api.store, "enqueue_v2_job", lambda row: jobs.append(row) or True)

    response = client.post("/v2/verification", json=body)
    assert response.status_code == 200, response.text
    assert response.json() == {"verification_id": "v" * 32, "state": "pending"}
    assert jobs[0]["stage"] == "validation" and jobs[0]["kind"] == "verification"

    result = [{
        "source_index": 0, "overlay_state": "active", "need_load_sm2/m": 5.7,
        "fact_load_sm2/m": 6.8, "need_load_kg/m3": 19.6, "fact_load_kg/m3": 28.4,
    }]
    monkeypatch.setattr(api.store, "get_v2_request", lambda kind, rid: {
        "verification_id": rid, "state": "success", "result": result,
    })
    got = client.get("/v2/verification/" + "v" * 32)
    assert got.status_code == 200
    assert got.json() == {"verification_id": "v" * 32, "state": "success", "result": result}


def test_v2_openapi_has_explicit_laconic_response_schemas_without_components():
    schema = client.get('/openapi.json').json()
    for path, method in (
        ('/v2/tasks', 'put'),
        ('/v2/tasks/{task_id}', 'get'),
        ('/v2/tasks/{task_id}/{n}', 'get'),
        ('/v2/bars', 'post'),
        ('/v2/bars/{bars_id}', 'get'),
        ('/v2/verification', 'post'),
        ('/v2/verification/{verification_id}', 'get'),
        ('/v2/dxf_upload', 'post'),
        ('/v2/json_upload', 'post'),
        ('/v2/tables_upload', 'post'),
        ('/v2/scenes/{scene_id}/polygons', 'get'),
        ('/v2/scenes/{scene_id}/overlays', 'post'),
        ('/v2/scenes/{scene_id}/overalys/{overlay_id}', 'get'),
    ):
        response_schema = schema['paths'][path][method]['responses']['200']['content']['application/json']['schema']
        assert response_schema, (path, method)
        assert '$ref' in response_schema or response_schema.get('type') in {'array', 'object'}

    component_blob = str({
        name: body for name, body in schema['components']['schemas'].items()
        if name.startswith('V2')
    })
    assert 'component_id' not in component_blob
    assert 'component_selection' not in component_blob


def test_v2_bars_get_filters_internal_error_diagnostics(monkeypatch):
    monkeypatch.setattr(api.store, 'get_v2_request', lambda kind, rid: {
        'bars_id': rid,
        'state': 'error',
        'error': {'stage': 'baring', 'message': 'failed', 'traceback': 'secret internal trace', 'job_id': 'j'},
    })
    got = client.get('/v2/bars/id')
    assert got.status_code == 200
    assert got.json() == {
        'bars_id': 'id', 'state': 'error',
        'error': {'stage': 'baring', 'message': 'failed'},
    }


def test_v2_overlay_snapshot_uses_event_sequence_not_numeric_overlay_id(monkeypatch):
    monkeypatch.setattr(api.store, "get_scene", lambda sid: {"scene_id": sid, "state": "ready"})
    monkeypatch.setattr(api.store, "resolve_scene_overlay_id", lambda sid, selector: 5)
    monkeypatch.setattr(api.store, "scene_overlay_events", lambda sid: [
        {"seq": 1, "id": 100, "type": "clean", "idxs": [1], "real": True},
        {"seq": 2, "id": 5, "type": "unclean", "idxs": [2], "real": False},
        {"seq": 3, "id": 6, "type": "clean", "idxs": [3], "real": True},
    ])
    got = client.get("/v2/scenes/scene/overalys/5")
    assert got.status_code == 200
    assert [row["id"] for row in got.json()] == [100, 5]


def test_v2_bars_and_verification_reject_invalid_zone_sets_before_enqueue(monkeypatch):
    jobs = []
    monkeypatch.setattr(api.store, "enqueue_v2_job", lambda row: jobs.append(row) or True)
    bad = _bars_body()
    bad["zones"] = [bad["zones"][1]]  # no background zone
    assert client.post("/v2/bars", json=bad).status_code == 422

    verification = _bars_body()
    verification["config"] = {
        "axis": "x", "anchor_factor": 40, "min_bar_gap_mm": 50,
        "steel_density_kg_m3": 7850, "t": 600,
    }
    verification["zones"].append(dict(verification["zones"][0]))  # two backgrounds / duplicate id
    assert client.post("/v2/verification", json=verification).status_code == 422
    assert jobs == []
