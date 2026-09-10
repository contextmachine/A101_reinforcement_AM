from __future__ import annotations

import pytest

from rebar_service.compact_zones import compact_zones_from_layout


def test_regular_vertical_tracks_become_one_compact_zone():
    layout = {
        "axis": "y",
        "zones": [
            {
                "id": 1,
                "background": False,
                "diameter": 20,
                "step": 200,
                "bars_with_anchorage_unclipped": [
                    (1000, 2000, 1000, 6000),
                    (1200, 2000, 1200, 6000),
                    (1400, 2000, 1400, 6000),
                ],
            }
        ],
    }
    rows = compact_zones_from_layout(layout)
    assert rows == [
        {
            "origin": [1000.0, 2000.0],
            "direction": [1.0, 0.0],
            "length": 4000.0,
            "step": 200.0,
            "right": 2,
            "left": 0,
            "d": 20.0,
        }
    ]
    assert "count" not in rows[0]


def test_irregular_track_positions_are_split_without_geometry_loss():
    layout = {
        "zones": [
            {
                "id": 1,
                "background": False,
                "diameter": 16,
                "step": 200,
                "bars_with_anchorage_unclipped": [
                    (0, 0, 0, 1000), (200, 0, 200, 1000),
                    (450, 0, 450, 1000), (650, 0, 650, 1000),
                ],
            }
        ]
    }
    rows = compact_zones_from_layout(layout)
    assert len(rows) == 2
    assert sorted(row["right"] for row in rows) == [1, 1]
    origins = sorted(row["origin"][0] for row in rows)
    assert origins == pytest.approx([0, 450])
