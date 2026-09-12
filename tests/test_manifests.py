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


SCALEDJOBS = tuple(
    f"deploy/k8s/overlays/{overlay}/{name}-scaledjob.yaml"
    for overlay in ("dev", "prod")
    for name in ("bars", "verification")
)


def _load(root: Path, rel: str):
    return yaml.safe_load((root / rel).read_text(encoding="utf-8"))


def _job_container(doc) -> dict:
    containers = doc["spec"]["jobTargetRef"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    return containers[0]


def test_api_and_worker_map_existing_postgres_secret_into_rebar_env():
    root = Path(__file__).resolve().parents[1]
    rels = ("deploy/k8s/base/api.yaml", "deploy/k8s/base/worker-deployment.yaml") + SCALEDJOBS
    for rel in rels:
        text = (root / rel).read_text(encoding="utf-8")
        assert "REBAR_POSTGRES_USER" in text
        assert "REBAR_POSTGRES_PASSWORD" in text
        assert "a101-postgres-auth" in text
        assert "POSTGRES_USER" in text
        assert "POSTGRES_PASSWORD" in text


def test_configmap_points_to_existing_postgres_service_and_has_no_durable_redis_ttl_knobs():
    root = Path(__file__).resolve().parents[1]
    text = (root / "deploy/k8s/base/configmap.yaml").read_text(encoding="utf-8")
    assert "REBAR_POSTGRES_HOST: a101-postgres" in text
    assert "REBAR_POSTGRES_PORT: '5432'" in text
    assert "REBAR_POSTGRES_DB: a101" in text
    assert "REBAR_TASK_TTL_SECONDS" not in text
    assert "REBAR_BLOB_CHUNK_BYTES" not in text
    assert "REBAR_EVENT_MAXLEN" not in text


def test_repository_contains_safe_redis_secret_example():
    root = Path(__file__).resolve().parents[1]
    example_path = root / "deploy/k8s/secrets/rebar-secrets.dev.example.yaml"
    assert example_path.exists()
    doc = yaml.safe_load(example_path.read_text(encoding="utf-8"))
    values = doc["stringData"]
    assert values["REBAR_REDIS_PASSWORD"] == "<replace-me>"
    assert "<replace-me>" in values["REBAR_REDIS_URL"]


def test_database_migration_job_runs_alembic_with_existing_postgres_secret():
    root = Path(__file__).resolve().parents[1]
    text = (root / "deploy/k8s/base/db-migrate-job.yaml").read_text(encoding="utf-8")
    assert "kind: Job" in text
    assert "name: rebar-db-migrate" in text
    assert "alembic" in text
    assert "upgrade" in text
    assert "head" in text
    assert "REBAR_POSTGRES_HOST" in text
    assert "a101-postgres" in text
    assert "a101-postgres-auth" in text
    assert "POSTGRES_USER" in text
    assert "POSTGRES_PASSWORD" in text


def test_clean_cutover_script_requires_explicit_redis_flush_confirmation():
    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts/cutover-postgres.sh").read_text(encoding="utf-8")
    assert "CONFIRM_REDIS_FLUSH=YES" in text
    assert "delete scaledobject rebar-worker" in text
    assert "scale deployment/rebar-worker --replicas=0" in text
    assert "clear-redis.sh" in text
    assert "migrate-db.sh" in text
    assert "deploy-k8s.sh" in text
    assert text.index("migrate-db.sh") < text.index("clear-redis.sh") < text.index("deploy-k8s.sh")


def test_clear_redis_script_flushes_only_selected_database():
    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts/clear-redis.sh").read_text(encoding="utf-8")
    assert "app=rebar-redis" in text
    assert "FLUSHDB" in text
    assert "FLUSHALL" not in text


def test_operator_docs_cover_postgres_cutover_and_verification():
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "PostgreSQL" in readme
    assert "a101-postgres" in readme
    assert "a101-postgres-auth" in readme
    assert "cutover-postgres.sh" in readme
    assert "CONFIRM_REDIS_FLUSH=YES" in readme
    assert "alembic_version" in readme
    assert "task_variants" in readme


def test_verify_script_checks_offline_alembic_sql():
    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts/verify.sh").read_text(encoding="utf-8")
    assert "alembic upgrade head --sql" in text
    assert "kubectl kustomize" in text


def test_worker_uses_single_connection_pool_to_avoid_keda_connection_explosion():
    root = Path(__file__).resolve().parents[1]
    worker = (root / "deploy/k8s/base/worker-deployment.yaml").read_text(encoding="utf-8")
    assert "name: REBAR_DB_POOL_SIZE\n          value: \"1\"" in worker
    assert "name: REBAR_DB_MAX_OVERFLOW\n          value: \"0\"" in worker


def test_api_default_database_pool_is_bounded_for_single_postgres_pod():
    root = Path(__file__).resolve().parents[1]
    config = (root / "deploy/k8s/base/configmap.yaml").read_text(encoding="utf-8")
    assert "REBAR_DB_POOL_SIZE: '5'" in config
    assert "REBAR_DB_MAX_OVERFLOW: '5'" in config


def test_solver_log_pvc_is_declared_in_base_and_mounted_only_by_the_main_worker():
    root = Path(__file__).resolve().parents[1]
    pvc = _load(root, "deploy/k8s/base/logs-pvc.yaml")
    assert pvc["kind"] == "PersistentVolumeClaim"
    assert pvc["metadata"]["name"] == "rebar-solver-logs"
    assert pvc["spec"]["storageClassName"] == "csi-s3"
    assert pvc["spec"]["accessModes"] == ["ReadWriteMany"]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "50Gi"

    base = _load(root, "deploy/k8s/base/kustomization.yaml")
    assert "logs-pvc.yaml" in base["resources"]

    pod = _load(root, "deploy/k8s/base/worker-deployment.yaml")["spec"]["template"]["spec"]
    container = pod["containers"][0]
    mounts = [m for m in container["volumeMounts"] if m["mountPath"] == "/app/logs"]
    assert len(mounts) == 1
    assert mounts[0]["subPath"] == "rebar-optimizer/logs"
    assert mounts[0]["name"] == "solver-logs"
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["solver-logs"]["persistentVolumeClaim"]["claimName"] == "rebar-solver-logs"

    # The Dockerfile must create the mount point so the read-only rootfs image can write logs.
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "/app/logs" in dockerfile

    for rel in SCALEDJOBS:
        container = _job_container(_load(root, rel))
        assert not [m for m in container["volumeMounts"] if m["mountPath"] == "/app/logs"]


def test_bars_and_verification_scaledjobs_are_registered_in_both_keda_overlays():
    root = Path(__file__).resolve().parents[1]
    expected = {
        "bars": ("rebar-bars-worker", "rebar:bars:workload", "rebar_service.bars_worker"),
        "verification": (
            "rebar-verification-worker",
            "rebar:verification:workload",
            "rebar_service.verification_worker",
        ),
    }
    for overlay in ("dev", "prod"):
        kustomization = _load(root, f"deploy/k8s/overlays/{overlay}/kustomization.yaml")
        for kind, (name, queue, module) in expected.items():
            rel = f"deploy/k8s/overlays/{overlay}/{kind}-scaledjob.yaml"
            assert f"{kind}-scaledjob.yaml" in kustomization["resources"]
            doc = _load(root, rel)
            assert doc["apiVersion"] == "keda.sh/v1alpha1"
            assert doc["kind"] == "ScaledJob"
            assert doc["metadata"]["name"] == name
            assert doc["spec"]["pollingInterval"] == 5
            assert doc["spec"]["maxReplicaCount"] == 8

            target = doc["spec"]["jobTargetRef"]
            assert target["backoffLimit"] == 0
            assert target["activeDeadlineSeconds"] == 21600
            assert target["ttlSecondsAfterFinished"] == 300

            pod = target["template"]["spec"]
            assert pod["restartPolicy"] == "Never"
            assert [s["name"] for s in pod["imagePullSecrets"]] == ["ghcr-secret"]
            assert pod["securityContext"]["runAsNonRoot"] is True
            assert pod["tolerations"][0]["value"] == "heavy-jobs"
            volumes = {v["name"] for v in pod["volumes"]}
            assert "tmp" in volumes

            container = _job_container(doc)
            assert container["image"].endswith("a101_reinforcement_am:am-super-branch")
            assert container["command"] == ["python", "-m", module]
            assert container["securityContext"]["readOnlyRootFilesystem"] is True
            assert "/tmp" in [m["mountPath"] for m in container["volumeMounts"]]
            sources = [
                next(iter(item.values()))["name"] for item in container["envFrom"]
            ]
            assert sources == ["rebar-config", "rebar-secrets"]
            env = {e["name"]: e for e in container["env"]}
            assert env["REBAR_DB_POOL_SIZE"]["value"] == "1"
            assert env["REBAR_DB_MAX_OVERFLOW"]["value"] == "0"
            assert env["REBAR_WORKER_EXIT_WHEN_IDLE"]["value"] == "true"

            trigger = doc["spec"]["triggers"][0]
            assert trigger["type"] == "redis"
            assert trigger["metadata"]["address"].startswith("rebar-redis.rebar-optimizer")
            assert trigger["metadata"]["listName"] == queue
            assert trigger["metadata"]["listLength"] == "1"
            assert trigger["metadata"]["activationListLength"] == "0"
            assert trigger["authenticationRef"]["name"] == "rebar-redis-auth"


def test_deploy_script_reports_the_isolated_v2_workers():
    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts/deploy-k8s.sh").read_text(encoding="utf-8")
    assert "scaledjob/rebar-bars-worker" in text
    assert "scaledjob/rebar-verification-worker" in text
    assert "pvc/rebar-solver-logs" in text
