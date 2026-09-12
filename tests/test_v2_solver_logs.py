from pathlib import Path

import rebar_service.solver_logs as logs
from rebar_service.config import Settings


def test_solver_log_capture_is_fully_disabled_when_s3_config_is_null(monkeypatch, tmp_path):
    created = []
    monkeypatch.setattr(logs.tempfile, "mkstemp", lambda *a, **k: created.append(True) or (_ for _ in ()).throw(AssertionError("must not create temp file")))
    settings = Settings(
        solver_log_bucket=None,
        solver_log_prefix=None,
        solver_log_access_key=None,
        solver_log_secret_key=None,
    )
    capture = logs.SolverLogCapture(settings, task_id="t", n=4, attempt=2)
    with capture as active:
        assert active.highs_options == {}
        assert active.local_path is None
    assert created == []
    assert capture.object_key is None


def test_solver_log_capture_passes_unique_log_file_and_uploads_after_run(monkeypatch, tmp_path):
    local = tmp_path / "highs.log"
    monkeypatch.setattr(logs.tempfile, "mkstemp", lambda **kwargs: (local.open("wb").fileno(), str(local)))
    # mkstemp monkeypatch above cannot safely keep descriptor alive; use helper monkeypatch instead.
    created = []
    def fake_create():
        local.write_text("")
        created.append(str(local))
        return str(local)
    monkeypatch.setattr(logs, "_create_temp_log", fake_create)
    uploaded = []
    monkeypatch.setattr(logs, "_upload_solver_log", lambda settings, path, key: uploaded.append((path, key)))
    settings = Settings(
        solver_log_bucket="bucket",
        solver_log_prefix="solver-logs",
        solver_log_access_key="key",
        solver_log_secret_key="secret",
    )
    capture = logs.SolverLogCapture(settings, task_id="task", n=7, attempt=3)
    with capture as active:
        assert active.highs_options == {"log_file": str(local)}
        Path(active.local_path).write_text("solver output")
    assert created == [str(local)]
    assert uploaded == [(str(local), "solver-logs/task/n-7/attempt-3.log")]
    assert capture.object_key == "solver-logs/task/n-7/attempt-3.log"
    assert not local.exists()


def test_solve_component_frontier_forwards_highs_log_option(monkeypatch):
    import A101.rectangle_solver_job as jobmod
    from A101.reinforcement_components import solve_component_frontier

    seen = {}
    def fake_job(**kwargs):
        seen.update(kwargs)
        return ({"n": kwargs["N"], "is_feasible": False, "status": "Infeasible", "infeasibility_proved": True}, {})
    monkeypatch.setattr(jobmod, "solve_rectangle_job", fake_job)
    solve_component_frontier(
        {"prepared": {"x": 1}}, [3], backend="highs", highs_options={"log_file": "/tmp/highs.log"}
    )
    assert seen["highs_options"] == {"log_file": "/tmp/highs.log"}
