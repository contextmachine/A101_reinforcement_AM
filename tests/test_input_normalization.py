from rebar_service.pipeline import normalize_input_payload


def test_legacy_polygon_json_list_is_normalized():
    raw = [[[[0, 0], [2, 0], [2, 1], [0, 1]], 7.5]]
    payload = normalize_input_payload(raw)
    assert payload == {
        "kind": "polygons",
        "units": "mm",
        "polygons": [{"points": [[0, 0], [2, 0], [2, 1], [0, 1]], "load": 7.5}],
    }


def test_polygon_object_without_kind_is_normalized():
    raw = {"units": "m", "polygons": [{"points": [[0, 0], [1, 0], [1, 1]], "load": 3}]}
    payload = normalize_input_payload(raw)
    assert payload["kind"] == "polygons"
    assert payload["units"] == "m"
