from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="REBAR_", extra="ignore")

    redis_url: str = "redis://localhost:6379/0"
    queue_state_ttl_seconds: int = 30 * 24 * 3600
    v2_queue_prefix: str = "rebar:v2"

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
    max_n_value: int = 100_000
    # v1 compatibility caps remain configurable instead of hidden API literals.
    legacy_task_request_max_n: int = 250
    legacy_prepared_max_n_override: int = 100
    solver_hard_max_n: int = 1000
    prepare_problem_max_n: int = 1000
    prepare_max_dense_cells: int = 20_000

    ready_queue: str = "rebar:jobs:ready"
    processing_queue: str = "rebar:jobs:processing"
    workload_queue: str = "rebar:jobs:workload"
    worker_claim_timeout_seconds: int = 5
    job_lease_seconds: int = 90
    max_jobs_per_task: int = 28_031_998
    max_concurrent_solvers_per_task: int = 32
    v2_worker_stage: str | None = None
    schedule_window_factor: int = 1

    default_solver_threads: int = 1
    max_solver_threads: int = 4
    max_solver_timeout_seconds: float | None = None
    max_solver_time_limit_seconds: float | None = None
    solver_timeout: float | None = None
    solver_time_limit: float | None = None
    solver_backend: str = "highs"
    require_optimal: bool = False
    fit_time_limit: float | None = None
    fit_milp_backend: str = "auto"
    default_steel_density_kg_m3: float = 7850.0
    solver_log_bucket: str | None = None
    solver_log_prefix: str | None = None
    solver_log_access_key: str | None = None
    solver_log_secret_key: str | None = None

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
        "max_solver_timeout_seconds",
        "max_solver_time_limit_seconds",
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

    def v2_queue_names(self, stage: str) -> tuple[str, str, str]:
        stage = str(stage).strip().lower()
        if stage not in {"preparing", "solving", "fitting", "baring", "validation"}:
            raise ValueError(f"Unknown v2 worker stage: {stage}")
        root = f"{self.v2_queue_prefix.rstrip(':')}:{stage}"
        return f"{root}:ready", f"{root}:processing", f"{root}:workload"

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
    def schedule_window(self) -> int:
        return max(1, self.max_jobs_per_task * self.schedule_window_factor)

    def effective_solver_max_n(self) -> int:
        return max(1, min(int(self.max_n_value), int(self.solver_hard_max_n)))

    def effective_prepare_max_n(self) -> int:
        return max(1, min(self.effective_solver_max_n(), int(self.prepare_problem_max_n)))

    def effective_solver_time_limit(self, requested: object = None) -> float | None:
        if requested is None:
            value = self.solver_time_limit
        else:
            value = float(requested)
        if value is None:
            return None
        if float(value) <= 0:
            raise ValueError("solver_time_limit must be > 0")
        cap = self.max_solver_time_limit_seconds
        if cap is not None and float(value) > float(cap):
            raise ValueError(f"solver_time_limit={value} exceeds server limit {cap}")
        return float(value)

    def effective_threads(self, requested: object = None) -> int:
        try:
            value = int(requested)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            value = self.default_solver_threads
        return max(1, min(value, self.max_solver_threads))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
