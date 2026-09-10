from pathlib import Path


def test_upload_routes_are_split_by_source_format():
    source = (Path(__file__).resolve().parents[1] / "rebar_service/api.py").read_text(encoding="utf-8")
    assert '@app.post("/v1/tasks/upload"' in source
    assert '@app.post("/v1/tasks/tables_upload"' in source
    assert '@app.post("/v1/tasks/json_upload"' in source
    assert '@app.post("/v1/tasks/pickle_upload"' in source

    start = source.index('async def create_task_upload(')
    end = source.index('@app.post("/v1/tasks/tables_upload"', start)
    dxf_upload = source[start:end]
    assert "nodes_file" not in dxf_upload
    assert "elements_file" not in dxf_upload
    assert "loads_file" not in dxf_upload
    assert "load_column" not in dxf_upload
    assert "dxf_only=True" in dxf_upload


def test_tables_upload_keeps_xlsx_parsing_out_of_async_route_body():
    source = (Path(__file__).resolve().parents[1] / "rebar_service/api.py").read_text(encoding="utf-8")
    start = source.index('async def create_task_tables_upload(')
    end = source.index('@app.post("/v1/tasks/json_upload"', start)
    body = source[start:end]
    assert "source_polygons_from_xlsx" not in body
    assert "kind\": \"xlsx_tables\"" in body


def test_pipeline_has_source_materialization_job():
    source = (Path(__file__).resolve().parents[1] / "rebar_service/pipeline.py").read_text(encoding="utf-8")
    assert 'materialize_source = "materialize_source"' in source
    assert "def handle_materialize_source" in source
