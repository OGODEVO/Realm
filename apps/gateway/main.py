"""FastAPI gateway: HTTP + API keys → AgentSDK.delegate_task → mesh.

External users never touch NATS. Engineers use REST (or this same pattern
in other services with the Python SDK).
"""

from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

# PYTHONPATH=src (or pip install -e .)
from agentnet.config import DEFAULT_NATS_URL
from agentnet.sdk import AgentSDK


def _api_keys() -> set[str]:
    raw = os.getenv("GATEWAY_API_KEYS", "dev-key-change-me")
    return {part.strip() for part in raw.split(",") if part.strip()}


def _nats_url() -> str:
    return (
        os.getenv("REALM_NATS_URL")
        or os.getenv("NATS_URL")
        or DEFAULT_NATS_URL
    )


# Fixed demo routes → worker usernames (override via env later if needed)
DEMO_ROUTES: dict[str, str] = {
    "orders": "@order_agent",
    "refunds": "@refund_agent",
    "inventory_low": "@inventory_agent",
    "support_tickets": "@support_agent",
    "shipping_delay": "@shipping_agent",
}


class JobRequest(BaseModel):
    to: str = Field(..., description="@username, acct_..., or capability:name")
    text: str
    title: str | None = None
    metadata: dict[str, Any] | None = None
    parent_task_id: str | None = None


class JobResponse(BaseModel):
    ok: bool
    task_id: str | None = None
    routed_to: str | None = None
    thread_id: str | None = None
    error: str | None = None


class GatewayState:
    sdk: AgentSDK | None = None


state = GatewayState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    sdk = AgentSDK(
        agent_id=os.getenv("GATEWAY_AGENT_ID", "http_gateway"),
        name=os.getenv("GATEWAY_AGENT_NAME", "HTTP Gateway"),
        username=os.getenv("GATEWAY_USERNAME", "http_gateway"),
        capabilities=["gateway", "http"],
        nats_url=_nats_url(),
        metadata={
            "role": "other",
            "company_visible": False,
            "kind": "http-gateway",
        },
    )
    await sdk.start()
    state.sdk = sdk
    try:
        yield
    finally:
        await sdk.stop()
        state.sdk = None


app = FastAPI(
    title="Realm HTTP Gateway",
    description="REST → Realm jobs (AgentSDK). Mesh token stays server-side.",
    version="0.1.0",
    lifespan=lifespan,
)


async def require_api_key(x_api_key: str | None = Header(default=None)) -> str:
    keys = _api_keys()
    if not x_api_key:
        raise HTTPException(status_code=401, detail="invalid or missing x-api-key")
    # Constant-time-ish membership for small key sets
    ok = False
    for key in keys:
        if len(key) == len(x_api_key) and secrets.compare_digest(key, x_api_key):
            ok = True
            break
    if not ok:
        raise HTTPException(status_code=401, detail="invalid or missing x-api-key")
    return x_api_key


def _sdk() -> AgentSDK:
    if state.sdk is None:
        raise HTTPException(status_code=503, detail="gateway not connected to Realm")
    return state.sdk


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": state.sdk is not None,
        "service": "realm-http-gateway",
        "nats_configured": bool(_nats_url()),
    }


@app.post("/v1/jobs", response_model=JobResponse)
async def create_job(
    body: JobRequest,
    _: str = Depends(require_api_key),
) -> JobResponse:
    """Generic job: route to any @agent / capability."""
    sdk = _sdk()
    result = await sdk.delegate_task(
        body.to,
        body.text,
        title=body.title,
        parent_task_id=body.parent_task_id,
        metadata=body.metadata,
        require_delivery_ack=False,
    )
    task_id = None
    if isinstance(result.data, dict):
        task_id = str(result.data.get("task_id") or "") or None
    task_id = task_id or result.trace_id
    if not result.ok and result.error:
        return JobResponse(ok=False, error=result.error, routed_to=body.to, task_id=task_id)
    return JobResponse(
        ok=True,
        task_id=task_id,
        routed_to=body.to,
        thread_id=result.thread_id,
    )


async def _route_demo(
    kind: str,
    payload: dict[str, Any],
    *,
    text: str | None = None,
) -> JobResponse:
    target = DEMO_ROUTES[kind]
    title = kind
    body_text = text or f"{kind}: {payload!s}"[:500]
    sdk = _sdk()
    result = await sdk.delegate_task(
        target,
        body_text,
        title=title,
        metadata={"kind": kind, **payload},
        require_delivery_ack=False,
    )
    task_id = None
    if isinstance(result.data, dict):
        task_id = str(result.data.get("task_id") or "") or None
    task_id = task_id or result.trace_id
    return JobResponse(
        ok=bool(result.ok or task_id),
        task_id=task_id,
        routed_to=target,
        thread_id=result.thread_id,
        error=result.error,
    )


@app.post("/v1/orders", response_model=JobResponse)
async def orders(body: dict[str, Any], _: str = Depends(require_api_key)) -> JobResponse:
    return await _route_demo("orders", body)


@app.post("/v1/refunds", response_model=JobResponse)
async def refunds(body: dict[str, Any], _: str = Depends(require_api_key)) -> JobResponse:
    return await _route_demo("refunds", body)


@app.post("/v1/inventory/low", response_model=JobResponse)
async def inventory_low(body: dict[str, Any], _: str = Depends(require_api_key)) -> JobResponse:
    return await _route_demo("inventory_low", body)


@app.post("/v1/support/tickets", response_model=JobResponse)
async def support_tickets(body: dict[str, Any], _: str = Depends(require_api_key)) -> JobResponse:
    return await _route_demo("support_tickets", body)


@app.post("/v1/shipping/delay", response_model=JobResponse)
async def shipping_delay(body: dict[str, Any], _: str = Depends(require_api_key)) -> JobResponse:
    return await _route_demo("shipping_delay", body)
