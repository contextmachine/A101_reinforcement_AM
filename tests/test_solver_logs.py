"""HiGHS log files: path helper + real tiny HiGHS runs through the solve and fit paths."""

from __future__ import annotations

import os

import pytest

from rebar_service.config import Settings
from rebar_service.solver_logs import highs_log_options, solver_log_file
from support.tiny_component_problem import tiny_problem

pytest.importorskip("highspy")


def _log_written(path: str) -> str:
    assert os.path.isfile(path), f"HiGHS log not created: {path}"
    assert os.path.getsize(path) > 0, f"HiGHS log is empty: {path}"
    text = open(path, encoding="utf-8", errors="replace").read()
    assert "HiGHS" in text or "Running" in text
    return text


def test_solver_log_file_creates_directories_and_uses_settings_dir(tmp_path):
    settings = Settings(solver_log_dir=str(tmp_path / "logs"))
    path = solver_log_file(settings, "task-1", 7, "worker/abc")
    assert path == str(tmp_path / "logs" / "task-1" / "7" / "worker_abc.log")
    assert os.path.isdir(os.path.dirname(path))
    assert not os.path.exists(path)


def test_solver_log_file_defaults_to_app_logs_dir():
    settings = Settings(solver_log_dir="")
    from rebar_service.config import APP_DIR

    assert settings.solver_log_path == APP_DIR / "logs"


def test_highs_log_options_shape(tmp_path):
    assert highs_log_options(str(tmp_path / "x.log")) == {
        "log_file": str(tmp_path / "x.log"),
        "output_flag": True,
        "log_to_console": False,
    }


# NOTE: HiGHS keeps one process-wide task scheduler; changing ``threads`` between
# in-process runs makes ``Highs.run`` fail with kError. In-process tests therefore
# keep threads=1 (the suite default); ``threads=0`` (HiGHS auto) is exercised in
# the spawned solver subprocess, which starts fresh.


def _deficient_solver_result(n: int = 2) -> dict:
    """A feasible-looking solver result whose class-2 boxes under-cover the class-3
    column of the tiny matrix ``[[1, 2, 3], [2, 1, 3]]`` so that ``fit_box_layout``
    must run its MILP (class upgrade) instead of returning "Already covered"."""

    return {n: {"n": n, "is_feasible": True, "is_optimal": True,
                "rectangles": [(0, 0, 1, 1, 2), (2, 0, 2, 1, 2)]}}


def test_explicit_logging_options_win_over_solver_msg_default(tmp_path):
    """solver_msg=False would disable output; explicit output_flag/log_file must still write the file."""

    from A101.select_min_density_rectangles_recipes import select_min_density_rectangles

    problem, _, _ = tiny_problem(max_n=4)
    path = str(tmp_path / "inproc.log")
    result = select_min_density_rectangles(
        prepared=problem["prepared"],
        n=2,
        backend="highs",
        solver_msg=False,
        threads=1,
        highs_options=highs_log_options(path),
    )
    assert result["is_feasible"] and not result.get("n1_fast_path")
    _log_written(path)


def test_solve_component_frontier_writes_highs_log_through_subprocess(tmp_path):
    from A101.reinforcement_components import solve_component_frontier

    problem, _, _ = tiny_problem(max_n=4)
    settings = Settings(solver_log_dir=str(tmp_path / "logs"))
    path = solver_log_file(settings, "taskA", 2, "worker-1")
    results, _ = solve_component_frontier(
        problem,
        [2],
        timeout=120.0,
        threads=0,  # HiGHS auto: accepted now; runs in a fresh spawned process
        backend="highs",
        raise_errors=True,
        highs_options=highs_log_options(path),
    )
    assert results[2].get("error") is None and results[2]["is_feasible"], results[2]
    assert path.endswith(os.path.join("taskA", "2", "worker-1.log"))
    _log_written(path)


def test_fit_component_frontier_writes_highs_log(tmp_path):
    from A101.reinforcement_components import fit_component_frontier

    problem, cfg, field_geometry = tiny_problem(max_n=4)
    path = str(tmp_path / "fit" / "2" / "w.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fitted = fit_component_frontier(
        problem,
        _deficient_solver_result(2),
        recipes=cfg.get("recipes"),
        densities=cfg["densities"],
        diameters=cfg["diameters"],
        steps=cfg["steps"],
        anchor_factor=40.0,
        axis="y",
        field=field_geometry,
        min_width=300.0,
        fit_milp_backend="highs",
        fit_threads=1,
        highs_options=highs_log_options(path),
    )
    assert fitted[2]["is_feasible"], fitted[2]
    assert fitted[2]["fit_result"]["status"] != "Already covered"
    assert fitted[2]["class_changes"], "the MILP must have upgraded a class"
    _log_written(path)


def test_fit_box_layout_direct_highs_log(tmp_path):
    from shapely.geometry import box

    from A101.fit_box_layout import fit_box_layout

    path = str(tmp_path / "fit_direct.log")
    result = fit_box_layout(
        [(box(0, 0, 1000, 1000), 3)], [(0, 0, 900, 1000, 3)],
        recipes={}, densities={1: 1.0, 3: 3.0}, milp_backend="highs", threads=1,
        highs_options=highs_log_options(path),
    )
    assert result["is_feasible"] and result["status"] != "Already covered"
    _log_written(path)


def test_fit_without_highs_options_writes_nothing(tmp_path):
    """No options => no log file; keeps the default silent behaviour."""

    from A101.reinforcement_components import fit_component_frontier

    problem, cfg, field_geometry = tiny_problem(max_n=4)
    fitted = fit_component_frontier(
        problem, _deficient_solver_result(2), recipes=cfg.get("recipes"), densities=cfg["densities"],
        diameters=cfg["diameters"], steps=cfg["steps"], axis="y", field=field_geometry,
        min_width=300.0, fit_milp_backend="highs", fit_threads=1,
    )
    assert fitted[2]["is_feasible"]
    assert list(tmp_path.iterdir()) == []
