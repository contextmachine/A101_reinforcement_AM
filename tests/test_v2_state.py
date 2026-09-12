from pathlib import Path

import pytest

from rebar_service.models import (
    V2BarsRequest,
    V2TaskStart,
    V2VerificationRequest,
)
from rebar_service.v2_state import next_n_action, normalize_v2_state


def test_v2_task_has_no_component_fields_and_n_is_unique_positive():
    request = V2TaskStart.model_validate({
        "scene_id": "scene",
        "overlay_id": 0,
        "smooth": False,
        "n": [10, 10, 2],
        "config": {
            "max_layers": 2,
            "axis": "x",
            "anchor_factor": 40,
            "min_width_mm": 300,
            "max_snap_mm": 600,
            "min_bar_gap_mm": 50,
            "steel_density_kg_m3": 7850,
            "back_grid": {"d": 18.0, "step": 300.0},
            "stock": [{"d": 20.0, "step": 150.0}],
            "solver": {"solver_time_limit": None},
        },
    })
    assert request.n == [10, 2]
    dumped = request.model_dump()
    assert "components" not in dumped
    assert "component_selection" not in dumped

    with pytest.raises(ValueError):
        V2TaskStart.model_validate({**dumped, "n": [0]})


def test_v2_bar_and_verification_contracts_keep_authoritative_anchorage():
    zone = {
        "id": 1,
        "kind": "additional",
        "arm": {"d": 20.0, "step": 150.0},
        "left": 0,
        "right": 0,
        "length": 1730.0,
        "start_anchorage": 700.0,
        "end_anchorage": 900.0,
        "origin": [4860.0, 700.0],
        "direction": [0.0, -1.0],
    }
    bars = V2BarsRequest.model_validate({
        "scene_id": "scene", "smooth": False, "overlay_id": 0,
        "config": {"axis": "x", "anchor_factor": 40, "min_bar_gap_mm": 50},
        "zones": [{"id": 0, "kind": "bg", "arm": {"d": 18.0, "step": 300.0}}, zone],
    })
    assert bars.zones[1].start_anchorage == 700.0
    assert bars.zones[1].end_anchorage == 900.0

    verification = V2VerificationRequest.model_validate({
        **bars.model_dump(),
        "config": {
            "axis": "x", "anchor_factor": 40, "min_bar_gap_mm": 50,
            "steel_density_kg_m3": 7850, "t": 600,
        },
    })
    assert verification.config.t == 600


def test_retry_policy_does_not_duplicate_active_or_successful_n():
    for state in ("pending", "preparing", "solving", "fitting", "baring", "success"):
        assert next_n_action(state) == "keep"
    assert next_n_action("cancelled") == "retry"
    assert next_n_action("error") == "retry"
    assert next_n_action(None) == "create"


def test_v2_public_state_names_are_strict():
    for state in ("pending", "preparing", "solving", "fitting", "baring", "error", "success", "cancelled"):
        assert normalize_v2_state(state) == state
    with pytest.raises(ValueError):
        normalize_v2_state("requested")


def test_v2_migration_creates_dedicated_durable_tables():
    root = Path(__file__).resolve().parents[1]
    migration = (root / "migrations/versions/0005_v2_pipeline.py").read_text(encoding="utf-8")
    for table in (
        "v2_tasks",
        "v2_task_n",
        "v2_runtime_artifacts",
        "v2_bars_requests",
        "v2_verification_requests",
    ):
        assert f'create_table(\n        "{table}"' in migration
    assert "attempt" in migration
    assert "preparation_state" in migration
