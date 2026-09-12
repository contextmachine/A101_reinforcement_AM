from math import pi

import pytest
from shapely.geometry import box

import rebar_service.v2_layout as layout_mod
from rebar_service.v2_layout import reinforcement_by_polygon, v2_mass_metrics


def test_v2_mass_metrics_add_hidden_anchorage_without_changing_visible_bar_geometry():
    zones = [
        {"id": 0, "kind": "bg", "arm": {"d": 10.0, "step": 100.0}},
        {"id": 7, "kind": "additional", "arm": {"d": 20.0, "step": 150.0},
         "origin": [0, 0], "direction": [1, 0], "length": 1000.0,
         "left": 0, "right": 0, "start_anchorage": 200.0, "end_anchorage": 300.0},
    ]
    bars = [
        {"background": True, "diameter": 10.0, "x0": 0.0, "y0": 0.0, "x1": 0.0, "y1": 1000.0},
        {"background": False, "input_zone_index": 0, "diameter": 20.0,
         "x0": 100.0, "y0": 0.0, "x1": 100.0, "y1": 600.0},
    ]
    metrics = v2_mass_metrics(bars, zones, steel_density_kg_m3=7850.0)
    area20 = pi * (0.020 ** 2) / 4
    visible_add = 7850.0 * area20 * 0.6
    with_anchor = 7850.0 * area20 * 1.1
    assert metrics["additional"]["without_anchorage_kg"] == pytest.approx(visible_add)
    assert metrics["additional"]["with_anchorage_kg"] == pytest.approx(with_anchor)
    assert metrics["total_with_anchorage_kg"] > metrics["total_without_anchorage_kg"]
    # Source geometry is untouched; hidden anchorage is mass-only.
    assert bars[1]["y1"] == 600.0


def test_reinforcement_by_polygon_uses_visible_intersection_and_explicit_units():
    polygon = {
        "source_index": 3,
        "overlay_state": "active",
        "load": 5.0,
        "geometry": box(0, 0, 1000, 1000),  # 1 m2
    }
    # One d=10 bar crosses the full 1m polygon. Hidden anchorage is intentionally absent.
    bars = [{"zone_id": 1, "start": [500.0, -100.0], "end": [500.0, 1100.0], "d": 10.0}]
    rows = reinforcement_by_polygon([polygon], bars, steel_density_kg_m3=7850.0, thickness_mm=500.0)
    assert len(rows) == 1
    row = rows[0]
    # V/A = (1000 * pi*10^2/4) / 1_000_000 mm = pi/40 mm; cm2/m = 10 * V/A.
    expected_fact = 10.0 * (1000.0 * pi * 10.0**2 / 4.0) / 1_000_000.0
    assert row["fact_load_sm2/m"] == pytest.approx(expected_fact)
    assert row["need_load_sm2/m"] == 5.0
    assert row["fact_load_kg/m3"] == pytest.approx((expected_fact * 0.1 / 500.0) * 7850.0)
    assert row["need_load_kg/m3"] == pytest.approx((5.0 * 0.1 / 500.0) * 7850.0)


def test_layout_v2_zones_passes_only_non_anchored_zone_geometry_to_allocator(monkeypatch):
    seen = {}
    zones = [
        {"id": 0, "kind": "bg", "arm": {"d": 18.0, "step": 300.0}},
        {"id": 1, "kind": "additional", "arm": {"d": 20.0, "step": 150.0},
         "origin": [0.0, 0.0], "direction": [1.0, 0.0], "length": 1000.0,
         "left": 0, "right": 1, "start_anchorage": 800.0, "end_anchorage": 800.0},
    ]

    def fake_layout(*, polygons, boxes, background, axis, min_step):
        seen.update(boxes=boxes, background=background, axis=axis)
        return {"is_feasible": True, "bars": [], "zones": [], "tracks": [], "stats": {}}

    monkeypatch.setattr(layout_mod, "_layout_rebars", fake_layout)
    result = layout_mod.layout_v2_zones([box(0, 0, 2000, 2000)], zones, axis="y", min_step=100.0)
    assert result["is_feasible"] is True
    assert seen["background"] == (18.0, 300.0)
    assert len(seen["boxes"]) == 1
    b = seen["boxes"][0]
    assert b["geometry"].bounds == pytest.approx((-75.0, 0.0, 225.0, 1000.0))
    # start/end anchorage metadata does not alter the allocator geometry.
    assert b["geometry"].bounds[1] == 0.0
    assert b["geometry"].bounds[3] == 1000.0
