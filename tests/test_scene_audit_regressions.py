"""Cross-layer regressions found during the second scene-package audit."""
import json
from contextlib import contextmanager
import numpy as np
import pytest
from fastapi.testclient import TestClient
from rebar_service import api
from rebar_service.config import Settings
from rebar_service.models import TaskCreated, CompactVerificationRequest
from rebar_service.postgres_store import PostgresStore
from rebar_service.pipeline import PipelineWorkflow, PipelineJob
from A101.max_n_milp import estimate_max_useful_n

class Rows:
    def __init__(self, values=()): self.values = list(values)
    def mappings(self): return self
    def first(self): return self.values[0] if self.values else None
    def all(self): return self.values

class ComponentDB:
    """DB transport double; the production store serializes/deserializes."""
    def __init__(self): self.row = None
    @contextmanager
    def begin(self): yield self
    @contextmanager
    def connect(self): yield self
    def execute(self, sql, params):
        if 'INSERT INTO components' in str(sql):
            self.row = {**params, 'n_bounds': json.loads(params['n_bounds']) if params['n_bounds'] else None}
            return Rows()
        if 'SELECT * FROM components' in str(sql): return Rows([self.row])
        raise AssertionError(str(sql))

def test_max_n_status_and_diagnostics_survive_real_store_roundtrip():
    db = ComponentDB()
    store = PostgresStore(Settings(), database=db)
    store._load_artifact = lambda *a, **kw: {}
    store.save_component('task', 0, {
        'state': 'prepared', 'max_useful_n': 3,
        'max_n_state': 'ready', 'max_n_milp': {'feasible': True, 'max_useful_n': 3, 'layers': []},
        'bounds': {'lower_bound': 1}, 'plan': [1, 3, 2],
    })
    loaded = store.load_component('task', 0)
    assert loaded.get('max_n_state') == 'ready'
    assert loaded.get('max_n_milp', {}).get('max_useful_n') == 3

def test_put_task_accepts_nested_upload_style_config_and_resolves_overlay(monkeypatch):
    seen = {}
    monkeypatch.setattr(api.store, 'get_scene', lambda sid: {'scene_id': sid, 'state': 'ready', 'components': [{'id': 0}]})
    monkeypatch.setattr(api.store, 'resolve_scene_overlay_id', lambda sid, v: 999)
    monkeypatch.setattr(api.store, 'load_scene_variant_polygons', lambda *a, **kw: [])
    def build(parameters, input_obj, **kw):
        seen.update(parameters=parameters, **kw)
        return TaskCreated(task_id='task', scene_id='scene', state='queued', overlay_id=999, smooth=True, websocket_url='ws', status_url='status')
    monkeypatch.setattr(api, '_build_task', build)
    response = TestClient(api.app).put('/v1/tasks', json={
        'scene_id': 'scene', 'overlay_id': -1, 'smooth': True, 'components': [-3], 'n': [1, 4],
        'config': {'anchor_factor': 44, 'axis': 'x', 'max_layers': 2},
    })
    assert response.status_code == 200, response.text
    assert seen['parameters'].anchor_factor == 44
    assert seen['parameters'].axis == 'x'
    assert seen['parameters'].whole is False
    assert response.json()['overlay_id'] == 999

def test_upload_honors_explicit_whole_false_inside_existing_config(monkeypatch):
    seen = []
    def build(parameters, input_obj, **kw):
        seen.append(parameters.whole)
        return TaskCreated(task_id='t', state='queued', websocket_url='w', status_url='s')
    monkeypatch.setattr(api, '_build_task', build)
    client = TestClient(api.app)
    for config, expected in [({'n': [1], 'whole': False}, False), ({'n': [1]}, True)]:
        response = client.post('/v1/tasks/upload', data={'config': json.dumps(config)}, files={'file': ('a.dxf', b'0\nEOF\n')})
        assert response.status_code == 200, response.text
        assert seen[-1] is expected

def test_new_task_n1_does_not_bypass_prepared_solver(monkeypatch):
    import A101.reinforcement_components as rc
    calls = []
    class Store:
        def get_meta(self, tid): return {'scene_id': 'scene', 'parameters': {'solver': {}}}
        def load_problem(self, *a, **kw): return {'problem': {'strict_physical_candidates': True}}
        def is_n_cancelled(self, *a, **kw): return False
        def save_solver_result(self, *a, **kw): pass
    wf = PipelineWorkflow(Store(), Settings())
    wf._single_component_frontier = lambda *a, **kw: pytest.fail('N=1 cannot bypass physical candidate constraints')
    wf.enqueue = lambda *a, **kw: True
    wf._publish = lambda *a, **kw: None
    monkeypatch.setattr(rc, 'solve_component_frontier', lambda *a, **kw: (calls.append(a[1]) or ({}, {})))
    wf.handle_solve_component(PipelineJob('solve_component', 'task', {'component_id': 0, 'n': 1, 'variant': 'raw'}))
    assert calls == [[1]]

def test_max_n_uses_complete_matrix_cover_not_pruned_main_candidates():
    problem = {'work_matrix': np.array([[1, 1]]), 'selectable_rectangles': [(0, 0, 0, 0, 1), (1, 0, 1, 0, 1)]}
    result = estimate_max_useful_n(problem, hard_cap=100)
    assert result['max_useful_n'] == 1

def test_max_n_does_not_skip_infeasible_later_mask_after_reaching_cap():
    problem = {'work_matrix': np.array([[1, 2]]), 'work_physical_mask': np.array([[True, False]]),
               'selectable_rectangles': [(0, 0, 0, 0, 1)]}
    result = estimate_max_useful_n(problem, hard_cap=1)
    assert result['feasible'] is False

def test_preparation_barrier_waits_for_expected_but_not_yet_inserted_component():
    class Store:
        def analysis_state(self, *a, **kw): return {'preparation_state': 'preparing'}
        def mark_analysis_prepared(self, *a, **kw): pytest.fail('premature analysis_prepared')
        def load_field(self, *a, **kw): return {'expected_solver_units': [0, 1]}
        def component_ids(self, *a, **kw): return ['0']
        def load_component(self, *a, **kw): return {'max_n_state': 'ready', 'max_useful_n': 2}
        def get_meta(self, *a, **kw): return {'component_selection': [-3]}
    wf = PipelineWorkflow(Store(), Settings())
    assert wf._maybe_complete_analysis('task', 'raw', False) is False

@pytest.mark.parametrize('field,value', [('d', float('inf')), ('direction', [float('nan'), 0]), ('origin', [float('inf'), 0])])
def test_verification_rejects_nonfinite_geometry(field, value):
    from pydantic import ValidationError
    zone = {'origin': [0, 0], 'direction': [1, 0], 'length': 1, 'step': 1, 'left': 0, 'right': 0, 'd': 20}
    zone[field] = value
    with pytest.raises(ValidationError):
        CompactVerificationRequest.model_validate({'scene_id': 'scene', 'zones': [zone]})

def test_prepare_infeasible_cover_is_domain_state_not_unhandled_error():
    import A101.reinforcement_components as rc
    assert hasattr(rc, 'CandidateCoverInfeasible')
    saved = []
    class Store:
        def load_component(self, *a, **kw): return {'component': {'id': 0}, 'state': 'queued'}
        def save_component(self, *a, **kw): saved.append(a[2])
    wf = PipelineWorkflow(Store(), Settings())
    def impossible(*a, **kw): raise rc.CandidateCoverInfeasible('no physical cover')
    wf._prepare_problem = impossible
    wf._publish = lambda *a, **kw: None
    wf._maybe_complete_analysis = lambda *a, **kw: False
    wf.handle_prepare_component(PipelineJob('prepare_component', 'task', {'component_id': 0, 'analysis_auto_solve': True}))
    assert saved[-1]['max_n_state'] == 'infeasible'
    assert saved[-1]['max_useful_n'] == 0

def test_put_ready_scene_does_not_reparse_or_resmooth_it(monkeypatch):
    seen = {}
    monkeypatch.setattr(api.store, 'get_scene', lambda sid: {'scene_id': sid, 'state': 'ready', 'components': [{'id': 0}]})
    monkeypatch.setattr(api.store, 'resolve_scene_overlay_id', lambda *a: 0)
    monkeypatch.setattr(api.store, 'load_scene_variant_polygons', lambda *a, **kw: pytest.fail('API must pass scene reference, not polygons'))
    def build(parameters, input_obj, **kw):
        seen.update(input_obj)
        return TaskCreated(task_id='task', scene_id='scene', state='queued', websocket_url='w', status_url='s')
    monkeypatch.setattr(api, '_build_task', build)
    response = TestClient(api.app).put('/v1/tasks', json={'scene_id': 'scene', 'n': [1], 'config': {}})
    assert response.status_code == 200, response.text
    assert seen == {'kind': 'scene_ref', 'scene_id': 'scene'}

def test_old_layout_without_unclipped_tracks_is_not_mislabeled_as_unclipped():
    from rebar_service.compact_zones import compact_zones_from_layout
    with pytest.raises(ValueError, match='unclipped'):
        compact_zones_from_layout({'zones': [{'diameter': 20, 'step': 150, 'bars': [[0, 0, 0, 100]]}]})

def test_max_n_solver_timeout_is_not_reported_as_infeasible(monkeypatch):
    from types import SimpleNamespace
    import A101.max_n_milp as m
    monkeypatch.setattr(m, 'milp', lambda **kw: SimpleNamespace(status=1, success=False, x=None, message='time limit'))
    with pytest.raises(m.MaxNEstimationError):
        m.minimum_rectangle_cover(np.ones((1, 2), bool))

def test_scene_reference_input_reads_original_dxf_not_polygon_wrapper():
    from rebar_service.codec import sha256
    class DB:
        @contextmanager
        def connect(self): yield self
        def execute(self, sql, params):
            if 'FROM task_sources' in str(sql):
                return Rows([{'kind': 'scene_ref', 'metadata': {'scene_id': 's'}}])
            return Rows([{'kind': 'dxf', 'filename': 'original.dxf', 'content': b'dxf-bytes', 'sha256': sha256(b'dxf-bytes'), 'metadata': {}}])
    obj = PostgresStore(Settings(), database=DB()).get_object('t', 'input')
    assert obj['kind'] == 'dxf' and obj['content'] == b'dxf-bytes'

def test_lightest_feasible_solution_wins_over_heavier_optimal():
    # Execute the production ORDER BY against a real SQL engine, not a mock sorter.
    from sqlalchemy import create_engine, text
    e = create_engine('sqlite://')
    with e.begin() as conn:
        conn.execute(text('CREATE TABLE solutions (task_id TEXT, variant TEXT, overlay_id INTEGER, total_n INTEGER, source TEXT, is_feasible BOOLEAN, is_optimal BOOLEAN, actual_mass_kg REAL, proxy_mass REAL, created_at INTEGER, result TEXT)'))
        for opt,mass,name in [(True,200,'heavy'),(False,100,'light')]:
            conn.execute(text('INSERT INTO solutions VALUES ("t","raw",0,1,"whole",1,:opt,:mass,0,0,:result)'),
                         {'opt':opt,'mass':mass,'result':json.dumps({'solution_id':name})})
    class DB:
        @contextmanager
        def connect(self):
            with e.connect() as conn: yield conn
    result = PostgresStore(Settings(), database=DB()).best_solution('t', 1, variant='raw', overlay_id=0)
    assert result['solution_id'] == 'light'

def test_scene_task_creation_copies_polygons_in_sql_not_python():
    calls=[]
    class DB:
        @contextmanager
        def begin(self):yield self
        @contextmanager
        def connect(self):yield self
        def execute(self,sql,params):calls.append((str(sql),params));return Rows()
    store=PostgresStore(Settings(),database=DB())
    store.get_scene=lambda scene_id:{'state':'ready'}
    store.load_scene_variant_polygons=lambda *a,**k:pytest.fail('must not load polygon arrays in API')
    store.create_task('t',{'scene_id':'s','analysis_variant':'smooth','initial_variant':'smooth','analysis_overlay_id':99},
                      {'order':[1,2]}, {'kind':'scene_ref','scene_id':'s'})
    variants=[(sql,p) for sql,p in calls if 'INSERT INTO task_variants' in sql]
    analyses=[(sql,p) for sql,p in calls if 'INSERT INTO task_analyses' in sql]
    assert len(variants)==2 and all('FROM scene_variants' in sql for sql,p in variants)
    assert len(analyses)==1 and analyses[0][1]['variant']=='smooth' and analyses[0][1]['overlay_id']==99

def test_legacy_solution_id_preserves_its_original_context(monkeypatch):
    monkeypatch.setattr(api.store, 'get_meta', lambda tid: {'scene_id': tid, 'initial_variant': 'raw'})
    monkeypatch.setattr(api.store, 'resolve_scene_overlay_id', lambda sid, value: value)
    def load(tid, sid, overlay_id=None):
        if overlay_id is not None and overlay_id != 987:
            return None
        return {'solution_id': sid, 'variant': 'smooth', 'overlay_id': 987, 'total_N': 4}
    monkeypatch.setattr(api.store, 'load_solution', load)
    response = TestClient(api.app).get('/v1/tasks/legacy/solutions/original-smooth')
    assert response.status_code == 200, response.text
    assert response.json()['variant'] == 'smooth'
    assert response.json()['overlay_id'] == 987


def test_all_infeasible_components_finish_as_domain_infeasibility():
    observed = []
    class Store:
        def analysis_state(self, *a, **kw): return {'preparation_state': 'preparing'}
        def mark_analysis_prepared(self, *a, **kw): pytest.fail('infeasible is not prepared')
        def mark_analysis_infeasible(self, *a, **kw): observed.append(('infeasible', kw))
        def load_field(self, *a, **kw): return {'expected_solver_units': [0]}
        def component_ids(self, *a, **kw): return ['0']
        def load_component(self, *a, **kw): return {'max_n_state': 'infeasible', 'max_useful_n': 0}
        def get_meta(self, *a, **kw): return {'component_selection': [-3], 'requested_n': [1]}
        def requested_ns(self, *a, **kw): return [1]
        def set_n_status(self, *a, **kw): observed.append(('n', a))
    wf = PipelineWorkflow(Store(), Settings())
    wf._publish = lambda *a, **kw: None
    assert wf._maybe_complete_analysis('task', 'raw', True)
    assert observed[0][0] == 'infeasible'
    assert observed[1][1][2] == 'infeasible'
