from pathlib import Path

import yaml


def test_all_yaml_manifests_parse():
    root = Path(__file__).resolve().parents[1]
    files = sorted(root.glob("deploy/**/*.yaml")) + sorted((root / ".github/workflows").glob("*.yml"))
    assert files
    for path in files:
        assert list(yaml.safe_load_all(path.read_text(encoding="utf-8")))


def test_readonly_image_uses_writable_numba_cache():
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    assert "NUMBA_CACHE_DIR=/tmp/numba-cache" in dockerfile


def test_ci_uses_available_setup_python_major():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "actions/setup-python@v6" in workflow
    assert "actions/setup-python@v7" not in workflow


def test_readme_pins_current_keda_patch_and_tested_kubernetes_window():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert "--version 2.20.2" in readme
    assert "1.33–1.35" in readme


def test_deploy_uses_current_setup_kubectl_major():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "azure/setup-kubectl@v5" in workflow


def test_worker_does_not_self_exit_on_idle_timeout():
    root = Path(__file__).resolve().parents[1]
    worker = (root / "rebar_service/worker.py").read_text(encoding="utf-8")
    assert "time.monotonic() - last_activity >= settings.worker_idle_seconds" not in worker
    assert "signal.signal(signal.SIGTERM" in worker
