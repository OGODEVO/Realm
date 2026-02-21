"""High-level AgentNet node API."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from nats.aio.client import Client as NATS
from nats.aio.msg import Msg

from agentnet.registry import list_online_agents, list_online_agents_with_client
from agentnet.schema import AgentInfo, AgentMessage
from agentnet.subjects import (
    REGISTRY_GOODBYE_SUBJECT,
    REGISTRY_HELLO_SUBJECT,
    agent_capability_subject,
    agent_inbox_subject,
)
from agentnet.utils import decode_json, encode_json, new_id, utc_now_iso

MessageHandler = Callable[[AgentMessage], Awaitable[None]]


class AgentNode:
    def __init__(
        self,
        agent_id: str,
        name: str,
        capabilities: list[str] | None = None,
        nats_url: str = "nats://localhost:4222",
        metadata: dict[str, Any] | None = None,
        heartbeat_interval: float = 12.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.name = name
        self.capabilities = capabilities or []
        self.metadata = metadata or {}
        self.nats_url = nats_url
        self.heartbeat_interval = max(1.0, heartbeat_interval)
        self.logger = logger or logging.getLogger(f"agentnet.{agent_id}")

        self._nc: NATS | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._message_handler: MessageHandler | None = None

    @property
    def info(self) -> AgentInfo:
        return AgentInfo(
            agent_id=self.agent_id,
            name=self.name,
            capabilities=self.capabilities,
            metadata=self.metadata,
            last_seen=utc_now_iso(),
        )

    def on_message(self, handler: MessageHandler) -> MessageHandler:
        self._message_handler = handler
        return handler

    async def start(self) -> None:
        if self._nc and self._nc.is_connected:
            return

        self._nc = NATS()
        await self._nc.connect(servers=[self.nats_url], name=f"agentnet-{self.agent_id}")
        await self._nc.subscribe(agent_inbox_subject(self.agent_id), cb=self._handle_inbox)
        
        for capability in self.capabilities:
            await self._nc.subscribe(
                agent_capability_subject(capability),
                queue=f"agentnet-capability-{capability}",
                cb=self._handle_inbox,
            )

        await self._publish_hello()

        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name=f"agentnet-heartbeat-{self.agent_id}",
        )

    async def start_forever(self) -> None:
        await self.start()
        try:
            await asyncio.Event().wait()
        finally:
            await self.close()

    async def close(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if not self._nc:
            return

        nc = self._nc
        self._nc = None

        if nc.is_connected:
            try:
                await self._publish_goodbye(client=nc)
            except Exception:  # noqa: BLE001
                self.logger.exception("Failed publishing registry.goodbye")
            await nc.drain()

    async def send(self, to: str, payload: Any, kind: str = "direct") -> str:
        if not self._nc or not self._nc.is_connected:
            raise RuntimeError("AgentNode is not connected. Call start() first.")

        envelope = AgentMessage(
            message_id=new_id(),
            from_agent=self.agent_id,
            to_agent=to,
            payload=payload,
            sent_at=utc_now_iso(),
            kind=kind,
        )
        await self._nc.publish(agent_inbox_subject(to), encode_json(envelope.to_dict()))
        return envelope.message_id

    async def send_to_capability(self, capability: str, payload: Any, kind: str = "direct") -> str:
        if not self._nc or not self._nc.is_connected:
            raise RuntimeError("AgentNode is not connected. Call start() first.")

        envelope = AgentMessage(
            message_id=new_id(),
            from_agent=self.agent_id,
            to_agent=f"capability:{capability}",
            payload=payload,
            sent_at=utc_now_iso(),
            kind=kind,
        )
        await self._nc.publish(agent_capability_subject(capability), encode_json(envelope.to_dict()))
        return envelope.message_id

    async def request(self, to: str, payload: Any, timeout: float = 5.0, kind: str = "request") -> AgentMessage:
        if not self._nc or not self._nc.is_connected:
            raise RuntimeError("AgentNode is not connected. Call start() first.")

        envelope = AgentMessage(
            message_id=new_id(),
            from_agent=self.agent_id,
            to_agent=to,
            payload=payload,
            sent_at=utc_now_iso(),
            kind=kind,
        )
        
        reply_msg = await self._nc.request(agent_inbox_subject(to), encode_json(envelope.to_dict()), timeout=timeout)
        data = decode_json(reply_msg.data)
        if not isinstance(data, dict):
            raise ValueError("reply message payload must be a JSON object")
        return AgentMessage.from_dict(data)

    async def request_capability(self, capability: str, payload: Any, timeout: float = 5.0, kind: str = "request") -> AgentMessage:
        if not self._nc or not self._nc.is_connected:
            raise RuntimeError("AgentNode is not connected. Call start() first.")

        envelope = AgentMessage(
            message_id=new_id(),
            from_agent=self.agent_id,
            to_agent=f"capability:{capability}",
            payload=payload,
            sent_at=utc_now_iso(),
            kind=kind,
        )
        
        reply_msg = await self._nc.request(agent_capability_subject(capability), encode_json(envelope.to_dict()), timeout=timeout)
        data = decode_json(reply_msg.data)
        if not isinstance(data, dict):
            raise ValueError("reply message payload must be a JSON object")
        return AgentMessage.from_dict(data)

    async def list_online_agents(self, timeout: float = 2.0) -> list[AgentInfo]:
        if self._nc and self._nc.is_connected:
            return await list_online_agents_with_client(self._nc, timeout=timeout)
        return await list_online_agents(self.nats_url, timeout=timeout)

    async def _handle_inbox(self, msg: Msg) -> None:
        if self._message_handler is None:
            return

        try:
            data = decode_json(msg.data)
            if not isinstance(data, dict):
                raise ValueError("message payload must be a JSON object")
            message = AgentMessage.from_dict(data)
            if msg.reply:
                message.reply_to = msg.reply
        except Exception:  # noqa: BLE001
            self.logger.exception("Failed to decode inbox message")
            return

        await self._message_handler(message)

    async def _publish_hello(self, client: NATS | None = None) -> None:
        nc = client or self._nc
        if not nc:
            return
        payload = self.info.to_dict()
        await nc.publish(REGISTRY_HELLO_SUBJECT, encode_json(payload))

    async def _publish_goodbye(self, client: NATS | None = None) -> None:
        nc = client or self._nc
        if not nc:
            return
        await nc.publish(
            REGISTRY_GOODBYE_SUBJECT,
            encode_json({"agent_id": self.agent_id, "seen_at": utc_now_iso()}),
        )

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                await self._publish_hello()
            except Exception:  # noqa: BLE001
                self.logger.exception("Failed publishing registry heartbeat")

    async def __aenter__(self) -> "AgentNode":
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()
