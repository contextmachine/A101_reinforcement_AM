from __future__ import annotations

import importlib
import json
import os
from typing import Any, Dict
from urllib.parse import parse_qs

from .component_api import router as component_router
from .component_store import ComponentStore
from .component_workflow import ComponentWorkflow, WorkflowConfig


def _redis_client():
    import redis
    url = os.getenv("REBAR_REDIS_URL") or os.getenv("REDIS_URL") or "redis://localhost:6379/0"
    return redis.Redis.from_url(url, decode_responses=False)


def _legacy_app():
    errors = []
    for module_name in ("rebar_service.api", "rebar_service.main", "rebar_service.app", "main"):
        try:
            module = importlib.import_module(module_name)
            app = getattr(module, "app", None)
            if app is not None:
                return app
            for factory_name in ("create_app", "build_app", "get_app"):
                factory = getattr(module, factory_name, None)
                if callable(factory):
                    app = factory()
                    if app is not None:
                        return app
        except Exception as exc:
            errors.append("%s: %s" % (module_name, exc))
    raise ImportError("Legacy FastAPI app not found: " + "; ".join(errors))


class ComponentBootstrapMiddleware:
    """Capture legacy task creation and strip only new optional JSON fields."""

    CREATE_PATHS = {"/v1/tasks", "/v1/tasks/upload"}
    OPTION_KEYS = {"scan_mode", "whole", "component_result_top_k", "validate_results"}

    def __init__(self, app, workflow: ComponentWorkflow):
        self.app = app
        self.workflow = workflow

    async def __call__(self, scope, receive, send):
        path = str(scope.get("path", ""))
        method = str(scope.get("method", ""))
        create_request = method == "POST" and path in self.CREATE_PATHS
        action_match = None
        if method == "POST":
            import re
            action_match = re.fullmatch(r"/v1/tasks/([^/]+)/(n|cancel|pause|resume)", path)
        if scope.get("type") != "http" or not (create_request or action_match):
            await self.app(scope, receive, send)
            return

        chunks = []
        more = True
        while more:
            message = await receive()
            if message.get("type") != "http.request":
                continue
            chunks.append(message.get("body", b""))
            more = bool(message.get("more_body"))
        original_body = b"".join(chunks)
        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers", [])}
        content_type = headers.get("content-type", "")
        options: Dict[str, Any] = {}
        body_for_legacy = original_body

        query = parse_qs(scope.get("query_string", b"").decode("utf-8"))
        for key in self.OPTION_KEYS:
            if key in query:
                value = query[key][-1]
                options[key] = value.lower() in {"1", "true", "yes"} if key in {"whole", "validate_results"} else value

        if create_request and "application/json" in content_type:
            try:
                payload = json.loads(original_body.decode("utf-8") or "{}")
                if isinstance(payload, dict):
                    for key in self.OPTION_KEYS:
                        if key in payload:
                            options[key] = payload.pop(key)
                    body_for_legacy = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            except Exception:
                pass

        sent = False
        request_messages = [{"type": "http.request", "body": body_for_legacy, "more_body": False}]

        async def replay():
            return request_messages.pop(0) if request_messages else {"type": "http.request", "body": b"", "more_body": False}

        response_chunks = []
        status_code = 500

        async def capture(message):
            nonlocal status_code, sent
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 500))
            elif message.get("type") == "http.response.body":
                response_chunks.append(message.get("body", b""))
                if not message.get("more_body"):
                    sent = True
            await send(message)

        await self.app(scope, replay, capture)

        if sent and 200 <= status_code < 300:
            try:
                response = json.loads(b"".join(response_chunks).decode("utf-8") or "{}")
                if create_request:
                    task_id = response.get("task_id", response.get("id")) if isinstance(response, dict) else None
                    if task_id:
                        snapshot = {
                            "path": scope.get("path"),
                            "query_string": scope.get("query_string", b"").decode("utf-8"),
                            "content_type": content_type,
                            "body": original_body,
                            "filename": response.get("filename") if isinstance(response, dict) else None,
                        }
                        self.workflow.bootstrap_task(str(task_id), snapshot, options)
                elif action_match:
                    task_id, action = action_match.groups()
                    if action == "cancel":
                        self.workflow.store.cancel(task_id)
                    elif action == "pause":
                        self.workflow.store.update_task_meta(task_id, state="paused")
                    elif action == "resume":
                        self.workflow.store.update_task_meta(task_id, state="running")
                    elif action == "n":
                        payload = json.loads(original_body.decode("utf-8") or "{}") if "application/json" in content_type else {}
                        raw = payload.get("n", payload.get("N", [])) if isinstance(payload, dict) else []
                        if isinstance(raw, int):
                            raw = [raw]
                        self.workflow.schedule_requested_for_all(task_id, [int(v) for v in raw])
            except Exception as exc:
                # Legacy request has already succeeded; component-side failure stays observable without breaking it.
                print("component post-action failed:", type(exc).__name__, exc, flush=True)


legacy_app = _legacy_app()
component_store = ComponentStore(_redis_client())
component_service = ComponentWorkflow(component_store, WorkflowConfig())
legacy_app.state.component_store = component_store
legacy_app.state.component_service = component_service
legacy_app.include_router(component_router)
legacy_app.add_middleware(ComponentBootstrapMiddleware, workflow=component_service)
app = legacy_app
