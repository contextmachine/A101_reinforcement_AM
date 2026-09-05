from __future__ import annotations

import time

from rebar_service.config import Settings
from rebar_service.store import RedisStore


def make_store() -> RedisStore:
    store = RedisStore.__new__(RedisStore)
    store.settings = Settings()
    class MemoryRedis:
        def __init__(self):
            self.data = {}
            self.sets = {}
            self.zsets = {}
        def pipeline(self, transaction=False): return self
        def execute(self): return []
        def set(self, key, value, **kwargs): self.data[key] = value; return True
        def get(self, key): return self.data.get(key)
        def delete(self, *keys):
            for key in keys: self.data.pop(key, None); self.sets.pop(key, None); self.zsets.pop(key, None)
            return 1
        def mget(self, keys): return [self.data.get(key) for key in keys]
        def sadd(self, key, *values): self.sets.setdefault(key, set()).update(values); return len(values)
        def smembers(self, key): return set(self.sets.get(key, set()))
        def incr(self, key):
            value = int(self.data.get(key, 0)) + 1; self.data[key] = value; return value
        def expireat(self, *args, **kwargs): return True
        def expire(self, *args, **kwargs): return True
        def zadd(self, key, mapping): self.zsets.setdefault(key, {}).update(mapping); return len(mapping)
        def zrange(self, key, start, stop):
            rows = sorted(self.zsets.get(key, {}).items(), key=lambda item: item[1])
            ids = [item[0] for item in rows]
            return ids[start:] if stop == -1 else ids[start:stop+1]
    store.redis = MemoryRedis()
    store._task_expire_at = lambda task_id: int(time.time()) + 3600  # type: ignore[method-assign]
    return store


def test_raw_and_smooth_fields_and_components_coexist():
    store = make_store()

    store.save_field("task", {"tag": "raw"}, variant="raw")
    store.save_field("task", {"tag": "smooth"}, variant="smooth")
    store.save_component("task", 0, {"info": {"id": 0, "tag": "raw"}}, variant="raw")
    store.save_component("task", 0, {"info": {"id": 0, "tag": "smooth"}}, variant="smooth")

    assert store.load_field("task", variant="raw")["tag"] == "raw"
    assert store.load_field("task", variant="smooth")["tag"] == "smooth"
    assert store.load_component("task", 0, variant="raw")["info"]["tag"] == "raw"
    assert store.load_component("task", 0, variant="smooth")["info"]["tag"] == "smooth"
    assert store.component_ids("task", variant="raw") == ["0"]
    assert store.component_ids("task", variant="smooth") == ["0"]


def test_raw_and_smooth_frontiers_have_independent_versions():
    store = make_store()

    store.save_frontier_result("task", 0, 2, {"n": 2, "variant": "raw"}, variant="raw")
    store.save_frontier_result("task", 0, 2, {"n": 2, "variant": "smooth"}, variant="smooth")

    assert store.load_frontier("task", 0, variant="raw")[2]["variant"] == "raw"
    assert store.load_frontier("task", 0, variant="smooth")[2]["variant"] == "smooth"
    assert store.frontier_version("task", variant="raw") == 1
    assert store.frontier_version("task", variant="smooth") == 1


def test_solution_variant_filtering_keeps_raw_and_smooth_separate():
    store = make_store()
    base = {
        "source": "components",
        "total_N": 3,
        "is_feasible": True,
        "actual_mass_kg": 10.0,
        "proxy_mass": 11.0,
    }
    store.save_solution("task", {**base, "solution_id": "raw-s", "variant": "raw", "smooth": False})
    store.save_solution("task", {**base, "solution_id": "smooth-s", "variant": "smooth", "smooth": True, "actual_mass_kg": 9.0})

    assert [r["solution_id"] for r in store.solutions("task", variant="raw")] == ["raw-s"]
    assert [r["solution_id"] for r in store.solutions("task", variant="smooth")] == ["smooth-s"]
    assert store.best_solution("task", 3, variant="raw")["solution_id"] == "raw-s"
