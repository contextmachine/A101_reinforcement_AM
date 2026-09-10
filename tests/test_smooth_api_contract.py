"""Check published parameters rather than literal Python source signatures."""
from __future__ import annotations

from rebar_service.api import app


def parameters(path: str, method: str) -> dict:
    return {p['name']: p for p in app.openapi()['paths'][path][method].get('parameters', [])}


def test_analysis_launch_routes_publish_smooth_selector():
    assert parameters('/v1/tasks', 'post')['smooth']['schema']['default'] is False
    upload = parameters('/v1/tasks/upload', 'post')
    assert upload['start']['schema']['default'] is True
    assert upload['smooth']['schema']['default'] is False
    assert '/v1/tasks/{task_id}/components/prepare' not in app.openapi()['paths']
    for path in ('/v1/tasks/{task_id}/components/{component_id}/n', '/v1/tasks/{task_id}/n'):
        assert 'smooth' in parameters(path, 'post')
        assert not parameters(path, 'post')['smooth'].get('required', False)


def test_component_read_routes_and_solution_list_can_select_smooth_variant():
    for path in ('/v1/tasks/{task_id}/components', '/v1/tasks/{task_id}/components/{component_id}',
                 '/v1/tasks/{task_id}/components/{component_id}/results',
                 '/v1/tasks/{task_id}/components/{component_id}/results/{n}',
                 '/v1/tasks/{task_id}/solutions'):
        p = parameters(path, 'get')['smooth']
        assert p['in'] == 'query' and not p.get('required', False)
        assert 'Legacy-task' in p['description']
