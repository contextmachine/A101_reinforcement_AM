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


def test_api_and_worker_map_existing_postgres_secret_into_rebar_env():
    root = Path(__file__).resolve().parents[1]
    for rel in ("deploy/k8s/base/api.yaml", "deploy/k8s/base/worker-deployment.yaml"):
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


def test_v2_worker_deployments_are_stage_isolated_and_db_pools_are_bounded():
    root = Path(__file__).resolve().parents[1]
    path = root / "deploy/k8s/base/v2-worker-deployments.yaml"
    assert path.exists()
    docs = [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]
    deployments = {doc["metadata"]["name"]: doc for doc in docs if doc.get("kind") == "Deployment"}
    expected = {f"rebar-v2-{stage}" for stage in ("preparing", "solving", "fitting", "baring", "validation")}
    assert set(deployments) == expected
    for stage in ("preparing", "solving", "fitting", "baring", "validation"):
        deployment = deployments[f"rebar-v2-{stage}"]
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        assert container["command"] == ["python", "-m", "rebar_service.v2_worker"]
        env = {row["name"]: row.get("value") for row in container.get("env", []) if "value" in row}
        assert env["REBAR_V2_WORKER_STAGE"] == stage
        assert env["REBAR_DB_POOL_SIZE"] == "1"
        assert env["REBAR_DB_MAX_OVERFLOW"] == "0"


def test_v2_keda_scaledobjects_watch_only_their_stage_workload_queues():
    root = Path(__file__).resolve().parents[1]
    for overlay in ("dev", "prod"):
        path = root / f"deploy/k8s/overlays/{overlay}/v2-worker-scaledobjects.yaml"
        assert path.exists()
        docs = [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]
        scaled = {doc["metadata"]["name"]: doc for doc in docs}
        for stage in ("preparing", "solving", "fitting", "baring", "validation"):
            row = scaled[f"rebar-v2-{stage}"]
            assert row["spec"]["scaleTargetRef"]["name"] == f"rebar-v2-{stage}"
            assert row["spec"]["triggers"][0]["metadata"]["listName"] == f"rebar:v2:api-v2:{stage}:workload"
            assert int(row["spec"]["maxReplicaCount"]) >= 32


def test_configmap_centralizes_v2_runtime_limits_without_enabling_s3_logging_by_default():
    root = Path(__file__).resolve().parents[1]
    text = (root / "deploy/k8s/base/configmap.yaml").read_text(encoding="utf-8")
    for key in (
        "REBAR_V2_QUEUE_PREFIX",
        "REBAR_MAX_CONCURRENT_SOLVERS_PER_TASK",
        "REBAR_SOLVER_HARD_MAX_N",
        "REBAR_PREPARE_PROBLEM_MAX_N",
        "REBAR_PREPARE_MAX_DENSE_CELLS",
        "REBAR_DEFAULT_STEEL_DENSITY_KG_M3",
    ):
        assert f"{key}:" in text
    # Null/absent S3 config is the feature flag: a default deploy must not create solver logs.
    assert "REBAR_SOLVER_LOG_BUCKET:" not in text
    assert "REBAR_SOLVER_LOG_PREFIX:" not in text
    assert "REBAR_SOLVER_LOG_ACCESS_KEY:" not in text
    assert "REBAR_SOLVER_LOG_SECRET_KEY:" not in text


def test_prod_private_patches_image_pull_secret_for_all_v2_workers():
    root = Path(__file__).resolve().parents[1]
    path = root / "deploy/k8s/overlays/prod-private/v2-workers-image-pull-secret-patch.yaml"
    assert path.exists()
    docs = [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]
    names = {doc["metadata"]["name"] for doc in docs}
    assert names == {f"rebar-v2-{stage}" for stage in ("preparing", "solving", "fitting", "baring", "validation")}
    for doc in docs:
        assert doc["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": "ghcr-pull"}]
    kust = yaml.safe_load((root / "deploy/k8s/overlays/prod-private/kustomization.yaml").read_text(encoding="utf-8"))
    assert any(row.get("path") == "v2-workers-image-pull-secret-patch.yaml" for row in kust.get("patches", []))


def test_side_by_side_v2_api_manifest_has_dedicated_name_config_and_startup_probe():
    root = Path(__file__).resolve().parents[1]
    text = (root / "deploy/k8s/base/rebar-v2-api.yaml").read_text(encoding="utf-8")
    assert "name: rebar-v2-api" in text
    assert "app: rebar-v2-api" in text
    assert "name: rebar-config-v2" in text
    assert "startupProbe:" in text


def test_v2_redis_policy_allows_api_and_all_worker_stages():
    root = Path(__file__).resolve().parents[1]
    text = (root / "deploy/k8s/base/rebar-v2-redis-networkpolicy.yaml").read_text(encoding="utf-8")
    for app in (
        "rebar-v2-api", "rebar-v2-preparing", "rebar-v2-solving",
        "rebar-v2-fitting", "rebar-v2-baring", "rebar-v2-validation",
    ):
        assert f"- {app}" in text
    assert "port: 6379" in text


def test_v2_worker_scaledobjects_keep_completed_pods_observable_longer():
    root = Path(__file__).resolve().parents[1]
    for overlay in ("dev", "prod"):
        text = (root / f"deploy/k8s/overlays/{overlay}/v2-worker-scaledobjects.yaml").read_text(encoding="utf-8")
        assert text.count("cooldownPeriod: 600") == 5
