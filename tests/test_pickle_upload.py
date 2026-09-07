import pickle

import numpy as np
import pytest
from shapely.geometry import Polygon


def test_restricted_pickle_accepts_numpy_points_and_shapely_polygon():
    from rebar_service.safe_pickle import load_source_polygons_pickle

    points = np.asarray([[0.0, 0.0], [100.0, 0.0], [100.0, 100.0], [0.0, 100.0]])
    payload = pickle.dumps([{"points": points, "geometry": Polygon(points), "load": 5.7, "color": 181}], protocol=4)

    result = load_source_polygons_pickle(payload)

    assert result["kind"] == "polygons"
    assert result["units"] == "mm"
    assert result["polygons"][0]["load"] == 5.7
    assert result["polygons"][0]["color"] == 181
    assert result["polygons"][0]["points"][2] == [100.0, 100.0]


def test_restricted_pickle_rejects_arbitrary_globals():
    from rebar_service.safe_pickle import UnsafePickleError, load_source_polygons_pickle

    class Evil:
        def __reduce__(self):
            return (eval, ("40 + 2",))

    with pytest.raises(UnsafePickleError):
        load_source_polygons_pickle(pickle.dumps([Evil()], protocol=4))
