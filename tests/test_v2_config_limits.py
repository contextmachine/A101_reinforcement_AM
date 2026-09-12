from pathlib import Path

import pytest

from rebar_service.config import Settings


def test_effective_solver_and_prepare_n_limits_are_centralized():
    s = Settings(max_n_value=5000, solver_hard_max_n=1200, prepare_problem_max_n=900)
    assert s.effective_solver_max_n() == 1200
    assert s.effective_prepare_max_n() == 900


def test_solver_time_limit_server_cap_is_explicit():
    s = Settings(max_solver_time_limit_seconds=100.0)
    assert s.effective_solver_time_limit(None) == s.solver_time_limit
    assert s.effective_solver_time_limit(25) == 25.0
    with pytest.raises(ValueError, match="server limit"):
        s.effective_solver_time_limit(101)


def test_legacy_pipeline_has_no_module_solver_hard_cap_constant():
    root = Path(__file__).resolve().parents[1]
    text = (root / "rebar_service/pipeline.py").read_text(encoding="utf-8")
    assert "SOLVER_HARD_MAX_N" not in text


def test_legacy_api_compatibility_caps_are_named_settings_not_literals():
    s = Settings()
    assert s.legacy_task_request_max_n == 250
    assert s.legacy_prepared_max_n_override == 100
    root = Path(__file__).resolve().parents[1]
    text = (root / "rebar_service/api.py").read_text(encoding="utf-8")
    assert "min(int(settings.max_n_value), 250)" not in text
    assert "min(int(settings.max_n_value), 100)" not in text
