"""V2Store SQL contract, exercised with capture-database doubles (no PostgreSQL).

Mirrors the style of tests/test_postgres_store_unit.py: a fake Database records
every statement and returns canned rows, so the tests pin the SQL and the row
shapes that phase B codes against.
"""

from __future__ import annotations

import inspect
import json
from contextlib import contextmanager

import pytest

from rebar_service.codec import encode_object, sha256
from rebar_service.v2.store import V2Store

from tests.support.memory_v2_store import MemoryV2Store


class _Result:
    def __init__(self, rows=None, scalar=None):
        self._rows = [dict(row) for row in (rows or [])]
        self._scalar = scalar

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)

    def scalar_one(self):
        return self._scalar


class _Connection:
    def __init__(self, database):
        self.database = database

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        self.database.calls.append((sql, dict(params or {})))
        for pattern, response in self.database.responses:
            if pattern in sql:
                return response() if callable(response) else response
        return _Result()


class _Database:
    """Records statements; `responses` maps an SQL substring to a canned result."""

    def __init__(self, responses=None):
        self.calls: list[tuple[str, dict]] = []
        self.responses = list(responses or [])
        self.transactions = 0
        self.connections = 0

    @contextmanager
    def begin(self):
        self.transactions += 1
        yield _Connection(self)

    @contextmanager
    def connect(self):
        self.connections += 1
        yield _Connection(self)

    def sql(self) -> str:
        return "\n".join(statement for statement, _ in self.calls)

    def matching(self, marker: str) -> list[tuple[str, dict]]:
        return [(sql, params) for sql, params in self.calls if marker in sql]


def _store(responses=None) -> tuple[V2Store, _Database]:
    database = _Database(responses)
    return V2Store(database), database


# ---------- tasks ----------
def test_create_task_inserts_task_row_and_one_pending_row_per_requested_n():
    store, database = _store()

    store.create_task(
        "task1", scene_id="scene1", overlay_id=7, smooth=True,
        config={"axis": "x", "anchor_factor": 40}, ns=[3, 1, 3],
    )

    task_inserts = database.matching("INSERT INTO v2_tasks")
    assert len(task_inserts) == 1
    sql, params = task_inserts[0]
    assert "CAST(:config AS jsonb)" in sql
    assert params["scene_id"] == "scene1"
    assert params["overlay_id"] == 7
    assert params["smooth"] is True
    assert json.loads(params["config"]) == {"axis": "x", "anchor_factor": 40}

    n_inserts = database.matching("INSERT INTO v2_task_ns")
    assert [params["n"] for _, params in n_inserts] == [3, 1]
    assert "ON CONFLICT (task_id, n) DO NOTHING" in n_inserts[0][0]
    assert database.transactions == 1


def test_create_task_rejects_non_positive_n():
    store, _ = _store()
    with pytest.raises(ValueError):
        store.create_task("t", scene_id="s", overlay_id=0, smooth=False, config={}, ns=[1, 0])


def test_get_task_returns_the_documented_row_shape():
    row = {
        "id": "task1", "scene_id": "scene1", "overlay_id": 7, "smooth": True,
        "config": {"axis": "y"}, "state": "ready", "error": None,
        "max_useful_n": 12, "min_useful_n": 2, "prepare_info": {"cells": 3},
        "cancelled": False, "created_at": 1000.0, "updated_at": 1001.0,
    }
    store, _ = _store([("SELECT id, scene_id, overlay_id", _Result([row]))])

    task = store.get_task("task1")

    assert task == {
        "task_id": "task1", "scene_id": "scene1", "overlay_id": 7, "smooth": True,
        "config": {"axis": "y"}, "state": "ready", "error": None,
        "max_useful_n": 12, "min_useful_n": 2, "prepare_info": {"cells": 3},
        "cancelled": False, "created_at": 1000.0, "updated_at": 1001.0,
    }


def test_get_task_returns_none_for_unknown_task():
    store, _ = _store()
    assert store.get_task("missing") is None


def test_set_task_updates_only_the_given_fields():
    store, database = _store()

    store.set_task("task1", state="ready", max_useful_n=9, prepare_info={"grid": 300})

    sql, params = database.matching("UPDATE v2_tasks")[0]
    assert "state=:state" in sql
    assert "max_useful_n=:max_useful_n" in sql
    assert "prepare_info=CAST(:prepare_info AS jsonb)" in sql
    assert "error=" not in sql
    assert "updated_at=now()" in sql
    assert params["max_useful_n"] == 9
    assert json.loads(params["prepare_info"]) == {"grid": 300}


def test_set_task_rejects_unknown_fields():
    store, database = _store()
    with pytest.raises(ValueError):
        store.set_task("task1", generation=2)
    assert not database.calls


def test_set_task_without_fields_touches_nothing():
    store, database = _store()
    store.set_task("task1")
    assert not database.calls


# ---------- per-N rows ----------
def test_list_ns_is_ordered_and_hides_the_heavy_result_column():
    rows = [
        {"n": 1, "state": "success", "status": "optimal", "fun": 2.5,
         "mass_metrics": {"bg": {}}, "error": None, "updated_at": 5.0},
    ]
    store, database = _store([("FROM v2_task_ns WHERE task_id=:task_id ORDER BY n", _Result(rows))])

    listed = store.list_ns("task1")

    assert listed == [{"n": 1, "state": "success", "status": "optimal", "fun": 2.5,
                       "mass_metrics": {"bg": {}}, "error": None, "updated_at": 5.0}]
    sql, _ = database.calls[0]
    assert "result" not in sql.split("FROM")[0]
    assert database.connections == 1


def test_get_n_adds_the_result_payload():
    rows = [{"n": 4, "state": "bars", "status": None, "fun": None, "mass_metrics": None,
             "result": {"bars": []}, "error": None, "updated_at": 9.0}]
    store, _ = _store([("FROM v2_task_ns WHERE task_id=:task_id AND n=:n", _Result(rows))])

    assert store.get_n("task1", 4)["result"] == {"bars": []}


def test_get_n_returns_none_when_the_row_is_missing():
    store, _ = _store()
    assert store.get_n("task1", 4) is None


def test_set_n_writes_only_the_fields_that_were_passed():
    store, database = _store()

    store.set_n("task1", 3, state="fitting")

    sql, params = database.matching("UPDATE v2_task_ns")[0]
    assert "state=:state" in sql
    for column in ("status=", "fun=", "mass_metrics=", "result=", "error="):
        assert column not in sql
    assert params == {"task_id": "task1", "n": 3, "state": "fitting"}


def test_set_n_can_clear_a_column_with_an_explicit_none():
    store, database = _store()

    store.set_n("task1", 3, error=None, mass_metrics=None)

    sql, params = database.matching("UPDATE v2_task_ns")[0]
    assert "error=:error" in sql
    assert "mass_metrics=CAST(:mass_metrics AS jsonb)" in sql
    assert params["error"] is None
    assert params["mass_metrics"] is None


def test_set_n_refuses_to_null_the_not_null_state_column():
    store, database = _store()
    with pytest.raises(ValueError):
        store.set_n("task1", 3, state=None)
    assert not database.calls


def test_set_n_serialises_json_columns():
    store, database = _store()

    store.set_n("task1", 3, state="success", status="optimal", fun=1.5,
                mass_metrics={"bg": {"with_anchorage_kg": 2.0}}, result={"bars": [1]})

    _, params = database.matching("UPDATE v2_task_ns")[0]
    assert params["fun"] == 1.5
    assert json.loads(params["mass_metrics"]) == {"bg": {"with_anchorage_kg": 2.0}}
    assert json.loads(params["result"]) == {"bars": [1]}


def test_add_ns_returns_only_the_rows_that_were_actually_inserted():
    inserted = [_Result([{"n": 5}]), _Result([])]
    store, database = _store([("INSERT INTO v2_task_ns", lambda: inserted.pop(0))])

    added = store.add_ns("task1", [5, 6])

    assert added == [5]
    sql, _ = database.matching("INSERT INTO v2_task_ns")[0]
    assert "ON CONFLICT (task_id, n) DO NOTHING" in sql
    assert "RETURNING n" in sql


def test_add_ns_with_an_empty_request_opens_no_transaction():
    store, database = _store()
    assert store.add_ns("task1", []) == []
    assert database.transactions == 0


# ---------- scheduling lock ----------
def test_schedule_lock_locks_the_task_row_and_yields_the_transaction_connection():
    store, database = _store()

    with store.schedule_lock("task1") as conn:
        assert conn is not None
        store.set_n("task1", 2, state="solving", conn=conn)

    sql, params = database.calls[0]
    assert sql == "SELECT id FROM v2_tasks WHERE id=:task_id FOR UPDATE"
    assert params == {"task_id": "task1"}
    # The nested write reused the locked transaction instead of opening a second one.
    assert database.transactions == 1
    assert database.matching("UPDATE v2_task_ns")


def test_ns_in_states_uses_the_supplied_connection():
    rows = _Result([{"n": 2}, {"n": 7}])
    store, database = _store([("SELECT n FROM v2_task_ns", rows)])

    with store.schedule_lock("task1") as conn:
        assert store.ns_in_states("task1", ["pending", "preparing"], conn=conn) == [2, 7]

    assert database.connections == 0
    sql, params = database.matching("SELECT n FROM v2_task_ns")[0]
    assert "state = ANY(:states)" in sql
    assert params["states"] == ["pending", "preparing"]


def test_ns_in_states_without_states_asks_nothing():
    store, database = _store()
    assert store.ns_in_states("task1", []) == []
    assert not database.calls


# ---------- cancellation ----------
def test_cancel_without_ns_cancels_every_live_row_and_flags_the_task():
    store, database = _store([("UPDATE v2_task_ns SET state='cancelled'", _Result([{"n": 1}, {"n": 2}]))])

    assert store.cancel("task1", None) == [1, 2]

    sql, params = database.matching("UPDATE v2_task_ns SET state='cancelled'")[0]
    assert "n = ANY(:ns)" not in sql
    assert params["live"] == ["pending", "preparing", "solving", "fitting", "bars"]
    task_sql, _ = database.matching("UPDATE v2_tasks SET cancelled=true")[0]
    assert "NOT EXISTS" not in task_sql


def test_cancel_with_ns_flags_the_task_only_when_no_live_row_is_left():
    store, database = _store([("UPDATE v2_task_ns SET state='cancelled'", _Result([{"n": 3}]))])

    assert store.cancel("task1", [3, 3]) == [3]

    sql, params = database.matching("UPDATE v2_task_ns SET state='cancelled'")[0]
    assert "n = ANY(:ns)" in sql
    assert params["ns"] == [3]
    task_sql, _ = database.matching("UPDATE v2_tasks SET cancelled=true")[0]
    assert "NOT EXISTS" in task_sql and "state <> 'cancelled'" in task_sql


def test_cancel_with_an_empty_list_is_a_no_op():
    store, database = _store()
    assert store.cancel("task1", []) == []
    assert not database.calls


def test_is_cancelled_combines_the_task_flag_and_the_row_state():
    store, _ = _store([("SELECT (t.cancelled", _Result([{"cancelled": True}]))])
    assert store.is_cancelled("task1", 3) is True

    store, _ = _store([("SELECT (t.cancelled", _Result([{"cancelled": False}]))])
    assert store.is_cancelled("task1", 3) is False

    store, _ = _store()
    assert store.is_cancelled("missing", 3) is False


# ---------- artifacts ----------
def test_artifacts_round_trip_through_the_codec_with_a_sha256_guard():
    store, database = _store()
    value = {"field": [[1.0, 2.0]], "cfg": {"axis": "y"}}

    store.save_artifact("task1", "field", value)

    sql, params = database.matching("INSERT INTO v2_artifacts")[0]
    assert "ON CONFLICT (task_id, key) DO UPDATE" in sql
    assert params["sha256"] == sha256(params["payload"])
    assert params["codec"].startswith("pickle+")

    loader, _ = _store([("SELECT codec, payload, sha256 FROM v2_artifacts",
                         _Result([{"codec": params["codec"], "payload": params["payload"], "sha256": params["sha256"]}]))])
    assert loader.load_artifact("task1", "field") == value


def test_load_artifact_rejects_a_corrupted_payload():
    payload, _ = encode_object({"a": 1})
    store, _ = _store([("SELECT codec, payload, sha256 FROM v2_artifacts",
                        _Result([{"codec": "pickle+zlib", "payload": payload, "sha256": "0" * 64}]))])
    with pytest.raises(IOError):
        store.load_artifact("task1", "problem")


def test_load_artifact_returns_none_when_absent():
    store, _ = _store()
    assert store.load_artifact("task1", "problem") is None


def test_delete_artifact_targets_one_key():
    store, database = _store()
    store.delete_artifact("task1", "solver:5")
    sql, params = database.matching("DELETE FROM v2_artifacts")[0]
    assert "task_id=:task_id AND key=:key" in sql
    assert params["key"] == "solver:5"


# ---------- isolated worker tasks ----------
@pytest.mark.parametrize(
    "kind,table",
    [("bar", "v2_bar_tasks"), ("verification", "v2_verification_tasks")],
)
def test_worker_tasks_are_created_read_and_finished(kind, table):
    row = {
        "id": "w1", "scene_id": "scene1", "overlay_id": 3, "smooth": False,
        "config": {"axis": "x"}, "zones": [{"id": 0, "kind": "bg"}], "state": "success",
        "result": {"bars": []}, "error": None, "created_at": 1.0, "updated_at": 2.0,
    }
    store, database = _store([(f"FROM {table} WHERE id=:task_id", _Result([row]))])

    getattr(store, f"create_{kind}_task")(
        "w1", scene_id="scene1", overlay_id=3, smooth=False,
        config={"axis": "x"}, zones=[{"id": 0, "kind": "bg"}],
    )
    read = getattr(store, f"get_{kind}_task")("w1")
    getattr(store, f"set_{kind}_task")("w1", state="error", error="boom")

    insert_sql, insert_params = database.matching(f"INSERT INTO {table}")[0]
    assert "'pending'" in insert_sql
    assert json.loads(insert_params["zones"]) == [{"id": 0, "kind": "bg"}]
    assert read["task_id"] == "w1" and read["zones"] == [{"id": 0, "kind": "bg"}]
    assert read["result"] == {"bars": []}
    update_sql, update_params = database.matching(f"UPDATE {table} SET")[0]
    assert "state=:state" in update_sql and "result=CAST(:result AS jsonb)" in update_sql
    assert update_params == {"task_id": "w1", "state": "error", "result": None, "error": "boom"}


def test_worker_task_lookup_returns_none_when_missing():
    store, _ = _store()
    assert store.get_bar_task("w1") is None
    assert store.get_verification_task("w1") is None


# ---------- contract surface ----------
V2_STORE_CONTRACT = {
    "create_task": ["task_id", "scene_id", "overlay_id", "smooth", "config", "ns"],
    "get_task": ["task_id"],
    "set_task": ["task_id"],
    "list_ns": ["task_id"],
    "get_n": ["task_id", "n"],
    "set_n": ["task_id", "n", "state", "status", "fun", "mass_metrics", "result", "error"],
    "add_ns": ["task_id", "ns"],
    "schedule_lock": ["task_id"],
    "ns_in_states": ["task_id", "states", "conn"],
    "cancel": ["task_id", "ns"],
    "is_cancelled": ["task_id", "n"],
    "save_artifact": ["task_id", "key", "value"],
    "load_artifact": ["task_id", "key"],
    "delete_artifact": ["task_id", "key"],
    "create_bar_task": ["task_id", "scene_id", "overlay_id", "smooth", "config", "zones"],
    "get_bar_task": ["task_id"],
    "set_bar_task": ["task_id", "state", "result", "error"],
    "create_verification_task": ["task_id", "scene_id", "overlay_id", "smooth", "config", "zones"],
    "get_verification_task": ["task_id"],
    "set_verification_task": ["task_id", "state", "result", "error"],
}


@pytest.mark.parametrize("implementation", [V2Store, MemoryV2Store])
def test_v2_store_contract_surface_is_implemented(implementation):
    for name, expected in V2_STORE_CONTRACT.items():
        method = getattr(implementation, name, None)
        assert method is not None, f"{implementation.__name__}.{name} is missing"
        parameters = inspect.signature(method).parameters
        if name == "set_task":
            assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())
        missing = [param for param in expected if param not in parameters]
        assert not missing, f"{implementation.__name__}.{name} misses {missing}"


# ---------- in-memory double ----------
def test_memory_store_deep_copies_on_write_and_on_read():
    store = MemoryV2Store()
    config = {"stock": [{"d": 18.0, "step": 300.0}]}
    store.create_task("t", scene_id="s", overlay_id=0, smooth=False, config=config, ns=[1])

    config["stock"].append({"d": 20.0, "step": 150.0})
    assert store.get_task("t")["config"] == {"stock": [{"d": 18.0, "step": 300.0}]}

    read = store.get_task("t")
    read["config"]["stock"].clear()
    assert store.get_task("t")["config"]["stock"] == [{"d": 18.0, "step": 300.0}]


def test_memory_store_runs_the_full_task_lifecycle():
    store = MemoryV2Store()
    store.create_task("t", scene_id="s", overlay_id=2, smooth=True, config={}, ns=[2, 1])

    assert [row["n"] for row in store.list_ns("t")] == [1, 2]
    assert "result" not in store.list_ns("t")[0]
    assert store.add_ns("t", [2, 5]) == [5]
    assert store.ns_in_states("t", ["pending"]) == [1, 2, 5]

    store.set_task("t", state="ready", max_useful_n=7)
    store.set_n("t", 1, state="solving")
    store.set_n("t", 1, state="success", status="optimal", fun=3.5, result={"bars": []})
    assert store.ns_in_states("t", ["solving"]) == []
    row = store.get_n("t", 1)
    assert (row["state"], row["status"], row["fun"]) == ("success", "optimal", 3.5)
    assert row["result"] == {"bars": []}

    store.set_n("t", 1, error=None)
    assert store.get_n("t", 1)["fun"] == 3.5
    with pytest.raises(ValueError):
        store.set_n("t", 1, state=None)


def test_memory_store_cancellation_matches_the_sql_semantics():
    store = MemoryV2Store()
    store.create_task("t", scene_id="s", overlay_id=0, smooth=False, config={}, ns=[1, 2])
    store.set_n("t", 1, state="success")

    assert store.cancel("t", [2]) == [2]
    assert store.get_task("t")["cancelled"] is False
    assert store.is_cancelled("t", 2) is True
    assert store.is_cancelled("t", 1) is False
    # Already finished N are not reopened as cancelled.
    assert store.cancel("t", [1]) == []

    store.cancel("t", None)
    assert store.get_task("t")["cancelled"] is True
    assert store.is_cancelled("t", 1) is True


def test_memory_store_keeps_artifacts_and_worker_tasks():
    store = MemoryV2Store()
    problem = {"recipes": [1, 2]}
    store.save_artifact("t", "problem", problem)
    problem["recipes"].append(3)
    assert store.load_artifact("t", "problem") == {"recipes": [1, 2]}
    store.delete_artifact("t", "problem")
    assert store.load_artifact("t", "problem") is None

    store.create_bar_task("b", scene_id="s", overlay_id=0, smooth=False, config={"axis": "y"}, zones=[])
    assert store.get_bar_task("b")["state"] == "pending"
    store.set_bar_task("b", state="success", result={"bars": []})
    assert store.get_bar_task("b")["result"] == {"bars": []}

    store.create_verification_task(
        "v", scene_id="s", overlay_id=0, smooth=False, config={"t": 600.0}, zones=[]
    )
    store.set_verification_task("v", state="error", error="boom")
    assert store.get_verification_task("v")["error"] == "boom"
    assert store.get_verification_task("missing") is None


def test_file_backed_artifacts_keep_only_a_reference_row(tmp_path):
    from rebar_service.v2.artifacts import FileArtifactBackend

    backend = FileArtifactBackend(tmp_path / "shared", tmp_path / "cache")
    store, database = _store([])
    store.artifact_backend = backend
    value = {"matrix": [[1, 2], [3, 4]]}
    store.save_artifact("task1", "problem", value)
    sql, params = database.matching("INSERT INTO v2_artifacts")[0]
    assert params["codec"] == "fileref" and params["payload"] == b"task1/problem.bin"
    raw = (tmp_path / "shared" / "task1" / "problem.bin").read_bytes()
    assert params["sha256"] == sha256(raw)

    loader, _ = _store([("SELECT codec, payload, sha256 FROM v2_artifacts",
                         _Result([{"codec": "fileref", "payload": b"task1/problem.bin", "sha256": params["sha256"]}]))])
    loader.artifact_backend = backend
    assert loader.load_artifact("task1", "problem") == value

    deleter, _ = _store([("DELETE FROM v2_artifacts",
                          _Result([{"codec": "fileref", "payload": b"task1/problem.bin", "sha256": params["sha256"]}]))])
    deleter.artifact_backend = backend
    deleter.delete_artifact("task1", "problem")
    assert not (tmp_path / "shared" / "task1" / "problem.bin").exists()

    without_backend, _ = _store([("SELECT codec, payload, sha256 FROM v2_artifacts",
                                  _Result([{"codec": "fileref", "payload": b"task1/problem.bin", "sha256": "x"}]))])
    with pytest.raises(IOError):
        without_backend.load_artifact("task1", "problem")


def test_migration_moves_inline_artifacts_to_files_one_at_a_time(tmp_path):
    from rebar_service.codec import encode_object
    from rebar_service.v2.artifacts import FileArtifactBackend
    from rebar_service.v2.artifacts_migrate import migrate_inline_artifacts

    payload, codec = encode_object({"big": list(range(100))})
    store, database = _store([
        ("SELECT task_id, key FROM v2_artifacts WHERE codec", _Result([{"task_id": "t1", "key": "problem"}])),
        ("SELECT codec, payload, sha256 FROM v2_artifacts",
         _Result([{"codec": codec, "payload": payload, "sha256": sha256(payload)}])),
    ])
    store.artifact_backend = FileArtifactBackend(tmp_path / "shared", None)
    assert migrate_inline_artifacts(store, log=lambda _: None) == 1
    sql, params = database.matching("INSERT INTO v2_artifacts")[0]
    assert params["codec"] == "fileref" and params["payload"] == b"t1/problem.bin"
    assert (tmp_path / "shared" / "t1" / "problem.bin").exists()

    with pytest.raises(RuntimeError):
        migrate_inline_artifacts(_store([])[0])
