"""Validator-driven gap filling after the layout (rebar_service.v2.repair)."""

from __future__ import annotations

from math import pi

import pytest

from rebar_service.v2.repair import fill_gaps
from rebar_service.v2.verification import reinforcement_rows

RHO = 7850.0
FIELD = 3000.0


def _rows(load=40.0, size=300.0):
    """A 3000x3000 field cut into square elements of ``size`` mm, all needing ``load`` cm²/m."""
    rows, idx = [], 0
    n = int(FIELD / size)
    for i in range(n):
        for j in range(n):
            x0, y0 = i * size, j * size
            rows.append({"idx": idx, "source_index": idx, "overlay_state": "active", "load": load,
                         "points": [[x0, y0], [x0 + size, y0], [x0 + size, y0 + size], [x0, y0 + size]]})
            idx += 1
    return rows


def _bar(x, d, zone_id, y0=0.0, y1=FIELD, anchor=None):
    a = 40.0 * d if anchor is None else anchor
    return {"zone_id": zone_id, "start": [x, y0], "end": [x, y1], "d": d, "anchorage": {"start": a, "end": a}}


def _layout_with_gap():
    """ø16@300 background plus a ø25@100 zone across the whole field with one bar missing at x=1500."""
    bars = [_bar(x, 16, 0) for x in range(50, 3000, 300)]
    xs = [x for x in range(50, 3000, 100) if x != 1550]
    bars += [_bar(x, 25, 1) for x in xs]
    zone = {"id": 1, "kind": "additional", "arm": {"d": 25.0, "step": 100.0}, "left": 0, "right": len(xs) - 1,
            "length": FIELD, "anchorage": None, "origin": [50.0, 0.0], "direction": [1.0, 0.0]}
    bg = {"id": 0, "kind": "bg", "arm": {"d": 16.0, "step": 300.0}, "anchorage": None}
    unit = lambda d: RHO * pi * (d / 2) ** 2 * 1e-9
    mass = {"additional": {k: 0.0 for k in ("with_anchorage_kg", "without_anchorage_kg", "with_anchorage_unclipped_kg", "without_anchorage_unclipped_kg")},
            "bg": {k: 0.0 for k in ("with_anchorage_kg", "without_anchorage_kg", "with_anchorage_unclipped_kg", "without_anchorage_unclipped_kg")}}
    for b in bars:
        g = "bg" if b["zone_id"] == 0 else "additional"
        mass[g]["without_anchorage_kg"] += unit(b["d"]) * FIELD
        mass[g]["with_anchorage_kg"] += unit(b["d"]) * (FIELD + 80 * b["d"])
    return {"bars": bars, "zones": [bg, zone], "mass_metrics": mass, "is_feasible": True}


def _short(rows, bars, tol=0.5):
    # the same 300 mm window the filler itself uses to decide what is short
    res = reinforcement_rows(rows, bars, steel_density_kg_m3=RHO, t_mm=600.0, cover_mm=30.0, smoothing_mm=300.0)
    return [(r["source_index"], r["need_load_sm2/m"] - r["fact_load_sm2/m"]) for r in res
            if r["need_load_sm2/m"] - r["fact_load_sm2/m"] > tol]


def test_missing_bar_gap_is_closed_with_one_rod():
    rows = _rows(load=54.0)  # ø25@100 + ø16@300 = 49.1 + 6.7 = 55.8 >= 54 when complete
    out = _layout_with_gap()
    before = _short(rows, out["bars"])
    assert before, "the missing bar must leave short elements"
    fixed = fill_gaps(rows, out, axis="y", anchor_factor=40.0, cover_mm=30.0, steel_density_kg_m3=RHO)
    assert fixed["repair"]["rods_added"] == 1
    assert not _short(rows, fixed["bars"])
    rod = fixed["bars"][-1]
    assert rod["d"] == 25.0 and abs(rod["start"][0] - 1550.0) < 26.0  # middle of the 200 mm gap
    assert rod["anchorage"] == {"start": 1000.0, "end": 1000.0}
    assert rod["start"][1] >= 0.0 and rod["end"][1] <= FIELD  # clipped to the field
    assert rod["zone_id"] == 1  # a spacing shift inside the zone it was inserted into
    assert fixed["zones"] == out["zones"]  # zones are never modified by the filler
    added = fixed["mass_metrics"]["additional"]["with_anchorage_kg"] - out["mass_metrics"]["additional"]["with_anchorage_kg"]
    assert added == pytest.approx(RHO * pi * 12.5 ** 2 * 1e-9 * (rod["end"][1] - rod["start"][1] + 2000.0), rel=1e-6)
    assert fixed["repair"]["short_after"]["polygons"] == 0


def test_covering_layout_is_left_untouched():
    rows = _rows(load=50.0)
    out = _layout_with_gap()
    out["bars"].append(_bar(1550, 25, 1))  # complete the zone
    fixed = fill_gaps(rows, out, axis="y", anchor_factor=40.0, cover_mm=30.0, steel_density_kg_m3=RHO)
    assert fixed["repair"]["rods_added"] == 0 and fixed["repair"]["passes"] == 0
    assert fixed["bars"] == out["bars"] and fixed["zones"] == out["zones"]
    assert fixed["mass_metrics"] == out["mass_metrics"]


def test_second_pass_is_idempotent():
    rows = _rows(load=55.0)
    fixed = fill_gaps(rows, _layout_with_gap(), axis="y", anchor_factor=40.0, cover_mm=30.0, steel_density_kg_m3=RHO)
    again = fill_gaps(rows, fixed, axis="y", anchor_factor=40.0, cover_mm=30.0, steel_density_kg_m3=RHO)
    assert again["repair"]["rods_added"] == 0
    assert len(again["bars"]) == len(fixed["bars"])


def test_polygons_outside_every_additional_zone_are_not_repaired():
    # background only, elements demand more than the background: no zone to borrow a diameter from
    rows = _rows(load=20.0)
    bars = [_bar(x, 16, 0) for x in range(50, 3000, 300)]
    out = {"bars": bars, "zones": [{"id": 0, "kind": "bg", "arm": {"d": 16.0, "step": 300.0}, "anchorage": None}],
           "mass_metrics": {"additional": {}, "bg": {}}}
    fixed = fill_gaps(rows, out, axis="y", anchor_factor=40.0, cover_mm=30.0, steel_density_kg_m3=RHO)
    assert fixed["repair"]["rods_added"] == 0
    assert fixed["repair"]["short_before"]["polygons"] == len(rows)


def test_rod_blocked_farther_along_its_zone_falls_back_to_the_short_elements():
    """Another zone's rod sits on the wanted line beyond the short elements: the filler still
    closes the gap (shifting the rod by the clearance or shortening it) instead of giving up."""
    rows = _rows(load=55.0)
    out = _layout_with_gap()  # ø25 zone 1 over the whole field, bar at x=1550 missing
    # another zone's rod exactly on the line the filler wants (x=1550), covering the upper half only
    out["bars"].append(_bar(1550, 25, 2, y0=1500.0, y1=FIELD))
    out["zones"].append({"id": 2, "kind": "additional", "arm": {"d": 25.0, "step": 100.0}, "left": 0, "right": 0,
                         "length": 1500.0, "anchorage": None, "origin": [1550.0, 1500.0], "direction": [1.0, 0.0]})
    fixed = fill_gaps(rows, out, axis="y", anchor_factor=40.0, cover_mm=30.0, steel_density_kg_m3=RHO)
    assert fixed["repair"]["rods_added"] >= 1
    assert not _short(rows, fixed["bars"], tol=1.0)  # nothing short by more than 1 cm²/m
    new = fixed["bars"][len(out["bars"]):]
    # every added rod keeps the clearance to zone 2's rod wherever the two overlap along y
    for b in new:
        if b["end"][1] > 1500.0 + 1e-6:
            assert abs(b["start"][0] - 1550.0) >= 25.0 - 1e-6
    assert all(b["zone_id"] == 1 for b in new)


def test_rods_touching_the_extent_end_to_end_do_not_block():
    rows = _rows(load=54.0)
    out = _layout_with_gap()
    # a rod on the wanted line that ends exactly where the field starts (touching, not overlapping)
    out["bars"].append(_bar(1550, 25, 2, y0=-2000.0, y1=0.0))
    fixed = fill_gaps(rows, out, axis="y", anchor_factor=40.0, cover_mm=30.0, steel_density_kg_m3=RHO)
    assert fixed["repair"]["rods_added"] == 1
    rod = fixed["bars"][-1]
    assert abs(rod["start"][0] - 1550.0) < 26.0 and rod["start"][1] >= 0.0 and rod["end"][1] <= FIELD


def test_clear_position_finds_the_free_spot_between_two_close_blockers():
    """ø25 zone bars at 7039.5 / 7139.5 and a ø18 background bar at 7100: the wanted position 7100
    is blocked, +21.5 collides with the bar at 7139.5, and the greedy push used to bounce between
    the two blockers forever; the free spot 7078.5 (21.5 from the background bar) must be found."""
    from rebar_service.v2.repair import _clear_position

    bars = [_bar(7039.5, 25, 1, y0=0.0, y1=3000.0), _bar(7139.5, 25, 1, y0=0.0, y1=3000.0), _bar(7100.0, 18, 0, y0=0.0, y1=3000.0)]
    pos = _clear_position(7100.0, 25.0, bars, 500.0, 900.0, 0, 1)
    assert pos == pytest.approx(7078.5)
