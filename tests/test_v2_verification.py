from __future__ import annotations

from math import pi

import pytest

from rebar_service.v2.verification import ROW_KEYS, reinforcement_rows, wire_overlay_state

RHO = 7850.0
T = 200.0


def _square(x0: float, y0: float, size: float) -> list[list[float]]:
    return [[x0, y0], [x0 + size, y0], [x0 + size, y0 + size], [x0, y0 + size]]


def _bar(start, end, d, zone_id=0):
    anchorage = {"start": 800, "end": 800}
    return {"zone_id": zone_id, "start": list(start), "end": list(end), "d": d, "anchorage": anchorage}


def _row(points, load, state="active", source_index=0):
    return {"points": points, "load": load, "color": 1, "overlay_state": state, "source_index": source_index}


def test_single_bar_crossing_square_polygon():
    polygons = [_row(_square(0, 0, 1000), 5.7)]
    bars = [_bar((500, -300), (500, 1300), 20)]  # 1000 mm of the axis lies inside the square
    rows = reinforcement_rows(polygons, bars, steel_density_kg_m3=RHO, t_mm=T)

    assert len(rows) == 1
    row = rows[0]
    assert tuple(row) == ROW_KEYS
    assert row["source_index"] == 0
    assert row["overlay_state"] == "active"
    assert row["need_load_sm2/m"] == pytest.approx(5.7)
    section = pi * 10.0**2
    assert row["fact_load_sm2/m"] == pytest.approx(10.0 * section * 1000.0 / 1_000_000.0)
    assert row["fact_load_sm2/m"] == pytest.approx(pi)
    assert row["need_load_kg/m3"] == pytest.approx(5.7 * RHO / (10.0 * T))
    assert row["fact_load_kg/m3"] == pytest.approx(section * 1000.0 * RHO / (1_000_000.0 * T))


def test_bars_partially_or_fully_outside_only_count_the_inside_length():
    polygons = [_row(_square(0, 0, 1000), 3.0)]
    bars = [
        _bar((500, 500), (500, 2500), 20),  # 500 mm inside, 1500 mm outside
        _bar((5000, 0), (5000, 1000), 20),  # entirely outside
        _bar((-100, 250), (400, 250), 20),  # horizontal, 400 mm inside
    ]
    rows = reinforcement_rows(polygons, bars, steel_density_kg_m3=RHO, t_mm=T)
    section = pi * 10.0**2
    assert rows[0]["fact_load_sm2/m"] == pytest.approx(10.0 * section * (500.0 + 400.0) / 1_000_000.0)


def test_real_and_empty_rows_use_wire_vocabulary_and_formulas():
    polygons = [
        _row(_square(0, 0, 1000), 5.0, "active", source_index=0),
        _row(_square(1000, 0, 1000), 5.0, "background_only", source_index=1),
        _row(_square(2000, 0, 1000), 5.0, "removed", source_index=2),
    ]
    # One bar through each square, d = 18.
    bars = [_bar((500 + 1000 * i, 0), (500 + 1000 * i, 1000), 18) for i in range(3)]
    rows = reinforcement_rows(polygons, bars, steel_density_kg_m3=RHO, t_mm=T)

    assert [r["source_index"] for r in rows] == [0, 1, 2]
    assert [r["overlay_state"] for r in rows] == ["active", "real", "empty"]

    expected_fact = 10.0 * pi * 9.0**2 * 1000.0 / 1_000_000.0
    active, real, empty = rows
    assert active["need_load_sm2/m"] == pytest.approx(5.0)
    assert active["fact_load_sm2/m"] == pytest.approx(expected_fact)

    # `real` polygons need nothing but the steel over them is still reported.
    assert real["need_load_sm2/m"] == 0.0
    assert real["need_load_kg/m3"] == 0.0
    assert real["fact_load_sm2/m"] == pytest.approx(expected_fact)
    assert real["fact_load_kg/m3"] == pytest.approx(expected_fact * RHO / (10.0 * T))

    # `empty` (removed) polygons carry no numbers at all.
    assert empty["need_load_sm2/m"] is None
    assert empty["fact_load_sm2/m"] is None
    assert empty["need_load_kg/m3"] is None
    assert empty["fact_load_kg/m3"] is None


def test_uniform_grid_matches_analytic_reinforcement_area():
    # d=18 @ step 300 => 10 * pi * 81 / 300 = 8.48 cm2/m.
    size = 30_000.0
    step = 300.0
    polygons = [_row(_square(0, 0, size), 7.0)]
    bars = [_bar((x, 0), (x, size), 18) for x in (step / 2 + step * i for i in range(int(size / step)))]
    rows = reinforcement_rows(polygons, bars, steel_density_kg_m3=RHO, t_mm=T)
    assert rows[0]["fact_load_sm2/m"] == pytest.approx(10.0 * pi * 81.0 / 300.0)
    assert rows[0]["fact_load_sm2/m"] == pytest.approx(8.48, abs=0.01)


def test_kg_per_m3_relation_to_cm2_per_m():
    polygons = [_row(_square(0, 0, 2000), 4.2)]
    bars = [
        _bar((300, -100), (300, 2100), 16),
        _bar((1200, 0), (1200, 2000), 25),
        _bar((0, 700), (2000, 700), 12),
    ]
    density, t = 7850.0, 600.0
    row = reinforcement_rows(polygons, bars, steel_density_kg_m3=density, t_mm=t)[0]
    factor = density / (10.0 * t)
    assert row["need_load_kg/m3"] == pytest.approx(row["need_load_sm2/m"] * factor)
    assert row["fact_load_kg/m3"] == pytest.approx(row["fact_load_sm2/m"] * factor)
    # Halving the thickness doubles the volumetric values while cm2/m stays put.
    thin = reinforcement_rows(polygons, bars, steel_density_kg_m3=density, t_mm=t / 2)[0]
    assert thin["fact_load_sm2/m"] == pytest.approx(row["fact_load_sm2/m"])
    assert thin["fact_load_kg/m3"] == pytest.approx(2.0 * row["fact_load_kg/m3"])
    assert thin["need_load_kg/m3"] == pytest.approx(2.0 * row["need_load_kg/m3"])


def test_no_bars_gives_zero_fact_and_rows_stay_in_input_order():
    polygons = [
        _row(_square(0, 0, 100), 1.0, source_index=7),
        _row(_square(100, 0, 100), 2.0, source_index=3),
    ]
    rows = reinforcement_rows(polygons, [], steel_density_kg_m3=RHO, t_mm=T)
    assert [r["source_index"] for r in rows] == [7, 3]
    assert [r["fact_load_sm2/m"] for r in rows] == [0.0, 0.0]
    assert [r["fact_load_kg/m3"] for r in rows] == [0.0, 0.0]
    assert [r["need_load_sm2/m"] for r in rows] == [1.0, 2.0]


def test_missing_source_index_falls_back_to_position():
    polygons = [{"points": _square(0, 0, 100), "load": 1.0, "overlay_state": "active"}] * 2
    rows = reinforcement_rows(polygons, [], steel_density_kg_m3=RHO, t_mm=T)
    assert [r["source_index"] for r in rows] == [0, 1]


def test_invalid_polygon_is_repaired_with_buffer_zero():
    # A square with a zero-width spike into its centre is invalid; buffer(0) restores the square.
    spiked = [[0, 0], [1000, 0], [1000, 1000], [0, 1000], [0, 0], [500, 500], [0, 0]]
    polygons = [_row(spiked, 1.0)]
    bars = [_bar((500, -10), (500, 1010), 10)]
    row = reinforcement_rows(polygons, bars, steel_density_kg_m3=RHO, t_mm=T)[0]
    assert row["fact_load_sm2/m"] == pytest.approx(10.0 * pi * 25.0 * 1000.0 / 1_000_000.0)


def test_zero_area_polygon_reports_need_but_no_fact():
    polygons = [_row([[0, 0], [100, 0], [200, 0]], 3.0)]
    row = reinforcement_rows(polygons, [_bar((0, 0), (200, 0), 10)], steel_density_kg_m3=RHO, t_mm=T)[0]
    assert row["need_load_sm2/m"] == pytest.approx(3.0)
    assert row["need_load_kg/m3"] == pytest.approx(3.0 * RHO / (10.0 * T))
    assert row["fact_load_sm2/m"] is None
    assert row["fact_load_kg/m3"] is None


def test_wire_states_pass_through_and_unknown_states_are_rejected():
    assert wire_overlay_state("active") == "active"
    assert wire_overlay_state("background_only") == "real"
    assert wire_overlay_state("removed") == "empty"
    assert wire_overlay_state("real") == "real"
    assert wire_overlay_state("empty") == "empty"
    assert wire_overlay_state(None) == "active"
    with pytest.raises(ValueError):
        wire_overlay_state("hidden")


@pytest.mark.parametrize(
    "kwargs",
    [{"steel_density_kg_m3": 0, "t_mm": 200}, {"steel_density_kg_m3": 7850, "t_mm": 0}],
)
def test_non_positive_density_or_thickness_is_rejected(kwargs):
    with pytest.raises(ValueError):
        reinforcement_rows([_row(_square(0, 0, 100), 1.0)], [], **kwargs)


def test_anchorage_is_ignored_and_degenerate_bars_are_skipped():
    polygons = [_row(_square(0, 0, 1000), 1.0)]
    bar = _bar((500, 0), (500, 1000), 20)
    bar["anchorage"] = {"start": 100_000, "end": 100_000}
    zero_length = _bar((500, 500), (500, 500), 20)
    row = reinforcement_rows(polygons, [bar, zero_length], steel_density_kg_m3=RHO, t_mm=T)[0]
    assert row["fact_load_sm2/m"] == pytest.approx(pi)


def test_bar_on_an_edge_shared_by_two_polygons_is_split_between_them():
    from rebar_service.v2.verification import reinforcement_rows

    left = {"points": [[0, 0], [1000, 0], [1000, 1000], [0, 1000]], "load": 10.0,
            "overlay_state": "active", "source_index": 0}
    right = {"points": [[1000, 0], [2000, 0], [2000, 1000], [1000, 1000]], "load": 10.0,
             "overlay_state": "active", "source_index": 1}
    bar = {"zone_id": 0, "start": [1000.0, 0.0], "end": [1000.0, 1000.0], "d": 20.0,
           "anchorage": {"start": 0.0, "end": 0.0}}
    rows = reinforcement_rows([left, right], [bar], steel_density_kg_m3=7850.0, t_mm=600.0)
    expected_total = 10.0 * 3.141592653589793 * 100.0 * 1000.0 / 1_000_000.0  # one bar over one square
    assert rows[0]["fact_load_sm2/m"] == pytest.approx(expected_total / 2)
    assert rows[1]["fact_load_sm2/m"] == pytest.approx(expected_total / 2)
    # a bar on the outer edge (no neighbour) counts fully for its polygon
    outer = {"zone_id": 0, "start": [0.0, 0.0], "end": [0.0, 1000.0], "d": 20.0,
             "anchorage": {"start": 0.0, "end": 0.0}}
    rows = reinforcement_rows([left, right], [outer], steel_density_kg_m3=7850.0, t_mm=600.0)
    assert rows[0]["fact_load_sm2/m"] == pytest.approx(expected_total)
    assert rows[1]["fact_load_sm2/m"] == 0.0
    # an opening next to the polygon does not take a share
    hole = {**right, "overlay_state": "removed"}
    rows = reinforcement_rows([left, hole], [bar], steel_density_kg_m3=7850.0, t_mm=600.0)
    assert rows[0]["fact_load_sm2/m"] == pytest.approx(expected_total)
