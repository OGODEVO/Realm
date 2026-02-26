"""Dev-mode signing helpers (Ed25519)."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from agentnet.utils import new_id, utc_now_iso

DEV_AUTH_SCHEME = "dev-ed25519-v1"


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def canonical_json_bytes(data: dict[str, Any]) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_payload_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def payload_digest_hex(payload: Any) -> str:
    return hashlib.sha256(canonical_payload_bytes(payload)).hexdigest()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    padded = raw + "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def load_or_create_dev_keypair(path: str | Path) -> tuple[str, str]:
    key_path = Path(path)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if key_path.exists():
        loaded = json.loads(key_path.read_text())
        private_key = str(loaded.get("private_key") or "")
        public_key = str(loaded.get("public_key") or "")
        if private_key and public_key:
            return private_key, public_key

    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    private_raw = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    private_b64 = _b64url_encode(private_raw)
    public_b64 = _b64url_encode(public_raw)
    key_path.write_text(json.dumps({"private_key": private_b64, "public_key": public_b64}, indent=2))
    return private_b64, public_b64


def build_register_claims(
    *,
    agent_id: str,
    name: str,
    username: str | None,
    account_id: str | None,
    timestamp: str | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    return {
        "agent_id": agent_id,
        "name": name,
        "username": username or "",
        "account_id": account_id or "",
        "timestamp": timestamp or utc_now_iso(),
        "nonce": nonce or new_id(),
    }


def sign_claims(*, private_key_b64: str, claims: dict[str, str]) -> str:
    private = Ed25519PrivateKey.from_private_bytes(_b64url_decode(private_key_b64))
    signature = private.sign(canonical_json_bytes(dict(claims)))
    return _b64url_encode(signature)


def verify_claims(*, public_key_b64: str, claims: dict[str, str], signature_b64: str) -> bool:
    try:
        public = Ed25519PublicKey.from_public_bytes(_b64url_decode(public_key_b64))
        public.verify(_b64url_decode(signature_b64), canonical_json_bytes(dict(claims)))
        return True
    except (InvalidSignature, ValueError):
        return False


def build_message_claims(
    *,
    message_id: str,
    from_account_id: str,
    to_account_id: str | None,
    to_agent: str,
    sent_at: str,
    ttl_ms: int | None,
    expires_at: str | None,
    trace_id: str | None,
    thread_id: str | None,
    parent_message_id: str | None,
    kind: str,
    schema_version: str | None,
    idempotency_key: str | None,
    payload: Any,
) -> dict[str, str]:
    return {
        "message_id": message_id,
        "from_account_id": from_account_id,
        "to_account_id": to_account_id or "",
        "to_agent": to_agent,
        "sent_at": sent_at,
        "ttl_ms": str(ttl_ms or 0),
        "expires_at": expires_at or "",
        "trace_id": trace_id or "",
        "thread_id": thread_id or "",
        "parent_message_id": parent_message_id or "",
        "kind": kind,
        "schema_version": schema_version or "1.0",
        "idempotency_key": idempotency_key or "",
        "payload_sha256": payload_digest_hex(payload),
    }


def parse_iso_utc(value: str) -> datetime | None:
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
