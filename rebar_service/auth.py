from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Query, WebSocket, WebSocketException, status

from .config import get_settings


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = get_settings().api_key
    if expected and not (x_api_key and hmac.compare_digest(x_api_key, expected)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


async def require_ws_key(websocket: WebSocket, token: str | None = Query(default=None)) -> None:
    expected = get_settings().api_key
    supplied = websocket.headers.get("x-api-key") or token
    if expected and not (supplied and hmac.compare_digest(supplied, expected)):
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
