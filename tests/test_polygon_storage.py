from shapely.geometry import Polygon

from rebar_service.polygon_storage import canonicalize_polygons, geometry_polygons


def test_canonicalize_polygons_removes_runtime_geometry_and_smoothing_debug_fields():
    rows = [
        {
            "points": [[0, 0], [1000, 0], [1000, 1000]],
            "geometry": Polygon([(0, 0), (1000, 0), (1000, 1000)]),
            "load": 5.5,
            "old_load": 7.0,
            "lower_touch_ratio": 0.75,
            "same_or_higher_neighbors": [1],
            "color": 3,
        }
    ]
    assert canonicalize_polygons(rows) == [
        {
            "points": [[0.0, 0.0], [1000.0, 0.0], [1000.0, 1000.0]],
            "load": 5.5,
            "color": 3,
        }
    ]


def test_geometry_polygons_rebuilds_shapely_geometry_from_canonical_json():
    rows = geometry_polygons(
        [{"points": [[0, 0], [1000, 0], [1000, 1000]], "load": 4.2, "color": 7}]
    )
    assert rows[0]["geometry"].area == 500000.0
    assert rows[0]["load"] == 4.2
    assert rows[0]["color"] == 7
