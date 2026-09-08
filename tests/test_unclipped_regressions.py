from copy import deepcopy
from math import pi

import pytest
from shapely.geometry import box

from A101.axis_orientation import add_box_anchorage
from A101.rebar_field_layout import layout_rebars
from rebar_service.pipeline import augment_layout_mass_metrics, PipelineJob


def test_unclassified_explicit_zone_is_included_in_all_mass_totals():
    layout = layout_rebars([box(0, 0, 1000, 2000)], [(100, 200, 900, 1800, 20, 150)], (16, 300))
    assert layout["is_feasible"]
    result = augment_layout_mass_metrics(layout)
    m = result["mass_metrics"]
    assert m["with_anchorage_unclipped_kg"] >= m["with_anchorage_kg"] - 1e-9
    assert m["additional"]["with_anchorage_kg"] > 0
    assert m["background_clipped_kg"] + m["additional"]["with_anchorage_kg"] == pytest.approx(m["with_anchorage_kg"])


def test_unclipped_keeps_separate_tracks_even_at_the_same_transverse_coordinate():
    # Two physical tracks, not two pieces of ONE track. Keep their multiplicity.
    layout = {
        "axis": "y",
        "tracks": [{"id": 1, "x": 150.0}, {"id": 2, "x": 150.0}],
        "bars": [
            {"track_id": 1, "zone_id": 0, "background": False, "diameter": 20, "x0": 150, "x1": 150, "y0": 0, "y1": 400},
            {"track_id": 2, "zone_id": 0, "background": False, "diameter": 20, "x0": 150, "x1": 150, "y0": 600, "y1": 1000},
        ],
        "zones": [{"id": 0, "class": 1, "diameter": 20, "step": 150, "background": False,
                   "track_ids": [1, 2], "bounds": (100, 0, 200, 1000), "fitted_bounds": (100, 0, 200, 1000),
                   "hold": 100, "anchored_bounds_unclipped": (100, -100, 200, 1100),
                   "bars": [(150, 0, 150, 400), (150, 600, 150, 1000)]}],
    }
    result = augment_layout_mass_metrics(layout)
    bars = result["zones"][0]["bars_with_anchorage_unclipped"]
    assert len(bars) == 2
    expected = 2 * 1.2 * (pi * 0.020 ** 2 / 4) * 7850
    assert result["mass_metrics"]["with_anchorage_unclipped_kg"] == pytest.approx(expected)


@pytest.mark.parametrize("axis", ["x", "y"])
@pytest.mark.parametrize("offset", [-3500, 0, 7500])
@pytest.mark.parametrize("holes", [False, True])
def test_metrics_follow_same_physical_layout_on_both_axes(axis, offset, holes):
    field = box(offset, 200, offset + 2400, 3500)
    if holes:
        field = field.difference(box(offset + 600, 900, offset + 1500, 1800))
    boxes = add_box_anchorage(
        [(offset + 100, 450, offset + 2300, 3100, 1)],
        recipes=None, diameters={1: 20}, steps={1: 150}, anchor_factor=40, axis=axis, field=field,
    )
    layout = layout_rebars([field], boxes, (16, 300), axis=axis)
    assert layout["is_feasible"]
    before = deepcopy(layout)
    result = augment_layout_mass_metrics(layout)
    m = result["mass_metrics"]
    assert m["with_anchorage_unclipped_kg"] + 1e-8 >= max(m["with_anchorage_kg"], m["without_anchorage_unclipped_kg"])
    assert m["with_anchorage_kg"] + 1e-8 >= m["without_anchorage_kg"]
    assert m["without_anchorage_unclipped_kg"] + 1e-8 >= m["without_anchorage_kg"]
    for key in ("with_anchorage_kg", "without_anchorage_kg", "with_anchorage_unclipped_kg", "without_anchorage_unclipped_kg"):
        assert m[key] == pytest.approx(m["background_clipped_kg"] + m["additional"][key])
    assert result["bars"] == before["bars"]
    assert layout == before
    assert m["schema_version"] == 2


def test_different_candidates_at_same_n_must_not_be_deduplicated():
    common = {"total_n": 5, "source": "components", "variant": "raw", "overlay_id": 0}
    a = PipelineJob("layout_solution", "t", {**common, "candidate_id": "a"})
    b = PipelineJob("layout_solution", "t", {**common, "candidate_id": "b"})
    assert a.job_id != b.job_id


def test_layout_refresh_token_is_part_of_job_identity():
    common = {"total_n": 5, "source": "components", "variant": "raw", "overlay_id": 0, "candidate_id": "a"}
    a = PipelineJob("layout_solution", "t", {**common, "layout_refresh": "r1"})
    b = PipelineJob("layout_solution", "t", {**common, "layout_refresh": "r2"})
    assert a.job_id != b.job_id


def test_metrics_rebuild_unclipped_anchorage_from_fitted_bounds_and_factor():
    field = box(0, 0, 1800, 2300)
    boxes = add_box_anchorage([(100, 500, 1700, 2100, 1)], recipes={}, diameters={1: 20},
                              steps={1: 150}, anchor_factor=40, axis="x", field=field)
    layout = layout_rebars([field], boxes, (16, 300), axis="x")
    assert layout["is_feasible"]
    zone = next(z for z in layout["zones"] if not z["background"])
    zone["anchored_bounds_unclipped"] = (500, -700, 2100, 2500)  # legacy axis metadata
    result = augment_layout_mass_metrics(layout, anchor_factor=40)
    result_zone = next(z for z in result["zones"] if not z["background"])
    b = result_zone["final_rectangle_with_anchorage_unclipped"]
    assert b[0] == -700
    assert b[2] == 2500
    assert result["mass_metrics"]["with_anchorage_unclipped_kg"] >= result["mass_metrics"]["with_anchorage_kg"]


def test_hole_splits_one_track_but_unclipped_counts_that_track_once():
    field = box(0, 0, 1800, 2600).difference(box(500, 900, 1300, 1700))
    boxes = add_box_anchorage([(100, 300, 1700, 2300, 1)], recipes={}, diameters={1: 20},
                             steps={1: 150}, anchor_factor=40, field=field)
    layout = layout_rebars([field], boxes, (16, 300))
    assert layout["is_feasible"]
    enriched = augment_layout_mass_metrics(layout)
    for zone in enriched["zones"]:
        if not zone["background"]:
            assert len(zone["bars_with_anchorage_unclipped"]) == len(set(zone["track_ids"]))


def test_missing_physical_track_identity_is_not_silently_guessed():
    from rebar_service.layout_metrics import LayoutMetricError
    layout = layout_rebars([box(0, 0, 1000, 2000)], [(100, 200, 900, 1800, 20, 150)], (16, 300))
    layout["tracks"] = []
    for zone in layout["zones"]:
        zone["track_ids"] = []
    for bar in layout["bars"]:
        bar.pop("track_id", None)
    with pytest.raises(LayoutMetricError, match="track"):
        augment_layout_mass_metrics(layout)


def test_compat_result_keeps_unclassified_physical_zone_and_its_mass():
    from rebar_service.pipeline import to_compat_result
    raw = layout_rebars([box(0, 0, 1000, 2000)], [(100, 200, 900, 1800, 20, 150)], (16, 300))
    layout = augment_layout_mass_metrics(raw)
    compat = to_compat_result({"is_feasible": True, "bar_layout": layout,
                               "actual_mass_kg": layout["mass_metrics"]["with_anchorage_kg"]})
    expected = [zone for zone in layout["zones"] if not zone.get("background")]
    assert len(compat["summary"]["zones"]) == len(expected)
    assert compat["summary"]["zones"][0]["class"] is None
    assert compat["summary"]["zones"][0]["diameter"] == 20
    assert sum(zone["zone mass with anchorage"] for zone in compat["summary"]["zones"]) == pytest.approx(
        layout["mass_metrics"]["additional"]["with_anchorage_kg"]
    )
