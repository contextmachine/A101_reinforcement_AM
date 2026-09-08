import pytest
from fastapi.testclient import TestClient
from rebar_service import api
from rebar_service.models import TaskCreated


@pytest.mark.parametrize("endpoint", ["upload", "tables_upload", "json_upload", "pickle_upload"])
def test_upload_without_required_file_is_422_not_500(endpoint):
    client = TestClient(api.app, raise_server_exceptions=False)
    response = client.post(f"/v1/tasks/{endpoint}", data={"config": '{"n":[1]}'})
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(row["type"] == "missing" for row in errors)


def test_tables_upload_reads_three_files_without_parsing_them_in_api(monkeypatch):
    seen = {}
    async def finish(**kwargs):
        seen.update(kwargs)
        return TaskCreated(task_id="t", state="uploaded", websocket_url="/ws", status_url="/status")
    monkeypatch.setattr(api, "_finish_source_upload", finish)
    client = TestClient(api.app, raise_server_exceptions=False)
    response = client.post("/v1/tasks/tables_upload?start=false", files={
        "nodes_file": ("nodes.xlsx", b"nodes"),
        "elements_file": ("elements.xlsx", b"elements"),
        "loads_file": ("loads.xlsx", b"loads"),
    }, data={"load_column": "2"})
    assert response.status_code == 200, response.text
    assert seen["input_obj"]["kind"] == "xlsx_tables"
    assert seen["input_obj"]["content"]
    assert seen["input_obj"]["load_column"] == 2
    assert seen["start"] is False


def test_tables_upload_rejects_wrong_extension():
    client = TestClient(api.app, raise_server_exceptions=False)
    response = client.post("/v1/tasks/tables_upload?start=false", files={
        "nodes_file": ("nodes.csv", b"nodes"),
        "elements_file": ("elements.xlsx", b"elements"),
        "loads_file": ("loads.xlsx", b"loads"),
    })
    assert response.status_code == 415
