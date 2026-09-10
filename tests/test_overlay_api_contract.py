"""Public HTTP/model contracts, independent of source formatting or comments."""
from fastapi.testclient import TestClient
from rebar_service import api
from rebar_service.models import OverlayEventMutation, WsCommand


def _operation(path, method="get"):
    return api.app.openapi()["paths"][path][method]


def test_api_exposes_overlay_journal_and_removes_prepare_endpoint():
    paths = api.app.openapi()["paths"]
    assert {"post", "get"}.issubset(paths["/v1/tasks/{task_id}/overlays"])
    assert "/v1/tasks/{task_id}/components/prepare" not in paths


def test_analysis_routes_accept_overlay_and_source_polygons_accepts_smooth_overlay():
    for suffix, method in [("source-polygons", "get"), ("components", "get"),
                           ("components/{component_id}/n", "post"),
                           ("components/{component_id}/results", "get"),
                           ("components/{component_id}/results/{n}", "get"),
                           ("results", "get"), ("results/{n}", "get"),
                           ("results/{n}/dxf", "get"), ("n", "post")]:
        params = {p["name"]: p for p in _operation("/v1/tasks/{task_id}/" + suffix, method).get("parameters", [])}
        for name in ("smooth", "overlay"):
            assert name in params and not params[name].get("required", False)


def test_load_column_exists_only_on_tables_upload():
    s = api.app.openapi()
    def body(path):
        ref = s["paths"][path]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"]
        return s["components"]["schemas"][ref.rsplit("/", 1)[-1]]["properties"]
    assert "load_column" not in body("/v1/tasks/upload")
    column = body("/v1/tasks/tables_upload")["load_column"]
    assert column["type"] == "integer" and column["minimum"] == 1 and column["maximum"] == 4


def test_cancel_and_websocket_commands_can_scope_n_to_overlay_analysis():
    params = {p["name"] for p in _operation("/v1/tasks/{task_id}/cancel", "post")["parameters"]}
    assert {"smooth", "overlay"}.issubset(params)
    assert WsCommand.model_validate({"action": "cancel", "n": [2], "overlay": -1}).overlay == -1


def test_overlay_event_request_defaults_real_false_and_deduplicates_indices():
    row = OverlayEventMutation.model_validate({"type": "clean", "idxs": [3, 3, 4], "id": 123})
    assert row.real is False and row.idxs == [3, 4]


def test_component_results_expose_normalized_status(monkeypatch):
    monkeypatch.setattr(api.store, "get_meta", lambda tid: {"scene_id": "s", "analysis_variant": "raw", "analysis_overlay_id": 0})
    monkeypatch.setattr(api.store, "load_frontier", lambda *a, **kw: {1: {"status": "optimal", "is_feasible": True, "is_optimal": True}})
    response = TestClient(api.app).get("/v1/tasks/t/components/0/results")
    assert response.status_code == 200
    row = response.json()["results"][0]
    assert row["status"] == "optimal" and row["is_optimal"] is True
