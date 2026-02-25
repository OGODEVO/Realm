from __future__ import annotations

import json
import os
from typing import Any

import httpx

from mesh_config import AgentModelConfig


def _env_required(name: str, default: str | None = None) -> str:
    value = os.getenv(name, "").strip()
    if not value and default is not None:
        return default
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _chat_messages(
    *,
    system_prompt: str,
    history: list[dict[str, str]],
    user_text: str,
    tool_manifest: str,
    tool_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    tool_result_text = json.dumps(tool_results, default=str) if tool_results else "[]"
    user_block = (
        f"User message:\n{user_text}\n\n"
        f"Tool results (if any):\n{tool_result_text}\n\n"
        "Answer clearly and concisely. If tool output is present, ground your answer in it."
    )
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": f"{system_prompt}\n\nAvailable tools:\n{tool_manifest}",
        }
    ]
    messages.extend(history)
    messages.append({"role": "user", "content": user_block})
    return messages


def _openai_chat_endpoint(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    if normalized.endswith("/v1") or normalized.endswith("/openai"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"


def _openai_responses_endpoint(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/responses"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/responses"
    return f"{normalized}/v1/responses"


def _claude_messages_endpoint(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1/messages"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/messages"
    return f"{normalized}/v1/messages"


def _openai_mode(*, base_url: str) -> str:
    raw = os.getenv("MESH_OPENAI_API_MODE", "auto").strip().lower()
    if raw in {"chat", "chat_completions"}:
        return "chat"
    if raw in {"responses", "response"}:
        return "responses"
    # auto mode: OpenAI first-party uses /responses, OpenAI-compatible vendors stay on chat/completions.
    return "responses" if "api.openai.com" in base_url.lower() else "chat"


def _extract_response_text(body: dict[str, Any]) -> str:
    parts: list[str] = []
    output = body.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
    combined = "\n".join(parts).strip()
    if combined:
        return combined
    # Fallback to minimal JSON text for debugging unexpected payload shapes.
    return json.dumps(body, default=str)


async def complete_text(
    *,
    model_cfg: AgentModelConfig,
    system_prompt: str,
    history: list[dict[str, str]],
    user_text: str,
    tool_manifest: str,
    tool_results: list[dict[str, Any]],
    max_tokens_override: int | None = None,
) -> str:
    provider = model_cfg.provider.lower().strip()
    if provider == "openai":
        return await _complete_openai(
            model_cfg=model_cfg,
            system_prompt=system_prompt,
            history=history,
            user_text=user_text,
            tool_manifest=tool_manifest,
            tool_results=tool_results,
            max_tokens_override=max_tokens_override,
        )
    if provider == "claude":
        return await _complete_claude(
            model_cfg=model_cfg,
            system_prompt=system_prompt,
            history=history,
            user_text=user_text,
            tool_manifest=tool_manifest,
            tool_results=tool_results,
            max_tokens_override=max_tokens_override,
        )
    raise RuntimeError(f"Unsupported provider '{model_cfg.provider}'. Use 'openai' or 'claude'.")


async def _complete_openai(
    *,
    model_cfg: AgentModelConfig,
    system_prompt: str,
    history: list[dict[str, str]],
    user_text: str,
    tool_manifest: str,
    tool_results: list[dict[str, Any]],
    max_tokens_override: int | None = None,
) -> str:
    api_key = _env_required(model_cfg.api_key_env)
    default_base_url: str | None = None
    env_name = model_cfg.base_url_env.upper().strip()
    if env_name == "OPENAI_BASE_URL":
        default_base_url = "https://api.openai.com/v1"
    elif env_name == "NOVITA_BASE_URL":
        default_base_url = "https://api.novita.ai/openai"
    base_url = _env_required(model_cfg.base_url_env, default=default_base_url).rstrip("/")
    mode = _openai_mode(base_url=base_url)
    max_tokens = model_cfg.max_tokens
    if max_tokens_override is not None and max_tokens_override > 0:
        max_tokens = min(max_tokens, int(max_tokens_override))
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    llm_timeout_seconds = _env_float("MESH_LLM_TIMEOUT_SECONDS", 45.0)
    async with httpx.AsyncClient(timeout=llm_timeout_seconds) as client:
        if mode == "responses":
            endpoint = _openai_responses_endpoint(base_url)
            history_text = "\n".join([f"{entry['role']}: {entry['content']}" for entry in history]) if history else "(none)"
            tool_result_text = json.dumps(tool_results, default=str) if tool_results else "[]"
            prompt = (
                f"Conversation history:\n{history_text}\n\n"
                f"User message:\n{user_text}\n\n"
                f"Available tools:\n{tool_manifest}\n\n"
                f"Tool results (if any):\n{tool_result_text}\n\n"
                "Respond clearly and concisely."
            )
            payload = {
                "model": model_cfg.model,
                "instructions": system_prompt,
                "input": prompt,
                "temperature": model_cfg.temperature,
                "max_output_tokens": max_tokens,
            }
            response = await client.post(endpoint, json=payload, headers=headers)
        else:
            endpoint = _openai_chat_endpoint(base_url)
            payload = {
                "model": model_cfg.model,
                "messages": _chat_messages(
                    system_prompt=system_prompt,
                    history=history,
                    user_text=user_text,
                    tool_manifest=tool_manifest,
                    tool_results=tool_results,
                ),
                "temperature": model_cfg.temperature,
                "max_tokens": max_tokens,
            }
            response = await client.post(endpoint, json=payload, headers=headers)
    response.raise_for_status()
    body = response.json()
    if mode == "responses":
        return _extract_response_text(body).strip()
    content = body["choices"][0]["message"]["content"]
    return str(content).strip()


async def _complete_claude(
    *,
    model_cfg: AgentModelConfig,
    system_prompt: str,
    history: list[dict[str, str]],
    user_text: str,
    tool_manifest: str,
    tool_results: list[dict[str, Any]],
    max_tokens_override: int | None = None,
) -> str:
    api_key = _env_required(model_cfg.api_key_env)
    base_url = _env_required(model_cfg.base_url_env, default="https://api.anthropic.com").rstrip("/")
    endpoint = _claude_messages_endpoint(base_url)

    history_text = "\n".join([f"{entry['role']}: {entry['content']}" for entry in history]) if history else "(none)"
    tool_result_text = json.dumps(tool_results, default=str) if tool_results else "[]"
    prompt = (
        f"Conversation history:\n{history_text}\n\n"
        f"User message:\n{user_text}\n\n"
        f"Available tools:\n{tool_manifest}\n\n"
        f"Tool results (if any):\n{tool_result_text}\n\n"
        "Respond clearly and concisely."
    )

    payload = {
        "model": model_cfg.model,
        "max_tokens": min(model_cfg.max_tokens, int(max_tokens_override))
        if max_tokens_override is not None and max_tokens_override > 0
        else model_cfg.max_tokens,
        "temperature": model_cfg.temperature,
        "system": system_prompt,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    llm_timeout_seconds = _env_float("MESH_LLM_TIMEOUT_SECONDS", 45.0)
    async with httpx.AsyncClient(timeout=llm_timeout_seconds) as client:
        response = await client.post(endpoint, json=payload, headers=headers)
    response.raise_for_status()
    body = response.json()
    blocks = body.get("content") or []
    text_parts = [str(block.get("text") or "") for block in blocks if isinstance(block, dict)]
    return "\n".join([part for part in text_parts if part]).strip()
