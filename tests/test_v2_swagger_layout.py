from __future__ import annotations

import logging

from fastapi.testclient import TestClient

import rebar_service.api as api


client = TestClient(api.app)


def test_default_swagger_uses_v2_only_schema_and_legacy_docs_remain_available():
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "/openapi-v2.json" in docs.text
    assert client.get("/docs/legacy").status_code == 200

    v2 = client.get("/openapi-v2.json").json()
    assert v2["info"]["title"] == "rebar-v2-api"
    assert all(not path.startswith("/v1/") for path in v2["paths"])
    assert "/v2/tasks" in v2["paths"]


def test_full_openapi_keeps_v1_last_and_moves_long_legacy_description_to_legacy_tag():
    schema = client.get("/openapi.json").json()
    paths = list(schema["paths"])
    first_v1 = min(i for i, path in enumerate(paths) if path.startswith("/v1/"))
    last_non_v1 = max(i for i, path in enumerate(paths) if not path.startswith("/v1/"))
    assert first_v1 > last_non_v1
    assert "Рекомендуемый сценарий работы" not in schema["info"].get("description", "")
    legacy = next(tag for tag in schema["tags"] if tag["name"].startswith("99."))
    assert "Рекомендуемый сценарий работы" in legacy["description"]


def test_v2_task_put_paths_precede_task_get_paths():
    schema = client.get("/openapi-v2.json").json()
    paths = list(schema["paths"])
    assert paths.index("/v2/tasks") < paths.index("/v2/tasks/{task_id}/n")
    assert paths.index("/v2/tasks/{task_id}/n") < paths.index("/v2/tasks/{task_id}/cancel")
    assert paths.index("/v2/tasks/{task_id}/cancel") < paths.index("/v2/tasks/{task_id}")
    assert paths.index("/v2/tasks/{task_id}") < paths.index("/v2/tasks/{task_id}/{n}")


def test_status_poll_access_filter_hides_successful_gets_but_keeps_errors():
    filt = api.StatusPollAccessFilter(enabled=False)
    ok = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "%s", (), None)
    ok.args = ("127.0.0.1:1", "GET", "/v2/tasks/abc", "1.1", 200)
    assert filt.filter(ok) is False

    failed = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "%s", (), None)
    failed.args = ("127.0.0.1:1", "GET", "/v2/tasks/abc", "1.1", 500)
    assert filt.filter(failed) is True

    put = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "%s", (), None)
    put.args = ("127.0.0.1:1", "PUT", "/v2/tasks", "1.1", 200)
    assert filt.filter(put) is True
