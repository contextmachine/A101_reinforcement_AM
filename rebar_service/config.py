from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


APP_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """Every deployment-control knob of the service.

    The Kubernetes ConfigMap ``rebar-config`` is the single source of these values:
    nothing here may be supplied through the HTTP API, and nothing here is capped by a
    hard-coded literal elsewhere in the code base.
    """

    model_config = SettingsConfigDict(env_file=".env", env_prefix="REBAR_", extra="ignore")

    redis_url: str = "redis://localhost:6379/0"
    queue_state_ttl_seconds: int = 30 * 24 * 3600

    postgres_host: str = "a101-postgres"
    postgres_port: int = 5432
    postgres_db: str = "a101"
    postgres_schema: str = "rebar"
    postgres_user: str = "a101"
    postgres_password: str = ""
    postgres_sslmode: str = "disable"
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_pool_recycle_seconds: int = 1800
    event_poll_interval_seconds: float = 0.5

    max_upload_bytes: int = 64 * 1024 * 1024
    max_source_polygons: int = 100_000
    max_planned_n_values: int = 10_000
    # Single ceiling for N: caps ``max_useful_n`` (the exact max-N MILP) and validates
    # requested N values. Replaces the historical 100/250/1000 literals.
    max_n: int = 1000

    # Main (reinforcement-selection) worker queue.
    ready_queue: str = "rebar:jobs:ready"
    processing_queue: str = "rebar:jobs:processing"
    workload_queue: str = "rebar:jobs:workload"
    # Isolated /v2/bars worker queue.
    bars_ready_queue: str = "rebar:bars:ready"
    bars_processing_queue: str = "rebar:bars:processing"
    bars_workload_queue: str = "rebar:bars:workload"
    # Isolated /v2/verification worker queue.
    verification_ready_queue: str = "rebar:verification:ready"
    verification_processing_queue: str = "rebar:verification:processing"
    verification_workload_queue: str = "rebar:verification:workload"

    worker_claim_timeout_seconds: int = 5
    job_lease_seconds: int = 90
    max_jobs_per_task: int = 28_031_998
    # KEDA ScaledJob workers process until their queue is empty, then exit.
    worker_exit_when_idle: bool = False

    # HiGHS threads for the reinforcement-selection MILP (0 lets HiGHS decide).
    solver_threads: int = 1
    # Threads for the fit MILP.
    fit_threads: int = 1
    # Hard wall-clock kill timeout of one solver subprocess; None = unlimited.
    solver_timeout: float | None = None
    # Default HiGHS ``time_limit`` for one N; a task may override it via
    # ``config.solver.solver_time_limit``.
    solver_time_limit: float | None = None
    solver_backend: str = "highs"
    require_optimal: bool = False
    fit_time_limit: float | None = None
    fit_milp_backend: str = "auto"

    # Directory that receives HiGHS logs as {task_id}/{n}/{worker-id}.log.
    # Empty string means ``<app-directory>/logs``.
    solver_log_dir: str = ""

    grid_size: float = 300.0
    fill_notches: float = 1000.0
    short_edge: float = 300.0
    simplify_step: float = 1000.0
    use_mosaic: bool = True
    min_internal_step: float = 100.0
    scheduler_batch_size: int = 256
    combine_batch_size: int = 256
    frontier_top_k: int = 5

    cors_origins: str = "*"

    @field_validator(
        "solver_timeout",
        "solver_time_limit",
        "fit_time_limit",
        mode="before",
    )
    @classmethod
    def parse_optional_timeout(cls, value):
        if value is None:
            return None
        if isinstance(value, str) and value.strip().lower() in {"", "none", "null", "off", "unlimited"}:
            return None
        return value

    @property
    def database_url(self):
        from sqlalchemy.engine import URL

        return URL.create(
            drivername="postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
            query={"sslmode": self.postgres_sslmode},
        )

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [x.strip() for x in self.cors_origins.split(",") if x.strip()]

    @property
    def solver_log_path(self) -> Path:
        value = (self.solver_log_dir or "").strip()
        return Path(value) if value else APP_DIR / "logs"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
