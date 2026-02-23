"""Shared Novita (OpenAI-compatible) client helpers for demo agents."""

from __future__ import annotations

import asyncio
import json
import os
from urllib import error, request

DEFAULT_NOVITA_BASE_URL = "https://api.novita.ai/openai"
DEFAULT_NOVITA_MODEL = "zai-org/glm-5"


def get_novita_api_key() -> str:
    api_key = os.getenv("NOVITA_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing NOVITA_API_KEY (or OPENAI_API_KEY). Set it in your environment.")
    return api_key


def get_novita_base_url() -> str:
    return os.getenv("NOVITA_BASE_URL", DEFAULT_NOVITA_BASE_URL).rstrip("/")


async def novita_chat(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
) -> str:
    api_key = get_novita_api_key()
    base_url = get_novita_base_url()
    url = f"{base_url}/chat/completions"

    def _call() -> str:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        raw = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url=url,
            data=raw,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with request.urlopen(req, timeout=120) as resp:
                body = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Novita HTTP {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Novita request failed: {exc}") from exc

        parsed = json.loads(body)
        choices = parsed.get("choices") or []
        message = choices[0].get("message") if choices else {}
        content = message.get("content") if isinstance(message, dict) else ""
        return str(content).strip() if content else ""

    return await asyncio.to_thread(_call)
