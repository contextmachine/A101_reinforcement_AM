import importlib.util
import sqlite3
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('scene_audit_migration',Path(__file__).resolve().parents[1]/'migrations/versions/0004_scene_audit_repairs.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

@pytest.mark.parametrize('rows, expected', [
    ([('raw',0,0,0,1),('smooth',0,1,0,2)],('smooth',0)),
    ([('raw',0,1,0,1),('smooth',0,1,0,2)],('raw',0)),
    ([('raw',0,0,0,1),('smooth',90,1,2,2),('smooth',1000,1,1,3)],('smooth',90)),
    ([('raw',300,1,1,9),('raw',200,1,2,10)],('raw',200)),
])
def test_canonical_ranking_distinguishes_placeholder_and_opaque_overlay_order(rows,expected):
    db=sqlite3.connect(':memory:')
    db.execute('CREATE TABLE c (variant TEXT, overlay_id INTEGER, populated INTEGER, overlay_seq INTEGER, updated_at INTEGER, original_initial_variant TEXT)')
    db.executemany("INSERT INTO c VALUES (?,?,?,?,?,'smooth')",rows)
    actual=db.execute('SELECT variant,overlay_id FROM c ORDER BY '+m.RANK_ORDER+' LIMIT 1').fetchone()
    assert actual==expected
    db.close()
