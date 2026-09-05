import json
from contextlib import contextmanager

from rebar_service.config import Settings
from rebar_service.postgres_store import PostgresStore


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class _CaptureConnection:
    def __init__(self, database):
        self.database = database

    def execute(self, statement, params=None):
        self.database.calls.append((str(statement), dict(params or {})))
        return _ScalarResult(17)


class _CaptureDatabase:
    def __init__(self):
        self.calls = []

    @contextmanager
    def begin(self):
        yield _CaptureConnection(self)


class _EventStore(PostgresStore):
    def get_meta(self, task_id):
        return {"task_id": task_id}


def test_event_json_stores_only_event_specific_payload():
    database = _CaptureDatabase()
    store = _EventStore(Settings(), database=database)

    event_id = store.publish_event("task123", "n_finished", {"n": 4, "variant": "smooth"})

    assert event_id == "17"
    _, params = database.calls[-1]
    assert params["event_type"] == "n_finished"
    assert json.loads(params["payload"]) == {"n": 4, "variant": "smooth"}


class _RowsResult:
    def __init__(self, *, scalar=None, scalars=None):
        self._scalar = scalar
        self._scalars = list(scalars or [])

    def scalar_one(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._scalars


class _RequestedNConnection:
    def __init__(self, database):
        self.database = database

    def execute(self, statement, params=None):
        sql = str(statement)
        params = dict(params or {})
        self.database.calls.append((sql, params))
        if "SELECT COUNT(*) FROM task_n_requests" in sql:
            return _RowsResult(scalar=1)
        if "SELECT n FROM task_n_requests" in sql:
            return _RowsResult(scalars=[3])
        if "SELECT COALESCE(MAX(position), -1)" in sql:
            return _RowsResult(scalar=0)
        return _RowsResult(scalar=0)


class _RequestedNDatabase:
    def __init__(self):
        self.calls = []

    @contextmanager
    def begin(self):
        yield _RequestedNConnection(self)


class _RequestedNStore(PostgresStore):
    def get_meta(self, task_id):
        return {"task_id": task_id, "initial_variant": "raw"}

    def get_plan(self, task_id, *, variant=None):
        return {"requested_n": [3], "variant": variant}


def test_readding_cancelled_requested_n_reactivates_existing_row():
    database = _RequestedNDatabase()
    store = _RequestedNStore(Settings(), database=database)

    store.add_requested_ns("task123", [3], variant="raw")

    matching_updates = [
        (sql, params)
        for sql, params in database.calls
        if "UPDATE task_n_requests" in sql and params.get("n") == 3
    ]
    assert matching_updates, "existing N must be reactivated instead of being ignored"
    sql, _ = matching_updates[-1]
    assert "cancelled_at=NULL" in sql
    assert "status='requested'" in sql
