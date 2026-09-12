"""Smeared-density verification (design §6): rods with a diameter, tributary bands, EC2 reach."""

from __future__ import annotations

from math import pi

import pytest

from rebar_service.v2.verification import (
    ROW_KEYS,
    bar_reach_mm,
    reinforcement_rows,
    wire_overlay_state,
)

RHO = 7850.0
T = 600.0
COVER = 30.0


def _poly(points, load, state=None, index=None):
    row = {"points": [list(p) for p in points], "load": load}
    if state is not None:
        row["overlay_state"] = state
    if index is not None:
        row["source_index"] = index
    return row


def _bar(start, end, d, zone_id=1):
    return {"zone_id": zone_id, "start": list(start), "end": list(end), "d": d, "anchorage": {"start": 0, "end": 0}}


def _square(x0, y0, size, load=5.0, **kw):
    return _poly([(x0, y0), (x0 + size, y0), (x0 + size, y0 + size), (x0, y0 + size)], load, **kw)


def _mesh(step, d, count, length=3000.0):
    """``count`` bars along y, spaced ``step`` in x, starting at x=0."""
    return [_bar((i * step, 0.0), (i * step, length), d) for i in range(count)]


def _area(d):
    return pi * (d / 2.0) ** 2


def test_row_shape_and_units_for_a_single_bar():
    # one ø20 bar through a 1000 mm square: band = ±reach on both sides (no neighbours)
    polygons = [_square(0, 0, 1000, load=5.7)]
    bars = [_bar((500, -100), (500, 1100), 20)]
    rows = reinforcement_rows(polygons, bars, steel_density_kg_m3=RHO, t_mm=T, cover_mm=COVER)
    assert len(rows) == 1
    row = rows[0]
    assert tuple(row) == ROW_KEYS
    assert row["source_index"] == 0 and row["overlay_state"] == "active"
    assert row["need_load_sm2/m"] == pytest.approx(5.7)
    reach = bar_reach_mm(20, COVER)  # 5 * (30 + 10) = 200 mm each side
    assert reach == pytest.approx(200.0)
    density = 10.0 * _area(20) / (2 * reach)  # cm²/m inside the band
    covered_fraction = (2 * reach) / 1000.0  # the band covers 400 of the 1000 mm width
    assert row["fact_load_sm2/m"] == pytest.approx(density * covered_fraction, rel=0.03)
    assert row["need_load_kg/m3"] == pytest.approx(5.7 * RHO / (10.0 * T))
    assert row["fact_load_kg/m3"] == pytest.approx(row["fact_load_sm2/m"] * RHO / (10.0 * T))


def test_uniform_mesh_reads_the_analytic_area_per_metre():
    # ø18 @ 300 over a big square: 10·π·81/300 = 8.48 cm²/m everywhere inside the mesh
    bars = _mesh(300.0, 18, 12, length=3000.0)
    polygons = [_square(600, 600, 1500, load=8.0)]
    rows = reinforcement_rows(polygons, bars, steel_density_kg_m3=RHO, t_mm=T, cover_mm=COVER)
    assert rows[0]["fact_load_sm2/m"] == pytest.approx(10.0 * pi * 81.0 / 300.0, rel=1e-3)
    assert rows[0]["fact_load_sm2/m"] == pytest.approx(8.48, abs=0.02)


def test_result_does_not_depend_on_how_the_elements_are_cut():
    bars = _mesh(300.0, 18, 12, length=3000.0)
    whole = [_square(600, 600, 1500, load=8.0)]
    # the same area cut into narrow strips between and across the bars
    strips = [_poly([(x, 600), (x + 100, 600), (x + 100, 2100), (x, 2100)], 8.0) for x in range(600, 2100, 100)]
    whole_fact = reinforcement_rows(whole, bars, steel_density_kg_m3=RHO, t_mm=T)[0]["fact_load_sm2/m"]
    strip_facts = [r["fact_load_sm2/m"] for r in reinforcement_rows(strips, bars, steel_density_kg_m3=RHO, t_mm=T)]
    assert all(f == pytest.approx(whole_fact, rel=1e-6) for f in strip_facts)
    assert min(strip_facts) > 0.99 * whole_fact


def test_strip_between_bars_farther_apart_than_the_reach_is_unreinforced():
    # two ø20 bars 1000 mm apart, reach 200 mm each: the middle 600 mm carries nothing
    bars = [_bar((0, 0), (0, 3000), 20), _bar((1000, 0), (1000, 3000), 20)]
    middle = [_square(350, 500, 300, load=5.0)]  # x in [350, 650]
    near = [_poly([(0, 500), (150, 500), (150, 800), (0, 800)], 5.0)]  # x in [0, 150]
    rows = reinforcement_rows(middle + near, bars, steel_density_kg_m3=RHO, t_mm=T, cover_mm=COVER)
    assert rows[0]["fact_load_sm2/m"] == 0.0
    assert rows[1]["fact_load_sm2/m"] == pytest.approx(10.0 * _area(20) / 400.0, rel=1e-3)


def test_cover_sets_the_reach():
    bars = [_bar((0, 0), (0, 3000), 20)]
    polygon = [_poly([(150, 500), (450, 500), (450, 800), (150, 800)], 5.0)]  # x in [150, 450]
    close = reinforcement_rows(polygon, bars, steel_density_kg_m3=RHO, t_mm=T, cover_mm=30.0)[0]
    wide = reinforcement_rows(polygon, bars, steel_density_kg_m3=RHO, t_mm=T, cover_mm=80.0)[0]
    # cover 30 -> reach 200: only x in [150, 200] is covered; cover 80 -> reach 450: all of it
    assert 0 < close["fact_load_sm2/m"] < wide["fact_load_sm2/m"]
    assert wide["fact_load_sm2/m"] == pytest.approx(10.0 * _area(20) / 900.0, rel=1e-3)


def test_bar_ending_inside_the_polygon_counts_only_where_it_exists():
    # mesh of ø18 @ 300 whose bars stop half-way through the polygon
    bars = _mesh(300.0, 18, 12, length=1350.0)
    polygons = [_square(600, 600, 1500, load=8.0)]
    row = reinforcement_rows(polygons, bars, steel_density_kg_m3=RHO, t_mm=T)[0]
    assert row["fact_load_sm2/m"] == pytest.approx(0.5 * 10.0 * pi * 81.0 / 300.0, rel=0.03)


def test_stacked_layers_on_the_same_line_add_their_area():
    single = _mesh(300.0, 20, 12)
    double = single + _mesh(300.0, 20, 12)
    polygons = [_square(600, 600, 1500, load=8.0)]
    one = reinforcement_rows(polygons, single, steel_density_kg_m3=RHO, t_mm=T)[0]["fact_load_sm2/m"]
    two = reinforcement_rows(polygons, double, steel_density_kg_m3=RHO, t_mm=T)[0]["fact_load_sm2/m"]
    assert two == pytest.approx(2.0 * one, rel=1e-6)


def test_bars_along_x_are_handled_in_their_own_frame():
    bars = [_bar((0.0, i * 300.0), (3000.0, i * 300.0), 18) for i in range(12)]
    polygons = [_square(600, 600, 1500, load=8.0)]
    row = reinforcement_rows(polygons, bars, steel_density_kg_m3=RHO, t_mm=T)[0]
    assert row["fact_load_sm2/m"] == pytest.approx(10.0 * pi * 81.0 / 300.0, rel=1e-3)


def test_real_and_empty_rows_use_wire_vocabulary_and_formulas():
    bars = _mesh(300.0, 18, 12)
    polygons = [
        _square(600, 600, 900, load=5.0, state="active", index=0),
        _square(600, 600, 900, load=5.0, state="background_only", index=1),
        _square(600, 600, 900, load=5.0, state="removed", index=2),
    ]
    rows = reinforcement_rows(polygons, bars, steel_density_kg_m3=RHO, t_mm=T)
    assert [r["source_index"] for r in rows] == [0, 1, 2]
    assert [r["overlay_state"] for r in rows] == ["active", "real", "empty"]
    active, real, empty = rows
    expected = 10.0 * pi * 81.0 / 300.0
    assert active["need_load_sm2/m"] == pytest.approx(5.0)
    assert active["fact_load_sm2/m"] == pytest.approx(expected, rel=1e-3)
    assert real["need_load_sm2/m"] == 0.0 and real["need_load_kg/m3"] == 0.0
    assert real["fact_load_sm2/m"] == pytest.approx(expected, rel=1e-3)
    assert real["fact_load_kg/m3"] == pytest.approx(expected * RHO / (10.0 * T), rel=1e-3)
    assert all(empty[key] is None for key in ROW_KEYS[2:])


def test_kg_per_m3_relation_to_cm2_per_m():
    bars = _mesh(300.0, 18, 12)
    polygons = [_square(600, 600, 900, load=8.0)]
    density, t = 7700.0, 250.0
    row = reinforcement_rows(polygons, bars, steel_density_kg_m3=density, t_mm=t)[0]
    factor = density / (10.0 * t)
    assert row["need_load_kg/m3"] == pytest.approx(row["need_load_sm2/m"] * factor)
    assert row["fact_load_kg/m3"] == pytest.approx(row["fact_load_sm2/m"] * factor)
    thin = reinforcement_rows(polygons, bars, steel_density_kg_m3=density, t_mm=t / 2)[0]
    assert thin["fact_load_sm2/m"] == pytest.approx(row["fact_load_sm2/m"])
    assert thin["fact_load_kg/m3"] == pytest.approx(2.0 * row["fact_load_kg/m3"])


def test_no_bars_gives_zero_fact_and_rows_stay_in_input_order():
    polygons = [_square(0, 0, 100, load=1.0, index=7), _square(500, 0, 100, load=2.0, index=3)]
    rows = reinforcement_rows(polygons, [], steel_density_kg_m3=RHO, t_mm=T)
    assert [r["source_index"] for r in rows] == [7, 3]
    assert [r["fact_load_sm2/m"] for r in rows] == [0.0, 0.0]
    assert [r["need_load_sm2/m"] for r in rows] == [1.0, 2.0]


def test_missing_source_index_falls_back_to_position():
    rows = reinforcement_rows([_square(0, 0, 100, 1.0), _square(200, 0, 100, 1.0)], [], steel_density_kg_m3=RHO, t_mm=T)
    assert [r["source_index"] for r in rows] == [0, 1]


def test_invalid_polygon_is_repaired_with_buffer_zero():
    bowtie = _poly([(0, 0), (1000, 1000), (1000, 0), (0, 1000)], 3.0)
    bars = _mesh(300.0, 18, 6, length=1000.0)
    row = reinforcement_rows([bowtie], bars, steel_density_kg_m3=RHO, t_mm=T)[0]
    assert row["fact_load_sm2/m"] > 0


def test_zero_area_polygon_reports_need_but_no_fact():
    degenerate = _poly([(0, 0), (100, 0), (200, 0)], 3.0)
    row = reinforcement_rows([degenerate], [_bar((0, -10), (200, -10), 10)], steel_density_kg_m3=RHO, t_mm=T)[0]
    assert row["need_load_sm2/m"] == pytest.approx(3.0)
    assert row["fact_load_sm2/m"] is None and row["fact_load_kg/m3"] is None


def test_tiny_polygon_without_a_raster_cell_centre_is_sampled_at_its_point():
    bars = _mesh(300.0, 18, 12)
    tiny = [_square(605, 605, 5, load=8.0)]
    row = reinforcement_rows(tiny, bars, steel_density_kg_m3=RHO, t_mm=T)[0]
    assert row["fact_load_sm2/m"] == pytest.approx(10.0 * pi * 81.0 / 300.0, rel=1e-3)


def test_parameter_validation():
    polygons = [_square(0, 0, 100, 1.0)]
    with pytest.raises(ValueError):
        reinforcement_rows(polygons, [], steel_density_kg_m3=0, t_mm=T)
    with pytest.raises(ValueError):
        reinforcement_rows(polygons, [], steel_density_kg_m3=RHO, t_mm=0)
    with pytest.raises(ValueError):
        reinforcement_rows(polygons, [], steel_density_kg_m3=RHO, t_mm=T, cover_mm=-1)
    with pytest.raises(ValueError):
        reinforcement_rows(polygons, [_bar((0, 0), (1, 1), 0)], steel_density_kg_m3=RHO, t_mm=T)


def test_wire_states_pass_through_and_unknown_states_are_rejected():
    assert wire_overlay_state("active") == "active"
    assert wire_overlay_state("background_only") == "real"
    assert wire_overlay_state("removed") == "empty"
    assert wire_overlay_state("real") == "real"
    assert wire_overlay_state(None) == "active"
    with pytest.raises(ValueError):
        wire_overlay_state("gone")
