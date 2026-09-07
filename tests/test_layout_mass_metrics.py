from shapely.geometry import box

from A101.axis_orientation import add_box_anchorage
from A101.rebar_field_layout import layout_rebars
from rebar_service.pipeline import augment_layout_mass_metrics, to_compat_result


def test_anchorage_preserves_unclipped_bounds_before_field_clamp():
    rows = add_box_anchorage(
        [(100, 100, 900, 900, 1)],
        recipes=None,
        diameters={1: 20},
        steps={1: 150},
        anchor_factor=32,
        axis="y",
        field=box(0, 0, 1000, 1000),
    )
    assert rows[0]["fitted_bounds"] == (100.0, 100.0, 900.0, 900.0)
    assert rows[0]["anchored_bounds_unclipped"] == (100.0, -540.0, 900.0, 1540.0)
    assert rows[0]["bounds"] == (100.0, 0.0, 900.0, 1000.0)


def test_layout_metrics_distinguish_anchored_and_unclipped_mass():
    anchored = add_box_anchorage(
        [(100, 100, 900, 900, 1)],
        recipes=None,
        diameters={1: 20},
        steps={1: 150},
        anchor_factor=32,
        axis="y",
        field=box(0, 0, 1000, 1000),
    )
    layout = layout_rebars(
        polygons=[box(0, 0, 1000, 1000)],
        boxes=anchored,
        background=(18, 300),
        axis="y",
        min_step=100,
    )
    enriched = augment_layout_mass_metrics(layout, steel_density_kg_m3=7850.0)
    zone = next(z for z in enriched["zones"] if not z.get("background"))
    metrics = enriched["mass_metrics"]

    assert zone["final_rectangle_without_anchorage"] != zone["final_rectangle_with_anchorage"]
    assert zone["final_rectangle_with_anchorage_unclipped"][1] < 0
    assert metrics["with_anchorage_unclipped_kg"] >= metrics["with_anchorage_kg"]
    assert metrics["without_anchorage_unclipped_kg"] >= metrics["without_anchorage_kg"]

    compat = to_compat_result({
        "solution_id": "s",
        "source": "whole",
        "total_N": 1,
        "component_ns": {"whole": 1},
        "proxy_mass": 1.0,
        "actual_mass_kg": metrics["with_anchorage_kg"],
        "is_feasible": True,
        "is_optimal": True,
        "status": "optimal",
        "bar_layout": enriched,
        "mass_metrics": metrics,
    })
    compat_zone = compat["summary"]["zones"][0]
    assert compat_zone["final rectangle"] != compat_zone["final rectangle with anchorage"]
    assert "zone mass with anchorage unclipped" in compat_zone
    assert compat["summary"]["mass with anchorage unclipped"] == metrics["with_anchorage_unclipped_kg"]
