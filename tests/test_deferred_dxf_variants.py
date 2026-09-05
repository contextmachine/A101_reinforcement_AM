from contextlib import contextmanager

import pytest

import rebar_service.postgres_store as postgres_store_module
from rebar_service.config import Settings
from rebar_service.pipeline import PipelineWorkflow
from rebar_service.postgres_store import PostgresStore


class _CaptureConnection:
    def __init__(self, database):
        self.database = database

    def execute(self, statement, params=None):
        self.database.calls.append((str(statement), dict(params or {})))
        return _NoopResult()


class _NoopResult:
    def first(self):
        return None

    def mappings(self):
        return self

    def scalar_one_or_none(self):
        return None


class _CaptureDatabase:
    def __init__(self):
        self.calls = []

    @contextmanager
    def begin(self):
        yield _CaptureConnection(self)


class _VariantMaterializingStore:
    def __init__(self):
        self.materialized = False

    def ensure_polygon_variants(self, task_id):
        assert task_id == "task-dxf"
        self.materialized = True

    def load_variant_polygons(self, task_id, *, variant="raw"):
        assert self.materialized, "worker must materialize DXF variants before reading them"
        assert task_id == "task-dxf"
        assert variant == "smooth"
        return [{"points": [[0, 0], [1, 0], [1, 1]], "load": 2.0}]


def _task_meta():
    return {
        "state": "queued_preparation",
        "parameters": {},
        "requested_n": [1, 2],
        "n_mode": "list",
        "n_source": [1, 2],
        "scan_mode": "requested",
        "whole": False,
        "component_result_top_k": 5,
        "validate_results": False,
        "max_concurrent_jobs": 4,
        "manual_mode": False,
        "initial_variant": "raw",
        "paused": False,
    }


def test_dxf_create_task_defers_polygon_variant_build(monkeypatch):
    database = _CaptureDatabase()
    store = PostgresStore(Settings(), database=database)

    def _must_not_build(_input):
        raise AssertionError("DXF parsing must not run inside the upload request")

    monkeypatch.setattr(postgres_store_module, "build_polygon_variants", _must_not_build)

    store.create_task(
        "task-dxf",
        _task_meta(),
        {"mode": "list", "order": [1, 2]},
        {"kind": "dxf", "filename": "drawing.dxf", "content": b"DXF bytes"},
    )

    variant_inserts = [
        (sql, params)
        for sql, params in database.calls
        if "INSERT INTO task_variants" in sql
    ]
    assert {params["variant"] for _, params in variant_inserts} == {"raw", "smooth"}
    assert all(params["polygons"] == "[]" for _, params in variant_inserts)
    assert all(params["preparation_state"] == "source_pending" for _, params in variant_inserts)


def test_pipeline_materializes_deferred_variants_before_loading_them():
    workflow = object.__new__(PipelineWorkflow)
    workflow.store = _VariantMaterializingStore()

    rows = workflow._persisted_variant_polygons(
        "task-dxf",
        "smooth",
        {"kind": "dxf", "filename": "drawing.dxf", "content": b"DXF bytes"},
    )

    assert workflow.store.materialized is True
    assert rows[0]["load"] == 2.0
