"""``add_box_anchorage`` honours optional per-box ``hold_start``/``hold_end``."""

from __future__ import annotations

import pytest
from shapely.geometry import box

from A101.axis_orientation import add_box_anchorage

DIAMETERS = {1: 10.0}
STEPS = {1: 200.0}


def _one(boxes, **kw):
    rows = add_box_anchorage(boxes, recipes={}, diameters=DIAMETERS, steps=STEPS, anchor_factor=40.0, **kw)
    assert len(rows) == 1
    return rows[0]


def test_default_is_symmetric_anchor_factor_times_diameter():
    row = _one([{"bounds": (0, 0, 100, 1000), "class": 1}], axis="y")
    assert row["hold"] == 400.0
    assert row["anchored_bounds_unclipped"] == (0.0, -400.0, 100.0, 1400.0)
    assert row["bounds"] == (0.0, -400.0, 100.0, 1400.0)


@pytest.mark.parametrize("axis", ["y", "x"])
def test_explicit_hold_start_and_hold_end_are_applied_per_end(axis):
    fitted = (0, 0, 100, 1000) if axis == "y" else (0, 0, 1000, 100)
    row = _one([{"bounds": fitted, "class": 1, "hold_start": 50.0, "hold_end": 250.0}], axis=axis)
    expected = (0.0, -50.0, 100.0, 1250.0) if axis == "y" else (-50.0, 0.0, 1250.0, 100.0)
    assert row["anchored_bounds_unclipped"] == expected
    assert row["bounds"] == expected
    assert row["fitted_bounds"] == tuple(map(float, fitted))
    assert row["hold"] == 250.0  # max of both ends when they differ
    assert "hold_start" not in row and "hold_end" not in row  # output keys unchanged


def test_single_explicit_hold_keeps_default_on_the_other_end():
    row = _one([{"bounds": (0, 0, 100, 1000), "class": 1, "hold_end": 0.0}], axis="y")
    assert row["anchored_bounds_unclipped"] == (0.0, -400.0, 100.0, 1000.0)
    row = _one([{"bounds": (0, 0, 100, 1000), "class": 1, "hold_start": 0, "hold_end": 0}], axis="y")
    assert row["anchored_bounds_unclipped"] == (0.0, 0.0, 100.0, 1000.0)
    assert row["hold"] == 0.0


def test_field_clipping_still_applies_to_asymmetric_holds():
    field = box(0, -100, 100, 1100)
    row = _one([{"bounds": (0, 0, 100, 1000), "class": 1, "hold_start": 50.0, "hold_end": 250.0}],
               axis="y", field=field)
    assert row["anchored_bounds_unclipped"] == (0.0, -50.0, 100.0, 1250.0)
    assert row["bounds"] == (0.0, -50.0, 100.0, 1100.0)


def test_negative_hold_is_rejected():
    with pytest.raises(ValueError):
        _one([{"bounds": (0, 0, 100, 1000), "class": 1, "hold_start": -1.0}], axis="y")
