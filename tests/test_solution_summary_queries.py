from pathlib import Path

from rebar_service.config import Settings
from rebar_service.postgres_store import PostgresStore


class _SummaryOnlyStore(PostgresStore):
    def __init__(self):
        super().__init__(Settings())

    def get_meta(self, task_id):
        return {"task_id": task_id, "initial_variant": "smooth"}

    def solution_summaries(self, task_id, **kwargs):
        return [
            {
                "solution_id": "s1",
                "variant": "smooth",
                "overlay_id": 0,
                "source": "components",
                "total_N": 3,
                "component_ns": {"0": 3},
                "proxy_mass": 12.0,
                "actual_mass_kg": 10.0,
                "is_feasible": True,
                "is_optimal": True,
                "status": "optimal",
            }
        ]

    def solutions(self, *args, **kwargs):
        raise AssertionError("metadata/list paths must not load full solution JSONB")


def test_result_metadata_uses_solution_summaries_not_full_results():
    store = _SummaryOnlyStore()
    rows = store.get_result_metas("task", variant="smooth", overlay_id=0)
    assert rows["3"]["solution_id"] == "s1"
    assert rows["3"]["is_optimal"] is True


def test_api_solution_list_uses_summary_query():
    source = (Path(__file__).resolve().parents[1] / "rebar_service/api.py").read_text(encoding="utf-8")
    start = source.index("async def list_solutions(")
    body = source[start : source.index("@app.get(\"/v1/tasks/{task_id}/solutions/{solution_id}\")", start)]
    assert "store.solution_summaries" in body
    assert "store.solutions(" not in body


def test_api_memory_limit_is_four_gib():
    text = (Path(__file__).resolve().parents[1] / "deploy/k8s/base/api.yaml").read_text(encoding="utf-8")
    assert "memory: 1Gi" in text
    assert "memory: 4Gi" in text
