import re
from pathlib import Path

from rebar_service.models import TaskParameters


EXPECTED_OLD_ROUTES = {
    ("POST", "/v1/tasks"),
    ("POST", "/v1/tasks/upload"),
    ("GET", "/v1/tasks/{task_id}"),
    ("GET", "/v1/tasks/{task_id}/source-polygons"),
    ("GET", "/v1/tasks/{task_id}/results"),
    ("GET", "/v1/tasks/{task_id}/results/{n}"),
    ("GET", "/v1/tasks/{task_id}/results/{n}/dxf"),
    ("GET", "/v1/tasks/{task_id}/events"),
    ("POST", "/v1/tasks/{task_id}/n"),
    ("POST", "/v1/tasks/{task_id}/cancel"),
    ("POST", "/v1/tasks/{task_id}/pause"),
    ("POST", "/v1/tasks/{task_id}/resume"),
    ("WS", "/v1/tasks/{task_id}/ws"),
}

EXPECTED_COMPONENT_ROUTES = {
    ("GET", "/v1/tasks/{task_id}/components"),
    ("GET", "/v1/tasks/{task_id}/components/{component_id}"),
    ("POST", "/v1/tasks/{task_id}/components/{component_id}/n"),
    ("GET", "/v1/tasks/{task_id}/components/{component_id}/results"),
    ("GET", "/v1/tasks/{task_id}/components/{component_id}/results/{n}"),
    ("GET", "/v1/tasks/{task_id}/solutions"),
    ("GET", "/v1/tasks/{task_id}/solutions/{solution_id}"),
    ("POST", "/v1/tasks/{task_id}/components/prepare"),
    ("GET", "/v1/tasks/{task_id}/component-events"),
}


def _declared_routes() -> set[tuple[str, str]]:
    root = Path(__file__).resolve().parents[1]
    source = (root / "rebar_service/api.py").read_text(encoding="utf-8")
    routes: set[tuple[str, str]] = set()
    for method, path in re.findall(r'@app\.(get|post|websocket)\("([^"]+)"', source):
        routes.add(("WS" if method == "websocket" else method.upper(), path))
    return routes


def test_canonical_api_exposes_old_and_component_solution_routes():
    routes = _declared_routes()
    assert EXPECTED_OLD_ROUTES <= routes
    assert EXPECTED_COMPONENT_ROUTES <= routes


def test_task_parameters_include_current_pipeline_options():
    params = TaskParameters.model_validate(
        {
            "n": [1, 2, 3],
            "scan_mode": "hard",
            "whole": True,
            "component_result_top_k": 7,
            "validate_results": True,
        }
    )
    assert params.scan_mode == "hard"
    assert params.whole is True
    assert params.component_result_top_k == 7
    assert params.validate_results is True


def test_duplicate_service_modules_are_removed():
    root = Path(__file__).resolve().parents[1] / "rebar_service"
    removed = {
        "component_api.py",
        "component_workflow.py",
        "component_jobs.py",
        "component_source.py",
        "component_models.py",
        "component_store.py",
        "component_config.py",
        "run3_app.py",
        "universal_worker.py",
        "legacy_bridge.py",
        "result_adapter.py",
    }
    assert not [name for name in sorted(removed) if (root / name).exists()]


def test_production_manifests_use_canonical_api_worker_and_one_queue():
    root = Path(__file__).resolve().parents[1]
    api = (root / "deploy/k8s/base/api.yaml").read_text(encoding="utf-8")
    worker = (root / "deploy/k8s/base/worker-deployment.yaml").read_text(encoding="utf-8")
    configmap = (root / "deploy/k8s/base/configmap.yaml").read_text(encoding="utf-8")
    prod_keda = (root / "deploy/k8s/overlays/prod/worker-scaledobject.yaml").read_text(encoding="utf-8")
    dev_keda = (root / "deploy/k8s/overlays/dev/worker-scaledobject.yaml").read_text(encoding="utf-8")

    assert "rebar_service.api:app" in api
    assert "rebar_service.run3_app" not in api
    assert "rebar_service.worker" in worker
    assert "universal_worker" not in worker
    assert "REBAR_COMPONENT_READY_QUEUE" not in configmap
    assert "REBAR_COMPONENT_PROCESSING_QUEUE" not in configmap
    assert "REBAR_COMPONENT_WORKLOAD_QUEUE" not in configmap
    assert "REBAR_RUN_LEGACY_WORKER" not in configmap
    assert "rebar:component:" not in prod_keda
    assert "rebar:component:" not in dev_keda
    assert prod_keda.count("type: redis") == 1
    assert dev_keda.count("type: redis") == 1


def test_run3_is_local_only_and_not_a_production_entrypoint():
    root = Path(__file__).resolve().parents[1]
    run3 = (root / "run3.py").read_text(encoding="utf-8")
    api_manifest = (root / "deploy/k8s/base/api.yaml").read_text(encoding="utf-8")
    worker_manifest = (root / "deploy/k8s/base/worker-deployment.yaml").read_text(encoding="utf-8")
    assert "run3_app" not in run3
    assert "universal_worker" not in run3
    assert "sub.add_parser(\"api\"" not in run3
    assert "sub.add_parser(\"worker\"" not in run3
    assert "run3.py" not in api_manifest
    assert "run3.py" not in worker_manifest


def test_am_super_branch_ci_and_deploy_defaults_are_preserved():
    root = Path(__file__).resolve().parents[1]
    ci = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    deploy = (root / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    prod = (root / "deploy/k8s/overlays/prod/kustomization.yaml").read_text(encoding="utf-8")
    dev = (root / "deploy/k8s/overlays/dev/kustomization.yaml").read_text(encoding="utf-8")

    assert "am-super-branch" in ci
    assert "default: am-super-branch" in deploy
    assert "newTag: am-super-branch" in prod
    assert "newTag: am-super-branch" in dev
