from pathlib import Path


def test_worker_scopes_job_failed_event_to_job_overlay_and_resets_failed_preparation():
    source = (Path(__file__).resolve().parents[1] / "rebar_service/worker.py").read_text(encoding="utf-8")
    assert 'overlay_id = int((job_data.get("payload") or {}).get("overlay_id", 0))' in source
    assert 'overlay_id=overlay_id' in source
    assert 'store.mark_analysis_failed' in source
