from __future__ import annotations

import hashlib
import pickle
import zlib
from typing import Any

_MAGIC_ZSTD = b"RBZ1"
_MAGIC_ZLIB = b"RBL1"


def encode_object(value: Any) -> tuple[bytes, str]:
    raw = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    try:
        import zstandard as zstd

        payload = _MAGIC_ZSTD + zstd.ZstdCompressor(level=5).compress(raw)
        codec = "pickle+zstd"
    except ImportError:
        payload = _MAGIC_ZLIB + zlib.compress(raw, level=6)
        codec = "pickle+zlib"
    return payload, codec


def decode_object(payload: bytes, codec: str | None = None) -> Any:
    if payload.startswith(_MAGIC_ZSTD):
        detected_codec = "pickle+zstd"
        import zstandard as zstd

        raw = zstd.ZstdDecompressor().decompress(payload[len(_MAGIC_ZSTD) :])
    elif payload.startswith(_MAGIC_ZLIB):
        detected_codec = "pickle+zlib"
        raw = zlib.decompress(payload[len(_MAGIC_ZLIB) :])
    else:
        raise ValueError("Неизвестный формат blob")
    if codec is not None and str(codec) != detected_codec:
        raise ValueError(f"Codec mismatch: metadata={codec!r}, payload={detected_codec!r}")
    return pickle.loads(raw)


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
