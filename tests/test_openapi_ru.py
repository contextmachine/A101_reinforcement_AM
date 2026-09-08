import json
import re

from rebar_service.api import app
from rebar_service.models import TaskParameters


def test_every_http_operation_has_russian_summary_description_and_group():
    schema = app.openapi()
    for path, methods in schema["paths"].items():
        for method, op in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            assert re.search("[А-Яа-я]", op.get("summary", "")), (method, path)
            assert re.search("[А-Яа-я]", op.get("description", "")), (method, path)
            assert op.get("tags"), (method, path)
            for p in op.get("parameters", []):
                assert re.search("[А-Яа-я]", p.get("description", "")), (path, p["name"])


def test_swagger_documents_actual_smooth_defaults_and_no_prepare_endpoint():
    paths = app.openapi()["paths"]
    assert "/v1/tasks/{task_id}/components/prepare" not in paths
    def description(path):
        return next(p for p in paths[path]["get"]["parameters"] if p["name"] == "smooth")["description"]
    assert "оба" in description("/v1/tasks/{task_id}/solutions")
    assert "initial_variant" in description("/v1/tasks/{task_id}/results")
    assert "raw" in description("/v1/tasks/{task_id}/source-polygons")


def test_multipart_config_has_valid_json_example_not_a_python_dict():
    s = app.openapi()
    for path in ("upload", "tables_upload", "json_upload", "pickle_upload"):
        ref = s["paths"][f"/v1/tasks/{path}"]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"]
        body = s["components"]["schemas"][ref.rsplit("/", 1)[-1]]
        conf = body["properties"]["config"]
        TaskParameters.model_validate(json.loads(conf["examples"][0]))
        assert "строк" in conf["description"]
        assert body["properties"]["file"]["format"] == "binary" if "file" in body["properties"] else "load_column" in body["properties"]


def test_mass_and_async_rules_are_in_openapi_and_schema_is_cached():
    s = app.openapi()
    assert "with_anchorage_unclipped_kg" in s["info"]["description"]
    assert "materialize_source" in s["paths"]["/v1/tasks/upload"]["post"]["description"]
    assert "OFFSET" in s["paths"]["/v1/tasks/{task_id}/component-events"]["get"]["description"]
    assert "WebSocket" in s["info"]["description"]
    assert s is app.openapi()


def test_openapi_and_swagger_are_served_and_local_references_resolve():
    from fastapi.testclient import TestClient
    client = TestClient(app)
    assert client.get("/docs").status_code == 200
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    def walk(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if ref and ref.startswith("#/"):
                target = schema
                for token in ref[2:].split("/"):
                    target = target[token.replace("~1", "/").replace("~0", "~")]
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(schema)
