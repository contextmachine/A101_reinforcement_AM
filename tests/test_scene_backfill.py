from contextlib import contextmanager
from types import SimpleNamespace


def test_backfill_dry_run_never_enqueues_and_apply_queues_only_pending_scenes():
    from rebar_service.scene_backfill import backfill_scenes
    class DB:
        @contextmanager
        def connect(self): yield self
        def execute(self, query, params):
            assert "needs_component_backfill" in str(query)
            assert params['limit'] == 20
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: ['old-1', 'old-2']))
    queued = []
    workflow = SimpleNamespace(enqueue_scene_materialization=lambda sid: queued.append(sid) or True)
    store = SimpleNamespace(database=DB())
    preview = backfill_scenes(store, workflow, limit=20)
    assert preview['selected'] == 2 and preview['queued'] == 0 and not queued
    result = backfill_scenes(store, workflow, limit=20, apply=True)
    assert result['queued'] == 2 and queued == ['old-1', 'old-2']
