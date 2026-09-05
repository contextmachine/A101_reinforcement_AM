from __future__ import annotations

from pathlib import Path


def test_analysis_launch_routes_publish_smooth_selector():
    source = (Path(__file__).resolve().parents[1] / "rebar_service/api.py").read_text(encoding="utf-8")

    assert "async def create_task(request: TaskCreate, smooth: bool = Query(False))" in source
    assert "start: bool = Query(True)" in source
    assert "smooth: bool = Query(False)" in source
    assert "async def prepare_existing_task(task_id: str, smooth: bool = Query(False))" in source
    assert "async def schedule_component_n(task_id: str, component_id: int, body: ComponentNRequest, smooth: bool = Query(False))" in source
    assert "async def add_n(task_id: str, mutation: NMutation, smooth: bool = Query(False))" in source


def test_component_read_routes_and_solution_list_can_select_smooth_variant():
    source = (Path(__file__).resolve().parents[1] / "rebar_service/api.py").read_text(encoding="utf-8")

    assert "async def list_components(task_id: str, smooth: bool = Query(False))" in source
    assert "async def get_component(task_id: str, component_id: int, smooth: bool = Query(False))" in source
    assert "async def list_component_results(task_id: str, component_id: int, smooth: bool = Query(False))" in source
    assert "async def get_component_result(task_id: str, component_id: int, n: int, smooth: bool = Query(False))" in source
    assert "smooth: bool | None = Query(None)" in source
