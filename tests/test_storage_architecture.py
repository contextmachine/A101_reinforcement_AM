from pathlib import Path

from shapely.geometry import box

from rebar_service.postgres_store import PostgresStore, json_safe_value


def test_whole_component_is_normalized_to_minus_one_in_postgres():
    assert PostgresStore.component_db_id("whole") == -1
    assert PostgresStore.component_db_id(-1) == -1
    assert PostgresStore.component_db_id("3") == 3
    assert PostgresStore.component_public_id(-1) == "whole"


def test_json_safe_value_stores_geometry_as_geojson_not_repr():
    value = json_safe_value({"geometry": box(0, 0, 10, 20)})
    assert value["geometry"]["type"] == "Polygon"
    assert value["geometry"]["coordinates"][0][0] == (10.0, 0.0) or value["geometry"]["coordinates"][0][0] == [10.0, 0.0]


def test_redis_queue_module_does_not_contain_durable_task_storage_names():
    path = Path(__file__).resolve().parents[1] / "rebar_service/redis_queue.py"
    text = path.read_text(encoding="utf-8")
    forbidden = ["result-meta", "frontier-version", "blob:", "n-status", '"events"']
    for token in forbidden:
        assert token not in text


def test_save_field_does_not_overwrite_immutable_raw_or_smooth_source_polygons():
    import inspect

    source = inspect.getsource(PostgresStore.save_field)
    assert "SET polygons=" not in source
    assert 'record.pop("start_polygons", None)' in source


def test_pipeline_does_not_store_derivable_component_count_in_task_metadata():
    root = Path(__file__).resolve().parents[1]
    pipeline = (root / "rebar_service/pipeline.py").read_text(encoding="utf-8")
    assert "component_count=len(" not in pipeline


def test_field_artifact_does_not_duplicate_variant_index_arrays():
    import inspect

    source = inspect.getsource(PostgresStore.save_field)
    assert "decomposition.pop(key, [])" in source
    for key in ("active_indices", "background_only_indices", "degenerate_indices"):
        assert f'"{key}"' in source
