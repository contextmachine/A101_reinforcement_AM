import pytest

from rebar_service.config import Settings
from rebar_service.redis_queue import RedisQueue
from rebar_service.v2_jobs import V2Job


def test_v2_queue_names_are_stage_isolated():
    settings = Settings(v2_queue_prefix="rebar:v2")
    assert settings.v2_queue_names("preparing") == (
        "rebar:v2:preparing:ready",
        "rebar:v2:preparing:processing",
        "rebar:v2:preparing:workload",
    )
    assert settings.v2_queue_names("solving")[0] != settings.v2_queue_names("fitting")[0]


def test_redis_queue_can_be_bound_to_nonlegacy_names_without_mutating_settings():
    settings = Settings()
    queue = RedisQueue(
        settings,
        ready_queue="x:ready",
        processing_queue="x:processing",
        workload_queue="x:workload",
    )
    assert queue.ready_queue == "x:ready"
    assert queue.processing_queue == "x:processing"
    assert queue.workload_queue == "x:workload"
    assert settings.ready_queue == "rebar:jobs:ready"


def test_v2_job_has_one_stage_and_attempt_in_dedupe_coordinate():
    first = V2Job(stage="solving", kind="solve", task_id="t", n=50, attempt=1)
    retry = V2Job(stage="solving", kind="solve", task_id="t", n=50, attempt=2)
    assert first.stage == "solving"
    assert first.job_id != retry.job_id
    assert first.dedupe_key != retry.dedupe_key

    with pytest.raises(ValueError):
        V2Job(stage="fitting", kind="solve", task_id="t", n=50, attempt=1)
