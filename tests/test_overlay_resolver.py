from rebar_service.overlays import normalize_overlay_id, resolve_overlay


POLYGONS = [
    {"points": [[0, 0], [1, 0], [1, 1]], "load": 1.0},
    {"points": [[1, 0], [2, 0], [2, 1]], "load": 2.0},
    {"points": [[2, 0], [3, 0], [3, 1]], "load": 3.0},
]


def test_overlay_zero_keeps_every_polygon_active():
    rows = resolve_overlay(POLYGONS, [], 0)
    assert [row["overlay_state"] for row in rows] == ["active", "active", "active"]
    assert [row["source_index"] for row in rows] == [0, 1, 2]


def test_clean_real_false_removes_polygon_and_real_true_keeps_background_only():
    events = [
        {"seq": 1, "id": 100, "type": "clean", "idxs": [0], "real": False},
        {"seq": 2, "id": 101, "type": "clean", "idxs": [1], "real": True},
    ]
    rows = resolve_overlay(POLYGONS, events, 101)
    assert rows[0]["overlay_state"] == "removed"
    assert rows[0]["active"] is False
    assert rows[0]["real"] is False
    assert rows[1]["overlay_state"] == "background_only"
    assert rows[1]["active"] is False
    assert rows[1]["real"] is True
    assert rows[2]["overlay_state"] == "active"


def test_unclean_restores_active_and_overlay_stops_at_requested_id():
    events = [
        {"seq": 1, "id": 100, "type": "clean", "idxs": [0], "real": False},
        {"seq": 2, "id": 101, "type": "unclean", "idxs": [0], "real": True},
        {"seq": 3, "id": 102, "type": "clean", "idxs": [2], "real": False},
    ]
    through_100 = resolve_overlay(POLYGONS, events, 100)
    assert through_100[0]["overlay_state"] == "removed"
    assert through_100[2]["overlay_state"] == "active"

    through_101 = resolve_overlay(POLYGONS, events, 101)
    assert through_101[0]["overlay_state"] == "active"
    # real only changes clean semantics; an unclean polygon is ordinary ACTIVE again.
    assert through_101[0]["real"] is False
    assert through_101[2]["overlay_state"] == "active"


def test_unknown_overlay_id_is_rejected_and_overlay_id_must_be_nonnegative():
    events = [{"seq": 1, "id": 100, "type": "clean", "idxs": [0], "real": False}]
    try:
        resolve_overlay(POLYGONS, events, 999)
    except KeyError as exc:
        assert "999" in str(exc)
    else:
        raise AssertionError("unknown overlay id must fail")

    assert normalize_overlay_id(None) == 0
    assert normalize_overlay_id(0) == 0
    try:
        normalize_overlay_id(-1)
    except ValueError:
        pass
    else:
        raise AssertionError("negative overlay id must fail")


def test_resolver_always_owns_stable_source_index():
    polygons = [{"source_index": 999, "points": [[0, 0], [1, 0], [1, 1]], "load": 1.0}]
    rows = resolve_overlay(polygons, [], 0)
    assert rows[0]["source_index"] == 0
