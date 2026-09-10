"""Real geometry + real SciPy/HiGHS solver + real fit/layout; in-memory IO only."""
import pytest
from rebar_service.pipeline import PipelineWorkflow, PipelineJob
from support.memory_scene_store import MemoryStore

@pytest.mark.parametrize('axis', ['x','y'])
def test_scene_prepare_max_n_solve_fit_combine_layout_end_to_end(axis):
    store=MemoryStore(axis)
    wf=PipelineWorkflow(store,store.settings)
    wf.prepare_task_components('task', auto_solve=True)
    count=0
    while store.jobs:
        job=store.jobs.popleft();count+=1
        assert count <= 100
        wf.dispatch(PipelineJob.from_value(job))
        store.dedupe.discard(job['dedupe_key'])
    assert store.analysis['preparation_state']=='prepared'
    kinds=[j['kind'] for j in store.enqueued]
    first_solve=next(i for i,k in enumerate(kinds) if k.startswith('solve_'))
    assert all(i < first_solve for i,k in enumerate(kinds) if k.startswith('compute_max_n'))
    assert any(s['source']=='components' and s['is_feasible'] for s in store.solutions.values())
    real_components = [cid for cid in store.components if cid != 'whole']
    if len(real_components) == 1:
        assert not any(s['source']=='whole' for s in store.solutions.values())
        assert not any(j['kind'] in {'prepare_whole','compute_max_n_whole','solve_whole','fit_whole'} for j in store.enqueued)
    else:
        assert any(s['source']=='whole' and s['is_feasible'] for s in store.solutions.values())
        assert any(j['kind'] == 'prepare_whole' for j in store.enqueued)
        assert any(j['kind'] == 'solve_whole' for j in store.enqueued)
    for result in store.solutions.values():
        if not result['is_feasible']:continue
        assert result['compact_zones']
        masses=result['mass_metrics']
        assert masses['with_anchorage_unclipped_kg'] >= masses['with_anchorage_kg']-1e-8
        assert result['overlay_id']==0


def test_background_only_scene_does_not_crash_whole_preparation():
    store = MemoryStore('x')
    for row in store.rows:
        row['load'] = 5.7
    store.mark_analysis_infeasible = lambda *a, **kw: store.analysis.update(preparation_state='infeasible')
    wf = PipelineWorkflow(store, store.settings)
    wf.prepare_task_components('task', auto_solve=True)
    while store.jobs:
        job = store.jobs.popleft()
        wf.dispatch(PipelineJob.from_value(job))
        store.dedupe.discard(job['dedupe_key'])
    assert store.analysis['preparation_state'] == 'infeasible'
    assert store.components == {}
    assert wf.aggregate_component_info('task')['max_useful_n'] == 0
    assert not any(j['kind'].startswith('solve_') for j in store.enqueued)
    assert any(event == 'analysis_infeasible' and p['reason'] == 'no_positive_n_required' for event, p in store.events)
