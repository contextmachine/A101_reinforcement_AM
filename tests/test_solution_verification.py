from __future__ import annotations

import pytest

from rebar_service.solution_verification import reinforcement_area, verify_compact_zones


def test_verification_adds_overlapping_zones_and_background():
    polygons = [{"points": [[0, 0], [1000, 0], [1000, 1000], [0, 1000]], "load": 10, "overlay_state": "active"}]
    zone = {"origin": [500, 0], "direction": [1, 0], "length": 1000, "step": 1000, "right": 0, "left": 0, "d": 20}
    actual = verify_compact_zones(polygons, [zone, zone], back_grid=[10, 1000])[0]
    expected_as = reinforcement_area(10, 1000) + 2 * reinforcement_area(20, 1000)
    assert actual == pytest.approx(100 * expected_as / 10)


def test_verification_omitted_background_is_zero_and_overlay_states_stay_aligned():
    polygons = [
        {"points": [[0, 0], [100, 0], [100, 100], [0, 100]], "load": 5, "overlay_state": "active"},
        {"points": [[100, 0], [200, 0], [200, 100], [100, 100]], "load": 5, "overlay_state": "background_only"},
        {"points": [[200, 0], [300, 0], [300, 100], [200, 100]], "load": 5, "overlay_state": "removed"},
    ]
    assert verify_compact_zones(polygons, [], back_grid=None) == [0.0, 100.0, None]


def test_rotated_zone_rectangle_covers_expected_polygon():
    # direction 45 degrees, bar direction is 135 degrees.
    zone = {"origin": [100, 0], "direction": [2**-0.5, 2**-0.5], "length": 200, "step": 100, "right": 1, "left": 1, "d": 20}
    polygons = [{"points": [[-100, -100], [300, -100], [300, 300], [-100, 300]], "load": 1, "overlay_state": "active"}]
    value = verify_compact_zones(polygons, [zone])[0]
    assert value > 0
