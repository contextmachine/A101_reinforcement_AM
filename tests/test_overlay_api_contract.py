from pathlib import Path


def test_api_exposes_overlay_journal_and_removes_prepare_endpoint():
    source = (Path(__file__).resolve().parents[1] / "rebar_service/api.py").read_text(encoding="utf-8")
    assert '@app.post("/v1/tasks/{task_id}/overlays")' in source
    assert '@app.get("/v1/tasks/{task_id}/overlays")' in source
    assert '@app.post("/v1/tasks/{task_id}/components/prepare"' not in source


def test_analysis_routes_accept_overlay_and_source_polygons_accepts_smooth_overlay():
    source = (Path(__file__).resolve().parents[1] / "rebar_service/api.py").read_text(encoding="utf-8")
    assert "async def get_source_polygons(task_id: str, smooth: bool = Query(False), overlay: int = Query(0, ge=0))" in source
    for marker in (
        "async def list_components(task_id: str, smooth: bool = Query(False), overlay: int = Query(0, ge=0))",
        "async def schedule_component_n(task_id: str, component_id: int, body: ComponentNRequest, smooth: bool = Query(False), overlay: int = Query(0, ge=0))",
        "async def list_component_results(task_id: str, component_id: int, smooth: bool = Query(False), overlay: int = Query(0, ge=0))",
        "async def get_component_result(task_id: str, component_id: int, n: int, smooth: bool = Query(False), overlay: int = Query(0, ge=0))",
        "async def list_results(task_id: str, smooth: bool | None = Query(None), overlay: int = Query(0, ge=0))",
        "async def get_result(task_id: str, n: int, smooth: bool | None = Query(None), overlay: int = Query(0, ge=0))",
        "async def get_result_dxf(task_id: str, n: int, smooth: bool | None = Query(None), overlay: int = Query(0, ge=0))",
        "async def add_n(task_id: str, mutation: NMutation, smooth: bool = Query(False), overlay: int = Query(0, ge=0))",
    ):
        assert marker in source


def test_upload_load_column_default_remains_empty():
    source = (Path(__file__).resolve().parents[1] / "rebar_service/api.py").read_text(encoding="utf-8")
    assert "load_column: Annotated[int | None, Form(ge=1, le=4)] = None" in source


def test_cancel_and_websocket_commands_can_scope_n_to_overlay_analysis():
    api = (Path(__file__).resolve().parents[1] / "rebar_service/api.py").read_text(encoding="utf-8")
    models = (Path(__file__).resolve().parents[1] / "rebar_service/models.py").read_text(encoding="utf-8")
    assert "async def cancel(task_id: str, mutation: CancelMutation, smooth: bool = Query(False), overlay: int = Query(0, ge=0))" in api
    assert "overlay: int = Field(default=0, ge=0)" in models


def test_overlay_event_request_defaults_real_false_and_deduplicates_indices():
    from rebar_service.models import OverlayEventMutation
    row = OverlayEventMutation.model_validate({"type": "clean", "idxs": [3, 3, 4], "id": 123})
    assert row.real is False
    assert row.idxs == [3, 4]


def test_component_results_expose_normalized_status():
    source = (Path(__file__).resolve().parents[1] / "rebar_service/api.py").read_text(encoding="utf-8")
    marker = '"status": row.get("status"),'
    assert marker in source
