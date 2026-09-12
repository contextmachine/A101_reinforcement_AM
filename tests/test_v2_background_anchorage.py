from copy import deepcopy
from math import hypot, pi

import pytest
from shapely.geometry import Polygon, box

from rebar_service.config import Settings
from rebar_service.v2_layout import (
    build_v2_bar_result,
    reinforcement_by_polygon,
    v2_mass_metrics,
)
from rebar_service.v2_pipeline import build_v2_bars_for_context, build_v2_bars_for_request


DENSITY = 7850.0
BG_ZONES = [{"id": 0, "kind": "bg", "arm": {"d": 10.0, "step": 200.0}}]


def _background_bar(start, end, diameter=10.0):
    return {
        "background": True, "diameter": diameter,
        "x0": start[0], "y0": start[1], "x1": end[0], "y1": end[1],
    }


def _mass(length_mm, diameter_mm, density=DENSITY):
    # Volume in mm^3 -> m^3; independent reference calculation.
    return length_mm * pi * diameter_mm**2 / 4.0 * 1e-9 * density


def _expected_background(bars, factor, density=DENSITY):
    return sum(
        _mass(
            hypot(bar["x1"] - bar["x0"], bar["y1"] - bar["y0"])
            + 2.0 * factor * bar["diameter"],
            bar["diameter"], density,
        )
        for bar in bars if bar["background"]
    )


def test_default_factor_adds_background_anchorage_to_total_and_background_mass():
    bars = [_background_bar((0.0, 0.0), (0.0, 1000.0))]
    original = deepcopy(bars)
    metrics = v2_mass_metrics(bars, BG_ZONES, steel_density_kg_m3=DENSITY)
    visible = _mass(1000.0, 10.0)
    anchored = _mass(1000.0 + 2.0 * 40.0 * 10.0, 10.0)
    assert metrics["bg"]["with_anchorage_kg"] == pytest.approx(anchored)
    assert metrics["bg"]["with_anchorage_unclipped_kg"] == pytest.approx(anchored)
    assert metrics["bg"]["without_anchorage_kg"] == pytest.approx(visible)
    assert metrics["bg"]["without_anchorage_unclipped_kg"] == pytest.approx(visible)
    assert metrics["total_with_anchorage_kg"] == pytest.approx(anchored)
    assert metrics["total_without_anchorage_kg"] == pytest.approx(visible)
    assert metrics["background_kg"] == pytest.approx(anchored)
    assert bars == original


@pytest.mark.parametrize("factor", [0.0, 25.0, 40.0, 52.5])
def test_explicit_factor_is_applied_to_each_clipped_background_segment(factor):
    # Two pieces of a cut track: anchorage starts at each piece's own endpoints.
    bars = [
        _background_bar((0.0, 0.0), (0.0, 400.0)),
        _background_bar((0.0, 600.0), (0.0, 1000.0)),
        _background_bar((100.0, 0.0), (100.0, 1000.0), diameter=16.0),
    ]
    bars[0]["track_id"] = bars[1]["track_id"] = 7
    metrics = v2_mass_metrics(
        bars, BG_ZONES, steel_density_kg_m3=DENSITY, anchor_factor=factor,
    )
    assert metrics["background_kg"] == pytest.approx(_expected_background(bars, factor))
    assert metrics["bg"]["without_anchorage_kg"] == pytest.approx(_expected_background(bars, 0.0))


def test_background_anchorage_does_not_replace_or_double_count_additional_anchorage():
    zones = deepcopy(BG_ZONES) + [{
        "id": 1, "kind": "additional", "arm": {"d": 20.0, "step": 150.0},
        "origin": [0.0, 0.0], "direction": [1.0, 0.0], "length": 1000.0,
        "left": 0, "right": 0, "start_anchorage": 200.0, "end_anchorage": 300.0,
    }]
    bars = [
        _background_bar((0.0, 0.0), (0.0, 1000.0)),
        {"background": False, "input_zone_index": 0, "diameter": 20.0,
         "x0": 100.0, "y0": 0.0, "x1": 100.0, "y1": 600.0},
    ]
    metrics = v2_mass_metrics(bars, zones, steel_density_kg_m3=DENSITY, anchor_factor=25.0)
    expected_bg = _mass(1500.0, 10.0)
    expected_add = _mass(1100.0, 20.0)
    assert metrics["total_with_anchorage_kg"] == pytest.approx(expected_bg + expected_add)
    assert metrics["additional"]["with_anchorage_kg"] == pytest.approx(expected_add)
    assert metrics["total_without_anchorage_kg"] == pytest.approx(_mass(1000.0, 10.0) + _mass(600.0, 20.0))
    without_bg_anchor = v2_mass_metrics(bars, zones, steel_density_kg_m3=DENSITY, anchor_factor=0.0)
    assert metrics["additional"] == without_bg_anchor["additional"]


@pytest.mark.parametrize("factor", [-1.0, float("nan"), float("inf")])
def test_invalid_background_anchor_factor_is_rejected(factor):
    with pytest.raises(ValueError, match="anchor_factor"):
        v2_mass_metrics([], BG_ZONES, steel_density_kg_m3=DENSITY, anchor_factor=factor)


def test_empty_background_layout_has_no_phantom_anchorage_mass():
    metrics = v2_mass_metrics([], BG_ZONES, steel_density_kg_m3=DENSITY, anchor_factor=40.0)
    assert metrics["background_kg"] == 0.0
    assert metrics["total_with_anchorage_kg"] == 0.0
    assert all(value == 0.0 for value in metrics["bg"].values())


def test_real_layout_with_hole_keeps_geometry_and_verification_unchanged():
    field = Polygon(
        [(0, 0), (1000, 0), (1000, 1000), (0, 1000)],
        holes=[[(300, 300), (700, 300), (700, 700), (300, 700)]],
    )
    result_zero, raw_zero = build_v2_bar_result(
        [field], BG_ZONES, axis="y", min_step=100.0,
        steel_density_kg_m3=DENSITY, anchor_factor=0.0,
    )
    result, raw = build_v2_bar_result(
        [field], BG_ZONES, axis="y", min_step=100.0,
        steel_density_kg_m3=DENSITY, anchor_factor=40.0,
    )
    assert raw and any(hypot(b["x1"] - b["x0"], b["y1"] - b["y0"]) < 1000 for b in raw)
    assert raw == raw_zero
    assert result["bar_layout"] == result_zero["bar_layout"]
    assert result["mass_kg"] == pytest.approx(_expected_background(raw, 40.0))
    assert result["mass_bg_kg"] == pytest.approx(result["mass_kg"])
    assert result["mass_kg"] > result_zero["mass_kg"]
    polygon = {"geometry": field, "load": 5.0, "source_index": 0, "overlay_state": "active"}
    verification_kwargs = {"steel_density_kg_m3": DENSITY, "thickness_mm": 500.0}
    before = reinforcement_by_polygon([polygon], result_zero["bar_layout"]["bars"], **verification_kwargs)
    after = reinforcement_by_polygon([polygon], result["bar_layout"]["bars"], **verification_kwargs)
    assert before == after


@pytest.mark.parametrize("factor", [0.0, 25.0, 40.0])
def test_main_pipeline_uses_anchor_factor_from_saved_params(factor):
    field = box(0, 0, 1000, 1000)
    context = {
        "field": {"start_polygons": [{"geometry": field}]},
        "cfg": {"axis": "y"}, "params": {"anchor_factor": factor},
    }
    result = build_v2_bars_for_context(context, BG_ZONES, Settings(), steel_density_kg_m3=DENSITY)
    bars = result["bar_layout"]["bars"]
    expected = sum(_mass(hypot(b["end"][0] - b["start"][0], b["end"][1] - b["start"][1])
                         + 2.0 * factor * b["d"], b["d"]) for b in bars)
    assert bars
    assert result["mass_kg"] == pytest.approx(expected)
    assert result["mass_bg_kg"] == pytest.approx(expected)


@pytest.mark.parametrize("factor", [0.0, 25.0, 40.0])
def test_request_pipeline_uses_anchor_factor_and_density_from_request(factor):
    class SceneStore:
        def resolved_scene_polygons(self, scene_id, *, variant, overlay_id):
            return [{
                "source_index": 0, "load": 5.0, "overlay_state": "active",
                "points": [[0, 0], [1000, 0], [1000, 1000], [0, 1000]],
            }]

    density = 8000.0
    request = {
        "scene_id": "scene", "smooth": False, "overlay_id": 0, "zones": BG_ZONES,
        "config": {"axis": "y", "anchor_factor": factor, "steel_density_kg_m3": density},
    }
    result = build_v2_bars_for_request(SceneStore(), request, Settings())
    bars = result["bar_layout"]["bars"]
    expected = sum(_mass(hypot(b["end"][0] - b["start"][0], b["end"][1] - b["start"][1])
                         + 2.0 * factor * b["d"], b["d"], density) for b in bars)
    assert bars
    assert result["mass_kg"] == pytest.approx(expected)
    assert result["mass_bg_kg"] == pytest.approx(expected)
