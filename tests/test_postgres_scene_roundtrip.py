"""Optional REAL PostgreSQL migration/storage test in a unique disposable schema.

Run only against a test database:
    REBAR_TEST_POSTGRES_DSN=postgresql+psycopg://... python -m pytest tests/test_postgres_scene_roundtrip.py -v
Never sets or changes public/rebar search_path defaults. No production data copied.
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

DSN = os.environ.get('REBAR_TEST_POSTGRES_DSN')
pytestmark = pytest.mark.skipif(not DSN, reason='explicit REBAR_TEST_POSTGRES_DSN test database required')


def test_real_postgres_0002_to_head_preserves_history_and_storage_roundtrip(monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from rebar_service import config as settings_module
    from rebar_service.codec import sha256
    from rebar_service.config import Settings
    from rebar_service.postgres_store import PostgresStore

    schema = 'rebar_audit_' + uuid.uuid4().hex
    url = make_url(DSN).set(drivername='postgresql+psycopg')
    engine = create_engine(url, connect_args={'options': f'-c search_path={schema},pg_catalog'})
    root = Path(__file__).resolve().parents[1]
    ac = Config(str(root / 'alembic.ini'))
    ac.set_main_option('script_location', str(root / 'migrations'))
    monkeypatch.setattr(settings_module, 'get_settings', lambda: SimpleNamespace(postgres_schema=schema, database_url=url))

    class DB:
        @contextmanager
        def connect(self):
            with engine.connect() as c:
                yield c
        @contextmanager
        def begin(self):
            with engine.begin() as c:
                yield c

    legacy = '1' * 32
    sid = '2' * 40
    scene_task = '3' * 32
    raw = [{'points': [[0, 0], [2000, 0], [2000, 2000], [0, 2000]], 'load': 27.0}]
    smooth = [{**raw[0], 'load': 19.0}]
    source = json.dumps({'polygons': raw}).encode('utf-8')
    try:
        command.upgrade(ac, '0002_overlay_analyses')
        with engine.begin() as c:
            c.execute(text("""INSERT INTO tasks
                (id,state,parameters,n_mode,n_source,scan_mode,component_result_top_k,max_concurrent_jobs,initial_variant)
                VALUES (:t,'completed','{}','list','[1]','requested',5,4,'smooth')"""), {'t': legacy})
            c.execute(text("""INSERT INTO task_sources (task_id,kind,filename,content,sha256)
                VALUES (:t,'json','scene.json',:b,:h)"""), {'t': legacy, 'b': source, 'h': sha256(source)})
            for variant, polygons in [('raw', raw), ('smooth', smooth)]:
                c.execute(text("""INSERT INTO task_variants (task_id,variant,polygons)
                    VALUES (:t,:v,CAST(:p AS jsonb))"""), {'t': legacy, 'v': variant, 'p': json.dumps(polygons)})
                c.execute(text("""INSERT INTO task_analyses (task_id,variant,overlay_id,preparation_state)
                    VALUES (:t,:v,0,:state)"""), {'t': legacy, 'v': variant, 'state': 'prepared' if variant == 'smooth' else 'stored'})
            c.execute(text("""INSERT INTO solutions
                (solution_id,task_id,variant,overlay_id,source,total_n,component_ns,proxy_mass,actual_mass_kg,
                 is_feasible,is_optimal,status,result)
                VALUES (:s,:t,'smooth',0,'whole',1,'{"whole":1}',10,123,true,true,'optimal',CAST(:r AS jsonb))"""),
                {'s': sid, 't': legacy, 'r': json.dumps({'solution_id': sid, 'variant': 'smooth', 'overlay_id': 0,
                                                      'total_N': 1, 'actual_mass_kg': 123.0, 'is_feasible': True})})
            c.execute(text("""INSERT INTO task_events (task_id,event_type,payload)
                VALUES (:t,'task_created','{"variant":"smooth"}')"""), {'t': legacy})
        command.upgrade(ac, 'head')
        with engine.connect() as c:
            assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one() == '0004_scene_audit_repairs'
            assert c.execute(text('SELECT count(*) FROM solutions WHERE solution_id=:s'), {'s': sid}).scalar_one() == 1
            assert c.execute(text("SELECT count(*) FROM solutions WHERE task_id=:t AND variant='raw' AND overlay_id=0"), {'t': legacy}).scalar_one() == 1
            assert c.execute(text("SELECT preparation_state FROM task_analyses WHERE task_id=:t AND variant='raw' AND overlay_id=0"), {'t': legacy}).scalar_one() == 'prepared'
            metadata = c.execute(text('SELECT metadata FROM scenes WHERE id=:t'), {'t': legacy}).scalar_one()
            assert metadata['legacy_canonical_source'] == {'variant': 'smooth', 'overlay_id': 0}
            assert metadata['needs_component_backfill'] is True
        store = PostgresStore(Settings(postgres_schema=schema), database=DB())
        assert store.ensure_scene_variants(legacy) is True
        assert store.ensure_scene_variants(legacy) is False
        assert store.scene_components(legacy)[0]['polygon_indices'] == [0]
        assert store.resolved_source_polygons(legacy, variant='raw', overlay_id=0)[0]['load'] == 19.0
        assert store.load_scene_variant_polygons(legacy, variant='raw')[0]['load'] == 27.0
        store.append_scene_overlay_events(legacy, [{'id': 999, 'type': 'clean', 'idxs': [0], 'real': True}])
        assert store.resolve_scene_overlay_id(legacy, -1) == 999
        store.create_task(scene_task, {'scene_id': legacy, 'initial_variant': 'smooth', 'analysis_variant': 'smooth',
                                      'analysis_overlay_id': 999, 'component_selection': [-3], 'parameters': {}},
                          {'order': [1]}, {'kind': 'scene_ref', 'scene_id': legacy})
        store.save_component(scene_task, 0, {'state': 'prepared', 'max_useful_n': 1, 'max_n_state': 'ready',
                                            'max_n_milp': {'feasible': True, 'max_useful_n': 1}},
                             variant='smooth', overlay_id=999)
        row = store.load_component(scene_task, 0, variant='smooth', overlay_id=999)
        assert row['max_n_state'] == 'ready' and row['max_n_milp']['max_useful_n'] == 1
    finally:
        # Only the freshly generated audit schema is disposable.
        with engine.begin() as c:
            c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
