from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import rebar_service.api as api
from rebar_service.overlays import resolve_overlay, resolve_overlay_selector
from rebar_service.source_polygons import SourcePolygonsError, source_polygons_from_json_bytes
from rebar_service.v2 import scenes


client = TestClient(api.app)
error_client = TestClient(api.app, raise_server_exceptions=False)

SQUARE = [[0.0, 0.0], [1000.0, 0.0], [1000.0, 1000.0], [0.0, 1000.0]]


class FakeStore:
    """In-memory double implementing the store contract used by the /v2 scene routes."""

    def __init__(self, polygons=None, state="ready"):
        self.polygons = [dict(row) for row in (polygons or [])]
        self.state = state
        self.scenes: dict[str, dict] = {}
        self.created: list[tuple[str, dict, dict]] = []
        self.events: list[dict] = []
        self.create_error: Exception | None = None

    # --- scenes -----------------------------------------------------------
    def create_scene(self, scene_id, meta, input_obj):
        if self.create_error is not None:
            raise self.create_error
        self.created.append((scene_id, dict(meta), dict(input_obj)))
        self.scenes[scene_id] = {"scene_id": scene_id, "state": str(meta.get("state", "ready"))}

    def get_scene(self, scene_id):
        return self.scenes.get(scene_id)

    def add_scene(self, scene_id="scene1"):
        self.scenes[scene_id] = {"scene_id": scene_id, "state": self.state}
        return scene_id

    # --- overlays ---------------------------------------------------------
    def scene_overlay_events(self, scene_id):
        if scene_id not in self.scenes:
            raise KeyError(scene_id)
        return [dict(row) for row in self.events]

    def resolve_scene_overlay_id(self, scene_id, selector=0):
        if scene_id not in self.scenes:
            raise KeyError(scene_id)
        return resolve_overlay_selector(self.events, selector)

    def resolved_scene_polygons(self, scene_id, *, variant="raw", overlay_id=0):
        if scene_id not in self.scenes:
            raise KeyError(scene_id)
        rows = resolve_overlay(self.polygons, self.events, overlay_id)
        return [{**row, "variant": variant} for row in rows]

    def append_scene_overlay_events(self, scene_id, events):
        if scene_id not in self.scenes:
            raise KeyError(scene_id)
        for raw in events:
            event_type = str(raw.get("type"))
            if event_type not in {"clean", "unclean"}:
                raise ValueError("overlay type must be 'clean' or 'unclean'")
            head = self.events[-1]["id"] if self.events else 0
            state = resolve_overlay(self.polygons, self.events, head)
            idxs = []
            for idx in raw.get("idxs") or []:
                idx = int(idx)
                if idx < 0 or idx >= len(self.polygons):
                    raise ValueError(f"source polygon indices out of range: {idx}")
                current = state[idx]["overlay_state"]
                if event_type == "clean" and current != "active":
                    continue
                if event_type == "unclean" and current == "active":
                    continue
                idxs.append(idx)
            self.events.append(
                {
                    "seq": len(self.events) + 1,
                    "id": max((row["id"] for row in self.events), default=0) + 1,
                    "type": event_type,
                    "idxs": idxs,
                    "real": bool(raw.get("real", False)),
                    "created_at": 0.0,
                    "time": raw.get("time"),
                }
            )
        return [dict(row) for row in self.events]


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore(
        polygons=[
            {"points": SQUARE, "load": 5.7, "color": 181},
            {"points": SQUARE, "load": 3.0},
            {"points": SQUARE, "load": 1.5},
        ]
    )
    monkeypatch.setattr(api, "store", fake)
    return fake


def test_v2_scene_routes_exist_with_unique_handler_names():
    paths = api.app.openapi()["paths"]
    for route in (
        "/v2/dxf_upload",
        "/v2/json_upload",
        "/v2/tables_upload",
        "/v2/scenes/{scene_id}/polygons",
        "/v2/scenes/{scene_id}/overlays",
        "/v2/scenes/{scene_id}/overlays/{overlay_id}",
    ):
        assert route in paths
    names = [getattr(route, "name", None) for route in api.app.routes]
    duplicates = {name for name in names if name and names.count(name) > 1}
    assert not duplicates


def test_dxf_upload_stores_the_raw_source_and_returns_ready_scene(store):
    response = client.post("/v2/dxf_upload", files={"file": ("a.dxf", b"0\nEOF\n", "application/dxf")})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "ready" and len(body["scene_id"]) == 32
    scene_id, meta, input_obj = store.created[0]
    assert scene_id == body["scene_id"] and meta["state"] == "ready"
    assert input_obj["kind"] == "dxf" and input_obj["content"] == b"0\nEOF\n"


def test_dxf_upload_rejects_other_extensions(store):
    response = client.post("/v2/dxf_upload", files={"file": ("a.json", b"[]", "application/json")})
    assert response.status_code == 415


def test_json_upload_accepts_a_bare_polygon_array_and_keeps_color(store):
    payload = [
        {"load": 5.7, "color": 181, "points": SQUARE},
        {"load": 3.0, "points": SQUARE},
    ]
    response = client.post("/v2/json_upload", json=payload)
    assert response.status_code == 200, response.text
    input_obj = store.created[0][2]
    assert input_obj["kind"] == "json"
    parsed = source_polygons_from_json_bytes(input_obj["content"])
    assert parsed["polygons"][0]["color"] == 181
    assert "color" not in parsed["polygons"][1]
    assert parsed["polygons"][0]["load"] == 5.7
    assert json.loads(input_obj["content"])[0]["points"][0] == [0.0, 0.0]


def test_json_upload_rejects_degenerate_polygons(store):
    response = client.post("/v2/json_upload", json=[{"load": 1.0, "points": [[0, 0], [1, 1]]}])
    assert response.status_code == 422
    assert not store.created


def test_tables_upload_packs_the_three_workbooks(store):
    from test_xlsx_source_polygons import _sample_tables
    from rebar_service.source_polygons import source_polygons_from_xlsx_bundle

    nodes, elements, loads = _sample_tables()
    response = client.post(
        "/v2/tables_upload",
        data={"load_column": "2"},
        files={
            "nodes_file": ("nodes.xlsx", nodes),
            "elements_file": ("elements.xlsx", elements),
            "loads_file": ("loads.xlsx", loads),
        },
    )
    assert response.status_code == 200, response.text
    input_obj = store.created[0][2]
    assert input_obj["kind"] == "xlsx_tables" and input_obj["load_column"] == 2
    parsed = source_polygons_from_xlsx_bundle(input_obj["content"], load_column=2)
    assert parsed["polygons"][0]["load"] == 8.1


def test_upload_parse_failure_is_422(store):
    store.create_error = SourcePolygonsError("bad source")
    response = error_client.post("/v2/dxf_upload", files={"file": ("a.dxf", b"broken")})
    assert response.status_code == 422
    assert response.json()["detail"] == "bad source"


def test_scene_polygons_returns_a_bare_array_with_wire_overlay_states(store):
    scene_id = store.add_scene()
    store.append_scene_overlay_events(scene_id, [{"type": "clean", "idxs": [1], "real": True}])
    store.append_scene_overlay_events(scene_id, [{"type": "clean", "idxs": [2], "real": False}])

    response = client.get(f"/v2/scenes/{scene_id}/polygons?overlay_id=-1")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert isinstance(rows, list) and len(rows) == 3
    assert [row["overlay_state"] for row in rows] == ["active", "real", "empty"]
    assert [row["source_index"] for row in rows] == [0, 1, 2]
    assert rows[0]["color"] == 181 and rows[1]["color"] is None
    assert rows[0]["load"] == 5.7 and rows[0]["points"][2] == [1000.0, 1000.0]
    assert set(rows[0]) == {"load", "color", "points", "overlay_state", "source_index"}


def test_scene_polygons_base_revision_is_all_active(store):
    scene_id = store.add_scene()
    store.append_scene_overlay_events(scene_id, [{"type": "clean", "idxs": [0], "real": False}])
    rows = client.get(f"/v2/scenes/{scene_id}/polygons").json()
    assert [row["overlay_state"] for row in rows] == ["active", "active", "active"]


def test_scene_polygons_selects_the_smooth_variant(store):
    scene_id = store.add_scene()
    seen = {}
    original = store.resolved_scene_polygons

    def spy(scene, *, variant="raw", overlay_id=0):
        seen["variant"] = variant
        return original(scene, variant=variant, overlay_id=overlay_id)

    store.resolved_scene_polygons = spy
    assert client.get(f"/v2/scenes/{scene_id}/polygons?smooth=true").status_code == 200
    assert seen["variant"] == "smooth"


def test_scene_polygons_missing_scene_is_404_and_unready_scene_is_409(store):
    assert client.get("/v2/scenes/missing/polygons").status_code == 404
    store.state = "preparing"
    scene_id = store.add_scene("preparing-scene")
    assert client.get(f"/v2/scenes/{scene_id}/polygons").status_code == 409


def test_scene_polygons_unknown_overlay_selector_is_404(store):
    scene_id = store.add_scene()
    assert client.get(f"/v2/scenes/{scene_id}/polygons?overlay_id=777").status_code == 404
    assert client.get(f"/v2/scenes/{scene_id}/polygons?overlay_id=-1").status_code == 404


def test_append_overlays_returns_the_id_of_the_last_stored_event(store):
    scene_id = store.add_scene()
    response = client.post(
        f"/v2/scenes/{scene_id}/overlays",
        json={
            "scene_id": scene_id,
            "overlays": [
                {"type": "clean", "idxs": [0], "real": True, "time": 12345654},
                {"type": "clean", "idxs": [1, 2], "real": False, "time": 12345667},
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"scene_id": scene_id, "overlay_id": 2}
    assert [row["id"] for row in store.events] == [1, 2]
    assert store.events[0]["time"] == 12345654


def test_append_overlays_still_returns_an_id_when_every_index_is_skipped(store):
    scene_id = store.add_scene()
    client.post(
        f"/v2/scenes/{scene_id}/overlays",
        json={"overlays": [{"type": "clean", "idxs": [0], "real": False}]},
    )
    response = client.post(
        f"/v2/scenes/{scene_id}/overlays",
        json={"overlays": [{"type": "clean", "idxs": [0], "real": False}]},
    )
    assert response.status_code == 200
    assert response.json()["overlay_id"] == 2
    assert store.events[-1]["idxs"] == []


def test_append_overlays_rejects_empty_body_mismatched_scene_and_bad_index(store):
    scene_id = store.add_scene()
    assert client.post(f"/v2/scenes/{scene_id}/overlays", json={"overlays": []}).status_code == 422
    mismatched = client.post(
        f"/v2/scenes/{scene_id}/overlays",
        json={"scene_id": "other", "overlays": [{"type": "clean", "idxs": [0]}]},
    )
    assert mismatched.status_code == 422
    out_of_range = client.post(
        f"/v2/scenes/{scene_id}/overlays",
        json={"overlays": [{"type": "clean", "idxs": [99]}]},
    )
    assert out_of_range.status_code == 422
    assert client.post("/v2/scenes/missing/overlays", json={"overlays": []}).status_code == 404


def test_get_overlays_returns_the_log_up_to_the_selected_revision(store):
    scene_id = store.add_scene()
    store.append_scene_overlay_events(
        scene_id,
        [
            {"type": "clean", "idxs": [0], "real": True, "time": 12345654},
            {"type": "clean", "idxs": [1], "real": False},
            {"type": "unclean", "idxs": [0]},
        ],
    )
    assert client.get(f"/v2/scenes/{scene_id}/overlays/0").json() == []
    latest = client.get(f"/v2/scenes/{scene_id}/overlays/-1").json()
    assert [row["id"] for row in latest] == [1, 2, 3]
    assert latest[0] == {"id": 1, "type": "clean", "idxs": [0], "real": True, "time": 12345654}
    assert latest[1]["time"] is None
    assert client.get(f"/v2/scenes/{scene_id}/overlays/-2").json() == latest[:2]
    assert client.get(f"/v2/scenes/{scene_id}/overlays/2").json() == latest[:2]


def test_get_overlays_errors(store):
    scene_id = store.add_scene()
    assert client.get(f"/v2/scenes/{scene_id}/overlays/77").status_code == 404
    assert client.get("/v2/scenes/missing/overlays/0").status_code == 404


def test_wire_overlay_state_translation_is_a_bijection():
    for stored, wire in (("active", "active"), ("background_only", "real"), ("removed", "empty")):
        assert scenes.to_wire_overlay_state(stored) == wire
        assert scenes.from_wire_overlay_state(wire) == stored
    with pytest.raises(ValueError):
        scenes.to_wire_overlay_state("real")
    with pytest.raises(ValueError):
        scenes.from_wire_overlay_state("removed")
