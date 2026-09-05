from contextlib import contextmanager

from shapely.geometry import box, mapping

from rebar_service.config import Settings
from rebar_service.postgres_store import PostgresStore


class _MappingsResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FrontierConnection:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, statement, params=None):
        return _MappingsResult(self._rows)


class _FrontierDatabase:
    def __init__(self, rows):
        self._rows = rows

    @contextmanager
    def connect(self):
        yield _FrontierConnection(self._rows)


def test_load_frontier_restores_nested_geojson_geometry_to_shapely():
    geometry = mapping(box(0, 0, 10, 20))
    rows = [
        {
            "n": 2,
            "result": {
                "is_feasible": True,
                "anchored_boxes": [{"geometry": geometry, "class": 1}],
                "rectangles": [{"geometry": geometry, "class": 1}],
            },
        }
    ]
    store = PostgresStore(Settings(), database=_FrontierDatabase(rows))

    frontier = store.load_frontier("task123", 0, variant="smooth", overlay_id=0)

    anchored_geometry = frontier[2]["anchored_boxes"][0]["geometry"]
    rectangle_geometry = frontier[2]["rectangles"][0]["geometry"]
    assert anchored_geometry.geom_type == "Polygon"
    assert rectangle_geometry.geom_type == "Polygon"
    assert anchored_geometry.area == 200.0
