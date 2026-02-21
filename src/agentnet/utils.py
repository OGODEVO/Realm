"""Small utility helpers used by AgentNet."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from typing import Any

_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id() -> str:
    return uuid.uuid4().hex


def new_ulid() -> str:
    timestamp_ms = int(time.time() * 1000)
    randomness = int.from_bytes(os.urandom(10), "big")
    value = (timestamp_ms << 80) | randomness

    chars: list[str] = []
    for _ in range(26):
        chars.append(_ULID_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def encode_json(data: Any) -> bytes:
    return json.dumps(data, separators=(",", ":"), default=_json_default).encode("utf-8")


def decode_json(raw: bytes | str) -> Any:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not raw:
        return {}
    return json.loads(raw)
