"""Local blob storage helpers for AgentNet SDK integrations."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentnet.utils import new_ulid, utc_now_iso


def _safe_filename(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    allowed = []
    for ch in raw:
        if ch.isalnum() or ch in {"-", "_", ".", " "}:
            allowed.append(ch)
        else:
            allowed.append("_")
    normalized = "".join(allowed).strip().strip(".")
    return normalized or None


def _guess_mime_type(*, filename: str | None, fallback: str = "application/octet-stream") -> str:
    if filename:
        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            return guessed
    return fallback


def _sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@dataclass(slots=True)
class BlobRef:
    blob_id: str
    storage: str = "local_fs"
    filename: str | None = None
    mime_type: str = "application/octet-stream"
    size_bytes: int = 0
    sha256: str | None = None
    created_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "blob_ref",
            "blob_id": self.blob_id,
            "storage": self.storage,
            "mime_type": self.mime_type,
            "size_bytes": int(self.size_bytes),
        }
        if self.filename:
            payload["filename"] = self.filename
        if self.sha256:
            payload["sha256"] = self.sha256
        if self.created_at:
            payload["created_at"] = self.created_at
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "BlobRef":
        blob_id = str(payload.get("blob_id") or "").strip()
        if not blob_id:
            raise ValueError("blob_id is required")
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        return cls(
            blob_id=blob_id,
            storage=str(payload.get("storage") or "local_fs"),
            filename=str(payload.get("filename") or "") or None,
            mime_type=str(payload.get("mime_type") or "application/octet-stream"),
            size_bytes=max(0, int(payload.get("size_bytes") or 0)),
            sha256=str(payload.get("sha256") or "") or None,
            created_at=str(payload.get("created_at") or "") or None,
            metadata=dict(metadata),
        )


def is_blob_ref(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    return str(payload.get("type") or "").strip().lower() == "blob_ref" and bool(str(payload.get("blob_id") or "").strip())


def parse_blob_ref(payload: Any) -> BlobRef | None:
    if not is_blob_ref(payload):
        return None
    assert isinstance(payload, dict)
    return BlobRef.from_payload(payload)


class LocalBlobStore:
    """Simple local filesystem blob store for SDK integrations."""

    def __init__(self, base_dir: str | os.PathLike[str] | None = None) -> None:
        root = Path(base_dir or os.getenv("AGENTNET_BLOB_DIR") or ".agentnet_blobs")
        self.base_dir = root
        self._data_dir = root / "data"
        self._meta_dir = root / "meta"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._meta_dir.mkdir(parents=True, exist_ok=True)

    def put_bytes(
        self,
        raw: bytes,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BlobRef:
        if not isinstance(raw, (bytes, bytearray)):
            raise TypeError("raw must be bytes-like")
        blob_id = f"blob_{new_ulid().lower()}"
        safe_filename = _safe_filename(filename)
        effective_mime = str(mime_type or "").strip() or _guess_mime_type(filename=safe_filename)
        payload = bytes(raw)
        ref = BlobRef(
            blob_id=blob_id,
            filename=safe_filename,
            mime_type=effective_mime,
            size_bytes=len(payload),
            sha256=_sha256_hex(payload),
            created_at=utc_now_iso(),
            metadata=dict(metadata or {}),
        )
        self._data_path(blob_id).write_bytes(payload)
        self._meta_path(blob_id).write_text(json.dumps(ref.to_payload(), indent=2, sort_keys=True))
        return ref

    def put_file(
        self,
        path: str | os.PathLike[str],
        *,
        filename: str | None = None,
        mime_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BlobRef:
        source = Path(path)
        raw = source.read_bytes()
        return self.put_bytes(
            raw,
            filename=filename or source.name,
            mime_type=mime_type,
            metadata=metadata,
        )

    def head(self, blob_id: str) -> BlobRef | None:
        meta_path = self._meta_path(blob_id)
        if not meta_path.exists():
            return None
        data = json.loads(meta_path.read_text())
        if not isinstance(data, dict):
            return None
        return BlobRef.from_payload(data)

    def get_bytes(self, blob_id: str) -> bytes:
        return self._data_path(blob_id).read_bytes()

    def get_text(self, blob_id: str, *, encoding: str = "utf-8") -> str:
        return self.get_bytes(blob_id).decode(encoding)

    def delete(self, blob_id: str) -> bool:
        existed = False
        for path in (self._data_path(blob_id), self._meta_path(blob_id)):
            if path.exists():
                path.unlink()
                existed = True
        return existed

    def _data_path(self, blob_id: str) -> Path:
        normalized = str(blob_id or "").strip()
        if not normalized:
            raise ValueError("blob_id is required")
        return self._data_dir / normalized

    def _meta_path(self, blob_id: str) -> Path:
        normalized = str(blob_id or "").strip()
        if not normalized:
            raise ValueError("blob_id is required")
        return self._meta_dir / f"{normalized}.json"
