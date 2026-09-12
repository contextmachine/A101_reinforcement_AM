from __future__ import annotations

from math import pi

import pytest
from shapely.geometry import LineString, box
from shapely.ops import unary_union

from A101.axis_orientation import add_box_anchorage
from rebar_service.v2.bars import (
    MASS_KEYS,
    bars_from_layout,
    fitted_boxes_to_zones,
    layout_zones,
    physical_polygons,
    resolve_anchorage,
    zone_to_box,
    zones_from_layout,
)

DENSITY = 7850.0
BG = {"id": 0, "kind": "bg", "arm": {"d": 18, "step": 300}}


def unit(d: float) -> float:
    """kg per mm of a round bar."""
    return pi * (d / 1000.0) ** 2 / 4.0 * DENSITY / 1000.0


def additional_zone(**overrides):
    zone = {
        "id": 1, "kind": "additional", "arm": {"d": 20, "step": 150}, "left": 0, "right": 0,
        "length": 1800.0, "origin": [500.0, 600.0], "direction": [1.0, 0.0],
    }
    zone.update(overrides)
    return zone


def run(polygons, zones, *, axis="y", anchor_factor=40.0, min_step=100.0):
    return layout_zones(
        polygons, zones, axis=axis, anchor_factor=anchor_factor,
        steel_density_kg_m3=DENSITY, min_step=min_step,
    )


def bars_of(result, zone_ids):
    return [b for b in result["bars"] if b["zone_id"] in set(zone_ids)]


def bar_key(bar):
    return (bar["zone_id"], round(bar["start"][0], 6), round(bar["start"][1], 6),
            round(bar["end"][0], 6), round(bar["end"][1], 6), bar["d"],
            bar["anchorage"]["start"], bar["anchorage"]["end"])


def assert_bar_shape(bar, axis):
    (x0, y0), (x1, y1) = bar["start"], bar["end"]
    if axis == "y":
        assert x0 == x1 and y0 < y1
    else:
        assert y0 == y1 and x0 < x1


# ------------------------------------------------------------------------------------------
# conversions
# ------------------------------------------------------------------------------------------


def test_physical_polygons_drop_removed_rows_and_keep_real_ones():
    rows = [
        {"points": [[0, 0], [10, 0], [10, 10], [0, 10]], "overlay_state": "active"},
        {"points": [[10, 0], [20, 0], [20, 10], [10, 10]], "overlay_state": "background_only"},
        {"points": [[20, 0], [30, 0], [30, 10], [20, 10]], "overlay_state": "removed"},
        {"geometry": box(30, 0, 40, 10)},
    ]
    polygons = physical_polygons(rows)
    assert len(polygons) == 3
    assert sorted(p.bounds[0] for p in polygons) == [0.0, 10.0, 30.0]


def test_resolve_anchorage_defaults_to_anchor_factor_times_d():
    assert resolve_anchorage(additional_zone(), 40) == (800.0, 800.0)
    assert resolve_anchorage(BG, 40) == (720.0, 720.0)
    explicit = additional_zone(anchorage={"start": 100, "end": 300})
    assert resolve_anchorage(explicit, 40) == (100.0, 300.0)
    with pytest.raises(ValueError):
        resolve_anchorage(additional_zone(), -1)


def test_zone_to_box_axis_y_spans_tributary_width_and_length():
    zone = additional_zone(left=1, right=2, origin=[1175.0, 600.0], length=1800.0)
    box_row = zone_to_box(zone, axis="y")
    assert box_row == {"id": 1, "bounds": (950.0, 600.0, 1550.0, 2400.0), "diameter": 20.0, "step": 150.0}


def test_zone_to_box_axis_x_uses_downward_direction_convention():
    zone = additional_zone(left=0, right=1, origin=[600.0, 575.0], direction=[0.0, -1.0])
    box_row = zone_to_box(zone, axis="x")
    assert box_row["bounds"] == (600.0, 350.0, 2400.0, 650.0)
    # A reversed transverse direction flips the bar axis: the bar runs towards -x from origin.
    reversed_zone = additional_zone(origin=[2400.0, 500.0], direction=[0.0, 1.0])
    assert zone_to_box(reversed_zone, axis="x")["bounds"] == (600.0, 425.0, 2400.0, 575.0)


def test_zone_to_box_rejects_misaligned_direction_and_bg_zone():
    with pytest.raises(ValueError):
        zone_to_box(additional_zone(direction=[0.0, -1.0]), axis="y")
    with pytest.raises(ValueError):
        zone_to_box(additional_zone(direction=[0.6, 0.8]), axis="y")
    with pytest.raises(ValueError):
        zone_to_box(BG, axis="y")


# ------------------------------------------------------------------------------------------
# layout_zones, axis y
# ------------------------------------------------------------------------------------------


def test_layout_axis_y_bg_and_additional_bars():
    field = box(0, 0, 3100, 3000)
    zone = additional_zone(left=1, right=2, origin=[1175.0, 600.0])
    result = run([field], [BG, zone])
    assert result["is_feasible"] and result["status"] == "feasible"
    assert result["warnings"] == [] and result["errors"] == []

    bg_bars = bars_of(result, [0])
    assert len(bg_bars) == 11
    assert sorted(b["start"][0] for b in bg_bars) == [50.0 + 300.0 * k for k in range(11)]
    for bar in result["bars"]:
        assert_bar_shape(bar, "y")
        assert 0.0 <= bar["start"][1] and bar["end"][1] <= 3000.0
    assert all(b["d"] == 18.0 and b["anchorage"] == {"start": 720.0, "end": 720.0} for b in bg_bars)

    zone_ids = {z["id"] for z in result["zones"]}
    add_bars = [b for b in result["bars"] if b["zone_id"] != 0]
    assert len(add_bars) == 4
    assert {b["zone_id"] for b in add_bars} <= zone_ids
    assert all(b["d"] == 20.0 and b["anchorage"] == {"start": 800.0, "end": 800.0} for b in add_bars)
    assert all(b["start"][1] == 600.0 and b["end"][1] == 2400.0 for b in add_bars)

    # Output zones: bg keeps its id, the first run keeps the input id, extra runs get max+1...
    assert result["zones"][0] == {
        "id": 0, "kind": "bg", "arm": {"d": 18.0, "step": 300.0}, "anchorage": {"start": 720.0, "end": 720.0},
    }
    additional = [z for z in result["zones"] if z["kind"] == "additional"]
    assert additional[0]["id"] == 1
    assert [z["id"] for z in additional] == list(range(1, len(additional) + 1))
    assert sum(z["left"] + z["right"] + 1 for z in additional) == 4
    for z in additional:
        assert z["arm"] == {"d": 20.0, "step": 150.0}
        assert z["length"] == 1800.0 and z["direction"] == [1.0, 0.0] and z["origin"][1] == 600.0
        assert z["anchorage"] == {"start": 800.0, "end": 800.0}
    # Every bar sits on its zone's arithmetic grid.
    by_id = {z["id"]: z for z in additional}
    for bar in add_bars:
        z = by_id[bar["zone_id"]]
        offset = (bar["start"][0] - z["origin"][0]) / z["arm"]["step"]
        assert abs(offset - round(offset)) <= 0.35 + 1e-9 and -z["left"] <= round(offset) <= z["right"]

    metrics = result["mass_metrics"]
    assert set(metrics) == {"additional", "bg"} and all(set(metrics[g]) == set(MASS_KEYS) for g in metrics)
    assert metrics["bg"]["without_anchorage_kg"] == pytest.approx(11 * 3000 * unit(18))
    assert metrics["bg"]["with_anchorage_kg"] == pytest.approx(11 * (3000 + 1440) * unit(18))
    bg = metrics["bg"]
    assert bg["without_anchorage_unclipped_kg"] == pytest.approx(bg["without_anchorage_kg"])
    assert metrics["bg"]["with_anchorage_unclipped_kg"] == pytest.approx(metrics["bg"]["with_anchorage_kg"])
    assert metrics["additional"]["without_anchorage_kg"] == pytest.approx(4 * 1800 * unit(20))
    assert metrics["additional"]["with_anchorage_kg"] == pytest.approx(4 * (1800 + 1600) * unit(20))
    assert metrics["additional"]["without_anchorage_unclipped_kg"] == pytest.approx(4 * 1800 * unit(20))
    assert metrics["additional"]["with_anchorage_unclipped_kg"] == pytest.approx(4 * 3400 * unit(20))


def test_hole_splits_track_into_two_bars_but_unclipped_counts_one_rod():
    field = box(0, 0, 1000, 3000).difference(box(450, 1200, 550, 1800))
    result = run([field], [BG, additional_zone()])
    assert result["is_feasible"]

    add_bars = bars_of(result, [1])
    assert sorted((b["start"][1], b["end"][1]) for b in add_bars) == [(600.0, 1200.0), (1800.0, 2400.0)]
    assert all(b["start"][0] == 500.0 and b["anchorage"] == {"start": 800.0, "end": 800.0} for b in add_bars)
    # Background tracks are not touched by this hole.
    assert len(bars_of(result, [0])) == 4

    add = result["mass_metrics"]["additional"]
    assert add["without_anchorage_kg"] == pytest.approx(1200 * unit(20))
    assert add["with_anchorage_kg"] == pytest.approx((1200 + 2 * 1600) * unit(20))
    assert add["without_anchorage_unclipped_kg"] == pytest.approx(1800 * unit(20))
    assert add["with_anchorage_unclipped_kg"] == pytest.approx((1800 + 1600) * unit(20))

    zones = [z for z in result["zones"] if z["kind"] == "additional"]
    assert len(zones) == 1
    assert zones[0]["id"] == 1 and zones[0]["origin"] == [500.0, 600.0] and zones[0]["length"] == 1800.0
    assert zones[0]["left"] == 0 and zones[0]["right"] == 0


def test_background_mass_group_with_hole_counts_component_extent_unclipped():
    field = box(0, 0, 1000, 3000).difference(box(300, 1200, 700, 1800))
    result = run([field], [BG])
    assert result["is_feasible"]
    assert result["zones"] == [
        BG | {"arm": {"d": 18.0, "step": 300.0}, "anchorage": {"start": 720.0, "end": 720.0}},
    ]
    bg_bars = result["bars"]
    assert len(bg_bars) == 6 and all(b["zone_id"] == 0 for b in bg_bars)
    visible = 2 * 3000 + 4 * 1200
    bg = result["mass_metrics"]["bg"]
    assert bg["without_anchorage_kg"] == pytest.approx(visible * unit(18))
    assert bg["with_anchorage_kg"] == pytest.approx((visible + 6 * 1440) * unit(18))
    assert bg["without_anchorage_unclipped_kg"] == pytest.approx(4 * 3000 * unit(18))
    assert bg["with_anchorage_unclipped_kg"] == pytest.approx(4 * (3000 + 1440) * unit(18))
    assert all(v == 0.0 for v in result["mass_metrics"]["additional"].values())


def test_bars_clipped_at_field_boundary_keep_full_anchorage():
    field = box(0, 0, 1000, 3000)
    zone = additional_zone(length=3000.0, origin=[500.0, -500.0])
    result = run([field], [BG, zone])
    assert result["is_feasible"]
    add_bars = bars_of(result, [1])
    assert len(add_bars) == 1
    assert add_bars[0]["start"] == [500.0, 0.0] and add_bars[0]["end"] == [500.0, 2500.0]
    assert add_bars[0]["anchorage"] == {"start": 800.0, "end": 800.0}
    add = result["mass_metrics"]["additional"]
    assert add["without_anchorage_kg"] == pytest.approx(2500 * unit(20))
    assert add["with_anchorage_kg"] == pytest.approx((2500 + 1600) * unit(20))
    assert add["without_anchorage_unclipped_kg"] == pytest.approx(3000 * unit(20))
    assert add["with_anchorage_unclipped_kg"] == pytest.approx((3000 + 1600) * unit(20))
    # The zone keeps its unclipped geometry (no anchorage in it).
    zone_out = next(z for z in result["zones"] if z["id"] == 1)
    assert zone_out["origin"] == [500.0, -500.0] and zone_out["length"] == 3000.0


def stepped_edge_field():
    """Field whose bounding box reaches x=4000 but which stops at x=2950 above y=500."""
    return unary_union([box(0, 0, 2950, 3000), box(2950, 0, 4000, 500)])


def edge_zone(**overrides):
    """One-bar zone whose single bar sits on the guide x=2950, at the field edge."""
    defaults = {"arm": {"d": 36, "step": 100}, "length": 1000.0, "origin": [2950.0, 1000.0]}
    return additional_zone(**{**defaults, **overrides})


def test_edge_guide_bar_moved_by_clearance_stays_inside_the_field():
    # Two one-bar zones snap to the same guide at the field edge; the clearance between
    # them used to push the outer bar to x=2968, where the track clips to nothing
    # ("empty_track" -> partial layout), because its allowed window was the component
    # bounding box, which is 1050 mm wider than the field over this band.
    field = stepped_edge_field()
    result = run([field], [BG, edge_zone(id=1), edge_zone(id=2)])
    assert result["is_feasible"] and result["status"] == "feasible"
    assert result["warnings"] == [] and result["errors"] == []

    add_bars = sorted(bars_of(result, [1, 2]), key=lambda b: b["start"][0])
    assert len(add_bars) == 2
    xs = [b["start"][0] for b in add_bars]
    assert xs[1] <= 2950.0 and xs[1] - xs[0] >= 36.0 - 1e-9
    for bar in add_bars:
        assert bar["start"][1] == 1000.0 and bar["end"][1] == 2000.0
        assert field.covers(LineString([bar["start"], bar["end"]]))


def test_edge_guide_bars_still_spread_symmetrically_where_the_field_is_wide():
    # The same guide, but on the band where the field really does reach past it: the clamp is
    # band-local, so the pair keeps the symmetric +-18 mm spread around the guide.
    field = stepped_edge_field()
    zones = [BG] + [edge_zone(id=i, length=300.0, origin=[2950.0, 100.0]) for i in (1, 2)]
    result = run([field], zones)
    assert result["is_feasible"] and result["warnings"] == [] and result["errors"] == []
    xs = sorted(b["start"][0] for b in bars_of(result, [1, 2]))
    assert xs == [2932.0, 2968.0]


def test_explicit_zone_anchorage_overrides_anchor_factor():
    field = box(0, 0, 1000, 3000)
    zone = additional_zone(anchorage={"start": 100.0, "end": 300.0})
    bg = dict(BG, anchorage={"start": 50.0, "end": 60.0})
    result = run([field], [bg, zone], anchor_factor=40)
    assert result["is_feasible"]
    add_bar = bars_of(result, [1])[0]
    assert add_bar["anchorage"] == {"start": 100.0, "end": 300.0}
    assert all(b["anchorage"] == {"start": 50.0, "end": 60.0} for b in bars_of(result, [0]))
    add = result["mass_metrics"]["additional"]
    assert add["with_anchorage_kg"] == pytest.approx((1800 + 400) * unit(20))
    assert add["with_anchorage_unclipped_kg"] == pytest.approx((1800 + 400) * unit(20))
    bg_metrics = result["mass_metrics"]["bg"]
    assert bg_metrics["with_anchorage_kg"] == pytest.approx(4 * (3000 + 110) * unit(18))
    zone_out = next(z for z in result["zones"] if z["id"] == 1)
    assert zone_out["anchorage"] == {"start": 100.0, "end": 300.0}
    assert result["zones"][0]["anchorage"] == {"start": 50.0, "end": 60.0}


def test_reversed_direction_maps_start_anchorage_to_upper_bar_end():
    field = box(0, 0, 1000, 3000)
    # direction (-1, 0): bar axis points -y, the zone starts at y=2400 and runs down to 600.
    zone = additional_zone(
        origin=[500.0, 2400.0], direction=[-1.0, 0.0], anchorage={"start": 100.0, "end": 300.0},
    )
    assert zone_to_box(zone, axis="y")["bounds"] == (425.0, 600.0, 575.0, 2400.0)
    result = run([field], [BG, zone])
    assert result["is_feasible"]
    bar = bars_of(result, [1])[0]
    assert bar["start"] == [500.0, 600.0] and bar["end"] == [500.0, 2400.0]
    assert bar["anchorage"] == {"start": 300.0, "end": 100.0}
    zone_out = next(z for z in result["zones"] if z["id"] == 1)
    assert zone_out["direction"] == [-1.0, 0.0] and zone_out["origin"] == [500.0, 2400.0]
    assert zone_out["anchorage"] == {"start": 100.0, "end": 300.0}


def test_round_trip_of_layout_zones_is_stable():
    field = box(0, 0, 3100, 3000)
    first = run([field], [BG, additional_zone(left=1, right=2, origin=[1175.0, 600.0])])
    assert first["is_feasible"]
    second = run([field], first["zones"])
    assert second["is_feasible"]
    assert second["zones"] == first["zones"]
    assert sorted(map(bar_key, second["bars"])) == sorted(map(bar_key, first["bars"]))
    assert second["mass_metrics"] == first["mass_metrics"]


def test_zones_and_bars_helpers_match_layout_zones():
    field = box(0, 0, 3100, 3000)
    zones = [BG, additional_zone(left=1, right=2, origin=[1175.0, 600.0])]
    result = run([field], zones)
    assert zones_from_layout(result["layout"], zones, axis="y", anchor_factor=40) == result["zones"]
    assert bars_from_layout(result["layout"], zones, axis="y", anchor_factor=40) == result["bars"]


# ------------------------------------------------------------------------------------------
# axis x
# ------------------------------------------------------------------------------------------


def test_layout_axis_x_horizontal_bars_and_zone_conventions():
    field = box(0, 0, 3000, 1000)
    zone = additional_zone(left=0, right=1, origin=[600.0, 575.0], direction=[0.0, -1.0])
    result = run([field], [BG, zone], axis="x")
    assert result["is_feasible"]
    for bar in result["bars"]:
        assert_bar_shape(bar, "x")
    bg_bars = bars_of(result, [0])
    assert sorted(b["start"][1] for b in bg_bars) == [50.0, 350.0, 650.0, 950.0]
    assert all(b["start"][0] == 0.0 and b["end"][0] == 3000.0 for b in bg_bars)
    add_bars = [b for b in result["bars"] if b["zone_id"] != 0]
    assert len(add_bars) == 2
    assert all(b["start"][0] == 600.0 and b["end"][0] == 2400.0 for b in add_bars)
    assert all(350.0 < b["start"][1] < 650.0 for b in add_bars)
    additional = [z for z in result["zones"] if z["kind"] == "additional"]
    assert additional[0]["id"] == 1
    assert sum(z["left"] + z["right"] + 1 for z in additional) == 2
    for z in additional:
        assert z["direction"] == [0.0, -1.0] and z["origin"][0] == 600.0 and z["length"] == 1800.0
    by_id = {z["id"]: z for z in additional}
    for bar in add_bars:
        z = by_id[bar["zone_id"]]
        offset = (z["origin"][1] - bar["start"][1]) / z["arm"]["step"]
        assert abs(offset - round(offset)) <= 0.35 + 1e-9 and -z["left"] <= round(offset) <= z["right"]
    bg = result["mass_metrics"]["bg"]
    assert bg["without_anchorage_kg"] == pytest.approx(4 * 3000 * unit(18))
    assert bg["with_anchorage_unclipped_kg"] == pytest.approx(4 * (3000 + 1440) * unit(18))
    add = result["mass_metrics"]["additional"]
    assert add["without_anchorage_kg"] == pytest.approx(2 * 1800 * unit(20))
    assert add["with_anchorage_kg"] == pytest.approx(2 * (1800 + 1600) * unit(20))

    again = run([field], result["zones"], axis="x")
    assert again["zones"] == result["zones"]
    assert sorted(map(bar_key, again["bars"])) == sorted(map(bar_key, result["bars"]))


def test_axis_x_hole_and_boundary_clipping():
    field = box(0, 0, 3000, 1000).difference(box(1200, 450, 1800, 550))
    zone = additional_zone(origin=[-500.0, 500.0], direction=[0.0, -1.0], length=3500.0)
    result = run([field], [BG, zone], axis="x")
    assert result["is_feasible"]
    add_bars = bars_of(result, [1])
    assert sorted((b["start"][0], b["end"][0]) for b in add_bars) == [(0.0, 1200.0), (1800.0, 3000.0)]
    assert all(b["start"][1] == 500.0 == b["end"][1] for b in add_bars)
    add = result["mass_metrics"]["additional"]
    assert add["without_anchorage_kg"] == pytest.approx(2400 * unit(20))
    assert add["with_anchorage_kg"] == pytest.approx((2400 + 2 * 1600) * unit(20))
    assert add["without_anchorage_unclipped_kg"] == pytest.approx(3500 * unit(20))
    assert add["with_anchorage_unclipped_kg"] == pytest.approx((3500 + 1600) * unit(20))


# ------------------------------------------------------------------------------------------
# infeasible / validation
# ------------------------------------------------------------------------------------------


def test_zone_outside_field_yields_infeasible_result_with_status():
    field = box(0, 0, 1000, 3000)
    result = run([field], [BG, additional_zone(origin=[5000.0, 600.0])])
    assert result["is_feasible"] is False
    assert result["status"] == "partial"
    assert result["bars"] == [] and result["zones"] == []
    assert result["mass_metrics"] == {g: {k: 0.0 for k in MASS_KEYS} for g in ("additional", "bg")}
    assert any(w.get("type") == "zone_without_bars" for w in result["warnings"])
    assert "tracks" in result["layout"]


def test_layout_zones_validates_zone_collection():
    field = box(0, 0, 1000, 3000)
    with pytest.raises(ValueError):
        run([field], [additional_zone()])
    with pytest.raises(ValueError):
        run([field], [BG, dict(BG, id=5)])
    with pytest.raises(ValueError):
        run([field], [BG, additional_zone(id=0)])
    with pytest.raises(ValueError):
        run([], [BG])


# ------------------------------------------------------------------------------------------
# fitted_boxes_to_zones (phase B input)
# ------------------------------------------------------------------------------------------


def test_fitted_boxes_to_zones_axis_y_from_add_box_anchorage():
    rows = add_box_anchorage(
        [(950, 600, 1550, 2400, 1)], recipes=None, diameters={1: 20}, steps={1: 150},
        anchor_factor=40, axis="y", field=box(0, 0, 3100, 3000),
    )
    assert rows[0]["fitted_bounds"] == (950.0, 600.0, 1550.0, 2400.0)
    zones = fitted_boxes_to_zones(rows, axis="y", anchor_factor=40, bg={"d": 18, "step": 300})
    assert zones[0] == {
        "id": 0, "kind": "bg", "arm": {"d": 18.0, "step": 300.0}, "anchorage": {"start": 720.0, "end": 720.0},
    }
    assert zones[1] == {
        "id": 1, "kind": "additional", "arm": {"d": 20.0, "step": 150.0}, "left": 0, "right": 3,
        "length": 1800.0, "anchorage": {"start": 800.0, "end": 800.0},
        "origin": [1025.0, 600.0], "direction": [1.0, 0.0],
    }
    # The wire zone reproduces the fitted (unanchored) box exactly.
    assert zone_to_box(zones[1], axis="y")["bounds"] == (950.0, 600.0, 1550.0, 2400.0)
    result = run([box(0, 0, 3100, 3000)], zones)
    assert result["is_feasible"]
    assert len([b for b in result["bars"] if b["zone_id"] != 0]) == 4


def test_fitted_boxes_to_zones_axis_x_and_explicit_bg_anchorage():
    rows = add_box_anchorage(
        [(600, 425, 2400, 575, 1)], recipes=None, diameters={1: 20}, steps={1: 150},
        anchor_factor=40, axis="x", field=box(0, 0, 3000, 1000),
    )
    zones = fitted_boxes_to_zones(
        rows, axis="x", anchor_factor=40, bg={"d": 18, "step": 300}, bg_anchorage={"start": 10, "end": 20},
    )
    assert zones[0]["anchorage"] == {"start": 10.0, "end": 20.0}
    assert zones[1]["origin"] == [600.0, 500.0] and zones[1]["direction"] == [0.0, -1.0]
    assert zones[1]["left"] == 0 and zones[1]["right"] == 0 and zones[1]["length"] == 1800.0
    assert zone_to_box(zones[1], axis="x")["bounds"] == (600.0, 425.0, 2400.0, 575.0)
    result = run([box(0, 0, 3000, 1000)], zones, axis="x")
    assert result["is_feasible"]
    add_bars = bars_of(result, [1])
    assert len(add_bars) == 1
    assert add_bars[0]["start"] == [600.0, 500.0] and add_bars[0]["end"] == [2400.0, 500.0]


def test_fitted_boxes_to_zones_accepts_plain_fit_rows_and_holds():
    rows = [
        {"bounds": (0, 0, 400, 1000), "d": 16, "step": 100, "hold_start": 100, "hold_end": 200},
        {"fitted_bounds": (1000, 0, 1250, 1000), "diameter": 20, "step": 100, "hold": 640},
    ]
    zones = fitted_boxes_to_zones(rows, axis="y", anchor_factor=40, bg={"d": 18, "step": 300})
    assert [z["id"] for z in zones] == [0, 1, 2]
    assert zones[1]["right"] == 3 and zones[1]["origin"] == [50.0, 0.0]
    assert zones[1]["anchorage"] == {"start": 100.0, "end": 200.0}
    # Width 250 with step 100 -> 3 bars centred in the box.
    assert zones[2]["right"] == 2 and zones[2]["origin"] == [1025.0, 0.0]
    assert zones[2]["anchorage"] == {"start": 640.0, "end": 640.0}


def test_tracks_split_by_a_hole_at_one_coordinate_form_one_zone_member_and_one_rod():
    """Two layout tracks of one zone at the same x (a hole between them) are one rod, not two."""
    from rebar_service.v2.bars import bars_from_layout, mass_metrics_from_layout, zones_from_layout

    zones = [
        {"id": 0, "kind": "bg", "arm": {"d": 16.0, "step": 300.0}},
        {"id": 5, "kind": "additional", "arm": {"d": 20.0, "step": 150.0}, "left": 0, "right": 1,
         "length": 3000.0, "origin": [450.0, 0.0], "direction": [1.0, 0.0]},
    ]
    layout = {
        "is_feasible": True, "status": "Feasible", "axis": "y",
        "components": [{"id": 0, "bounds": (0.0, 0.0, 1200.0, 3000.0)}],
        "zones": [{"id": 0, "background": True, "component_id": 0, "bounds": (0.0, 0.0, 1200.0, 3000.0),
                   "parts": [{"exterior": [[0, 0], [1200, 0], [1200, 3000], [0, 3000], [0, 0]], "holes": []}]}],
        "tracks": [
            {"id": 0, "background": False, "input_zone_index": 0, "component_id": 0, "x": 450.0},
            {"id": 1, "background": False, "input_zone_index": 0, "component_id": 0, "x": 450.0},
            {"id": 2, "background": False, "input_zone_index": 0, "component_id": 0, "x": 600.0},
        ],
        "bars": [
            {"id": 0, "track_id": 0, "background": False, "diameter": 20.0, "x0": 450.0, "y0": 0.0, "x1": 450.0, "y1": 1000.0},
            {"id": 1, "track_id": 1, "background": False, "diameter": 20.0, "x0": 450.0, "y0": 2000.0, "x1": 450.0, "y1": 3000.0},
            {"id": 2, "track_id": 2, "background": False, "diameter": 20.0, "x0": 600.0, "y0": 0.0, "x1": 600.0, "y1": 3000.0},
        ],
    }
    out_zones = zones_from_layout(layout, zones, axis="y", anchor_factor=40.0)
    additional = [z for z in out_zones if z["kind"] == "additional"]
    assert len(additional) == 1
    assert additional[0]["id"] == 5 and additional[0]["left"] + additional[0]["right"] + 1 == 2
    bars = bars_from_layout(layout, zones, axis="y", anchor_factor=40.0)
    assert len(bars) == 3 and {bar["zone_id"] for bar in bars} == {5}
    masses = mass_metrics_from_layout(layout, zones, axis="y", anchor_factor=40.0, steel_density_kg_m3=7850.0)
    unit = 7850.0 * 3.141592653589793 * (20.0 / 1000.0) ** 2 / 4 / 1000.0  # kg per mm
    assert masses["additional"]["without_anchorage_unclipped_kg"] == pytest.approx(unit * 2 * 3000.0)
    assert masses["additional"]["without_anchorage_kg"] == pytest.approx(unit * (1000.0 + 1000.0 + 3000.0))
    assert masses["additional"]["with_anchorage_kg"] == pytest.approx(unit * (5000.0 + 3 * 2 * 800.0))
    assert masses["additional"]["with_anchorage_unclipped_kg"] == pytest.approx(unit * (2 * 3000.0 + 2 * 2 * 800.0))
