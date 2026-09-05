from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
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
    max_planned_n_values: int = 10_000
    max_n_value: int = 100_000

    ready_queue: str = "rebar:jobs:ready"
    processing_queue: str = "rebar:jobs:processing"
    workload_queue: str = "rebar:jobs:workload"
    worker_claim_timeout_seconds: int = 5
    job_lease_seconds: int = 90
    max_jobs_per_task: int = 28_031_998
    schedule_window_factor: int = 1

    default_solver_threads: int = 1
    max_solver_threads: int = 4
    max_solver_timeout_seconds: float | None = None
    solver_timeout: float | None = None
    solver_time_limit: float | None = None
    solver_backend: str = "highs"
    require_optimal: bool = False
    fit_time_limit: float | None = None
    fit_milp_backend: str = "auto"

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
    def schedule_window(self) -> int:
        return max(1, self.max_jobs_per_task * self.schedule_window_factor)

    def effective_threads(self, requested: object = None) -> int:
        try:
            value = int(requested)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            value = self.default_solver_threads
        return max(1, min(value, self.max_solver_threads))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
