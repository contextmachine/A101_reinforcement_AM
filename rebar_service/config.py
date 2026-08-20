from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="REBAR_", extra="ignore")

    redis_url: str = "redis://localhost:6379/0"
    redis_address: str = "localhost:6379"
    redis_password: str = ""
    api_key: str = ""

    task_ttl_seconds: int = 2 * 24 * 3600
    event_maxlen: int = 10_000
    blob_chunk_bytes: int = 8 * 1024 * 1024
    max_upload_bytes: int = 64 * 1024 * 1024
    max_planned_n_values: int = 10_000
    max_n_value: int = 100_000
    max_solver_threads: int = 4
    max_solver_timeout_seconds: float = 21_600.0

    ready_queue: str = "rebar:jobs:ready"
    processing_queue: str = "rebar:jobs:processing"
    workload_queue: str = "rebar:jobs:workload"
    worker_claim_timeout_seconds: int = 5
    job_lease_seconds: int = 90
    max_jobs_per_task: int = 4
    global_max_jobs: int = 32
    schedule_window_factor: int = 1

    local_cache_dir: str = "/tmp/rebar-cache"
    local_cache_items: int = 3

    default_solver_timeout: float = 120.0
    default_solver_threads: int = 1
    default_emit_interval: float = 5.0
    default_prepared_max_n: int = 0

    cors_origins: str = "*"
    log_level: str = "INFO"
    service_name: str = "rebar-optimizer"
    image_tag: str = "dev"

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [x.strip() for x in self.cors_origins.split(",") if x.strip()]

    @property
    def schedule_window(self) -> int:
        return max(1, self.max_jobs_per_task * self.schedule_window_factor)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
