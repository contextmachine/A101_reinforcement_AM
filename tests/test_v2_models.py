from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from rebar_service.v2.models import (
    Anchorage,
    Bar,
    BarsConfig,
    BarsRequest,
    V2CancelMutation,
    FEPolygon,
    FEPolygonOut,
    V2NMutation,
    OverlayIn,
    OverlayOut,
    OverlaysPost,
    RcVariant,
    TaskConfig,
    V2TaskCreate,
    VerificationConfig,
    VerificationRequest,
    VerificationRow,
    Zone,
    ZoneAdditional,
    ZoneBase,
)


BG_ZONE = {"id": 0, "kind": "bg", "arm": {"d": 18.0, "step": 300.0}}
ADDITIONAL_ZONE = {
    "id": 1,
    "kind": "additional",
    "arm": {"d": 20.0, "step": 150.0},
    "left": 10,
    "right": 5,
    "length": 1730.0,
    "origin": [4860.0, 700.0],
    "direction": [0.0, -1.0],
}


def test_rc_variant_and_anchorage_require_physical_values():
    variant = RcVariant(d=18.0, step=300.0)
    assert (variant.d, variant.step) == (18.0, 300.0)
    for payload in ({"d": 0, "step": 300}, {"d": 18, "step": -1}):
        with pytest.raises(ValidationError):
            RcVariant.model_validate(payload)
    assert Anchorage(start=0.0, end=800.0).end == 800.0
    with pytest.raises(ValidationError):
        Anchorage.model_validate({"start": -1.0, "end": 0.0})


def test_request_bodies_forbid_unknown_fields():
    for model, payload in (
        (RcVariant, {"d": 18, "step": 300, "diameter": 18}),
        (TaskConfig, {"axis": "x", "threads": 4}),
        (FEPolygon, {"load": 1.0, "points": [[0, 0], [1, 0], [1, 1]], "area": 3}),
        (BarsConfig, {"axis": "x", "solver_time_limit": 5}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(payload)


def test_zone_union_is_discriminated_on_kind():
    adapter = TypeAdapter(Zone)
    assert isinstance(adapter.validate_python(BG_ZONE), ZoneBase)
    additional = adapter.validate_python(ADDITIONAL_ZONE)
    assert isinstance(additional, ZoneAdditional)
    assert additional.left == 10 and additional.right == 5
    with pytest.raises(ValidationError):
        adapter.validate_python({**BG_ZONE, "kind": "background"})
    # A bg payload must not silently absorb ZoneAdditional-only fields.
    with pytest.raises(ValidationError):
        adapter.validate_python({**BG_ZONE, "length": 100.0})


def test_zone_additional_direction_must_be_a_unit_vector():
    adapter = TypeAdapter(Zone)
    assert adapter.validate_python({**ADDITIONAL_ZONE, "direction": [0.6, 0.8]}).direction == (0.6, 0.8)
    adapter.validate_python({**ADDITIONAL_ZONE, "direction": [1.0 + 9e-6, 0.0]})
    for bad in ([1.0, 1.0], [0.0, 0.0], [0.5, 0.0]):
        with pytest.raises(ValidationError):
            adapter.validate_python({**ADDITIONAL_ZONE, "direction": bad})


def test_zone_defaults_keep_geometry_without_anchorage():
    zone = ZoneAdditional.model_validate(ADDITIONAL_ZONE)
    assert zone.anchorage is None
    assert zone.model_dump()["length"] == 1730.0
    bar = Bar.model_validate(
        {"zone_id": 0, "start": [100.0, 50.0], "end": [14200.0, 50.0], "d": 18.0,
         "anchorage": {"start": 800.0, "end": 800.0}}
    )
    assert bar.anchorage.start == 800.0 and bar.start == (100.0, 50.0)


def test_task_create_deduplicates_n_and_requires_positive_values():
    task = V2TaskCreate.model_validate({"scene_id": "s", "n": [3, 1, 3, 2, 1]})
    assert task.n == [3, 1, 2]
    assert task.overlay_id == 0 and task.smooth is False
    assert task.config.axis == "y" and task.config.anchor_factor == 40
    assert task.config.solver.solver_time_limit is None
    for bad in ([], [0], [1, -2]):
        with pytest.raises(ValidationError):
            V2TaskCreate.model_validate({"scene_id": "s", "n": bad})


def test_n_mutation_and_cancel_mutation_share_the_n_rules():
    assert V2NMutation.model_validate({"n": [2, 2, 1]}).n == [2, 1]
    assert V2CancelMutation.model_validate({}).n is None
    assert V2CancelMutation.model_validate({"n": [4, 4]}).n == [4]
    with pytest.raises(ValidationError):
        V2NMutation.model_validate({"n": []})


def test_task_config_accepts_the_contract_example():
    config = TaskConfig.model_validate(
        {
            "max_layers": 2, "axis": "x", "anchor_factor": 40, "min_width_mm": 300,
            "max_snap_mm": 600, "min_bar_gap_mm": 50, "steel_density_kg_m3": 7850,
            "back_grid": {"d": 18.0, "step": 300.0},
            "stock": [{"d": 18.0, "step": 300.0}, {"d": 25.0, "step": 100.0}],
            "solver": {"solver_time_limit": None},
        }
    )
    assert config.back_grid.d == 18.0 and len(config.stock) == 2
    with pytest.raises(ValidationError):
        TaskConfig.model_validate({"min_bar_gap_mm": 0})


def test_fe_polygon_requires_three_points_and_keeps_color():
    polygon = FEPolygon.model_validate(
        {"load": 5.7, "color": 181, "points": [[100, 0], [200, 340], [100, 406.25]]}
    )
    assert polygon.color == 181 and polygon.points[0] == (100.0, 0.0)
    assert FEPolygon.model_validate({"load": 1, "points": [[0, 0], [1, 0], [1, 1]]}).color is None
    with pytest.raises(ValidationError):
        FEPolygon.model_validate({"load": 1, "points": [[0, 0], [1, 0]]})


def test_fe_polygon_out_uses_the_wire_overlay_vocabulary():
    row = {"load": 1.0, "points": [[0, 0], [1, 0], [1, 1]], "overlay_state": "real", "source_index": 7}
    assert FEPolygonOut.model_validate(row).overlay_state == "real"
    for bad in ("background_only", "removed", "unknown"):
        with pytest.raises(ValidationError):
            FEPolygonOut.model_validate({**row, "overlay_state": bad})


def test_overlay_models_default_real_and_time():
    overlay = OverlayIn.model_validate({"type": "clean", "idxs": [3, 56, 78]})
    assert overlay.real is False and overlay.time is None
    stored = OverlayOut.model_validate({"type": "unclean", "idxs": [4], "id": 67690, "time": 12345654})
    assert stored.id == 67690 and stored.time == 12345654
    post = OverlaysPost.model_validate({"scene_id": "s", "overlays": [{"type": "clean", "idxs": []}]})
    assert post.scene_id == "s" and len(post.overlays) == 1
    assert OverlaysPost.model_validate({}).overlays == []
    with pytest.raises(ValidationError):
        OverlayIn.model_validate({"type": "erase", "idxs": []})


def test_bars_request_requires_exactly_one_background_zone_and_unique_ids():
    request = BarsRequest.model_validate(
        {"scene_id": "s", "config": {"axis": "x"}, "zones": [BG_ZONE, ADDITIONAL_ZONE]}
    )
    assert request.config.axis == "x" and request.config.min_bar_gap_mm is None
    assert [zone.id for zone in request.zones] == [0, 1]
    with pytest.raises(ValidationError, match="ровно одна"):
        BarsRequest.model_validate({"scene_id": "s", "zones": [ADDITIONAL_ZONE]})
    with pytest.raises(ValidationError, match="ровно одна"):
        BarsRequest.model_validate({"scene_id": "s", "zones": [BG_ZONE, {**BG_ZONE, "id": 2}]})
    with pytest.raises(ValidationError, match="уникальны"):
        BarsRequest.model_validate(
            {"scene_id": "s", "zones": [BG_ZONE, {**ADDITIONAL_ZONE, "id": 0}]}
        )


def test_verification_request_requires_thickness():
    request = VerificationRequest.model_validate(
        {
            "scene_id": "s", "smooth": True, "overlay_id": -1,
            "config": {"axis": "x", "anchor_factor": 40, "steel_density_kg_m3": 7850, "t": 600},
            "zones": [BG_ZONE],
        }
    )
    assert request.config.t == 600 and request.config.steel_density_kg_m3 == 7850
    with pytest.raises(ValidationError):
        VerificationConfig.model_validate({"axis": "x"})


def test_verification_row_uses_contract_slash_aliases_in_both_directions():
    payload = {
        "source_index": 0, "overlay_state": "active",
        "need_load_sm2/m": 5.7, "fact_load_sm2/m": 6.8,
        "need_load_kg/m3": 19.6, "fact_load_kg/m3": 28.4,
    }
    row = VerificationRow.model_validate(payload)
    assert row.need_load_sm2_m == 5.7 and row.fact_load_kg_m3 == 28.4
    assert row.model_dump(by_alias=True) == payload
    # populate_by_name keeps the python spelling usable from internal code.
    by_name = VerificationRow(source_index=1, overlay_state="empty", need_load_sm2_m=None)
    assert by_name.model_dump(by_alias=True)["need_load_sm2/m"] is None
