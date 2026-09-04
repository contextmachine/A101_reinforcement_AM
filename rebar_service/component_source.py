from __future__ import annotations

import base64
import json
import re
import tempfile
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import parse_qs


def axis_from_filename(filename: str) -> str:
    name = Path(filename or "").stem.lower()
    if "буквен" in name or re.search(r"оси[\s_-]*[хx]\b", name) or re.search(r"(?:^|[\s_-])[хx](?:$|[\s_()\-])", name):
        return "x"
    if "цифров" in name or re.search(r"оси[\s_-]*[уy]\b", name) or re.search(r"(?:^|[\s_-])[уy](?:$|[\s_()\-])", name):
        return "y"
    return "y"


def _multipart(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    content_type = str(snapshot.get("content_type", ""))
    body = snapshot.get("body", b"") or b""
    if isinstance(body, str):
        body = body.encode("latin1")
    message = BytesParser(policy=default).parsebytes(
        ("Content-Type: %s\r\nMIME-Version: 1.0\r\n\r\n" % content_type).encode("utf-8") + body
    )
    out: Dict[str, Any] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            out[name or "file"] = {"filename": filename, "content": payload, "content_type": part.get_content_type()}
        else:
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            try:
                out[name] = json.loads(text)
            except Exception:
                out[name] = text
    return out


def parse_request_snapshot(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    content_type = str(snapshot.get("content_type", ""))
    body = snapshot.get("body", b"") or b""
    if "application/json" in content_type:
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        return dict(json.loads(body or "{}"))
    if "multipart/form-data" in content_type:
        return _multipart(snapshot)
    if isinstance(body, bytes):
        try:
            return dict(json.loads(body.decode("utf-8")))
        except Exception:
            pass
    return {}


def requested_ns(payload: Mapping[str, Any]) -> list[int]:
    raw = payload.get("n", payload.get("N", payload.get("ns", [])))
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = [x for x in re.split(r"[,;\s]+", raw) if x]
    if isinstance(raw, (int, float)):
        raw = [raw]
    return list(dict.fromkeys(int(x) for x in (raw or []) if int(x) > 0))


def solver_options(payload: Mapping[str, Any]) -> Dict[str, Any]:
    raw = payload.get("solver", {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    return dict(raw or {})


def load_polygons(payload: Mapping[str, Any], snapshot: Mapping[str, Any]):
    from A101.read_dxf import extract_polygons

    if payload.get("polygons") is not None:
        return payload["polygons"], str(payload.get("filename", snapshot.get("filename", "field.dxf")))

    file_value = next((v for v in payload.values() if isinstance(v, Mapping) and "content" in v and str(v.get("filename", "")).lower().endswith(".dxf")), None)
    if file_value is not None:
        content = file_value["content"]
        filename = str(file_value.get("filename") or "field.dxf")
        with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as fh:
            fh.write(content)
            path = Path(fh.name)
        try:
            return extract_polygons(path), filename
        finally:
            path.unlink(missing_ok=True)

    if payload.get("dxf_base64"):
        content = base64.b64decode(payload["dxf_base64"])
        filename = str(payload.get("filename") or "field.dxf")
        with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as fh:
            fh.write(content)
            path = Path(fh.name)
        try:
            return extract_polygons(path), filename
        finally:
            path.unlink(missing_ok=True)

    for name in ("dxf_path", "file_path", "path"):
        if payload.get(name):
            path = Path(payload[name])
            return extract_polygons(path), path.name

    raise ValueError("Не удалось получить polygons/DXF из исходного запроса задачи")
