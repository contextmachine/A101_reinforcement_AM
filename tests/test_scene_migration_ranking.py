from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "migrations/versions/0004_scene_audit_repairs.py"


def test_0004_is_intentionally_noop_for_non_destructive_cutover():
    text = AUDIT.read_text(encoding="utf-8")
    assert 'revision = "0004_scene_audit_repairs"' in text
    assert 'down_revision = "0003_scenes_immutable_tasks"' in text
    assert "op.execute(" not in text
    assert "INSERT INTO" not in text
    assert "UPDATE " not in text
    assert "DELETE FROM" not in text
