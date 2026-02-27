from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from agentnet import AgentNode, parse_compaction_required
from agentnet.schema import AgentMessage

from mesh_config import AgentProfile, MeshDefaults
from mesh_llm import complete_text
from mesh_tools import ToolCatalog, parse_tool_calls
from pretty_logs import print_event, print_header

_TOOL_PLANNER_INSTRUCTION = """
Tool use protocol (strict):
- Return ONLY a JSON object.
- If you need tools, return:
  {"tool_calls":[{"name":"tool_name","args":{"key":"value"}}]}
- If you are done, return:
  {"final":"your final plain-text answer"}
- Never return markdown code fences.
"""

_FINALIZE_INSTRUCTION = """
Finalization step (strict):
- Do NOT request tools.
- Return ONLY a JSON object in this shape:
  {"final":"your final plain-text answer"}
- Keep the final concise and grounded in the latest tool results.
"""


@dataclass(slots=True)
class NetworkAgent:
    profile: AgentProfile
    defaults: MeshDefaults
    tool_catalog: ToolCatalog
    node: AgentNode = field(init=False)
    _system_prompt: str = field(init=False)
    _history_by_thread: dict[str, list[dict[str, str]]] = field(init=False)
    _allowed_tools: list[str] = field(init=False)
    _tool_manifest: str = field(init=False)
    _tz: ZoneInfo = field(init=False)
    _max_tool_loops: int = field(init=False)
    _planner_max_tokens: int = field(init=False)
    _final_max_tokens: int = field(init=False)
    _work_timeout_seconds: float = field(init=False)
    _compaction_auto_enabled: bool = field(init=False)
    _compaction_keep_tail_messages: int = field(init=False)
    _compaction_max_messages: int = field(init=False)

    def __post_init__(self) -> None:
        self._work_timeout_seconds = max(10.0, float(os.getenv("MESH_WORK_TIMEOUT_SECONDS", "240")))
        self.node = AgentNode(
            agent_id=self.profile.agent_id,
            name=self.profile.name,
            username=self.profile.username,
            nats_url=os.getenv(self.defaults.nats_url_env, "nats://agentnet_secret_token@localhost:4222"),
            capabilities=["mesh.agent"],
            work_timeout_seconds=self._work_timeout_seconds,
        )
        self._system_prompt = self.profile.system_prompt_path.read_text(encoding="utf-8").strip()
        self._history_by_thread = defaultdict(list)
        if "*" in self.profile.allowed_tools:
            self._allowed_tools = self.tool_catalog.available()
        else:
            self._allowed_tools = list(self.profile.allowed_tools)
        self._tool_manifest = self.tool_catalog.describe(self._allowed_tools)
        try:
            self._tz = ZoneInfo(self.defaults.context_timezone)
        except Exception:  # noqa: BLE001
            self._tz = ZoneInfo("UTC")
        self._max_tool_loops = max(1, int(os.getenv("MESH_TOOL_LOOP_MAX_STEPS", "3")))
        self._planner_max_tokens = max(128, int(os.getenv("MESH_PLANNER_MAX_TOKENS", "1200")))
        self._final_max_tokens = max(128, int(os.getenv("MESH_FINAL_MAX_TOKENS", "1200")))
        self._compaction_auto_enabled = os.getenv("MESH_AUTO_COMPACTION", "true").strip().lower() in {"1", "true", "yes", "on"}
        self._compaction_keep_tail_messages = max(0, int(os.getenv("MESH_COMPACTION_KEEP_TAIL_MESSAGES", "24")))
        self._compaction_max_messages = max(50, int(os.getenv("MESH_COMPACTION_MAX_MESSAGES", "400")))
        self._wire_handler()

    def _time_context_block(self) -> str:
        now_local = datetime.now(self._tz)
        now_utc = datetime.now(timezone.utc)
        return (
            "Runtime time context:\n"
            f"- Local timezone: {self._tz.key}\n"
            f"- Local datetime: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z (%z)')}\n"
            f"- UTC datetime: {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC (+0000)')}\n"
            f"- Unix epoch seconds: {int(now_utc.timestamp())}\n"
            "Treat this as authoritative current time for all time-sensitive reasoning."
        )

    @staticmethod
    def _extract_json_objects(text: str) -> list[dict[str, Any]]:
        decoder = json.JSONDecoder()
        idx = 0
        found: list[dict[str, Any]] = []
        length = len(text)
        while idx < length:
            start = text.find("{", idx)
            if start == -1:
                break
            try:
                obj, end = decoder.raw_decode(text, start)
            except json.JSONDecodeError:
                idx = start + 1
                continue
            if isinstance(obj, dict):
                found.append(obj)
            idx = max(end, start + 1)
        return found

    @staticmethod
    def _parse_json_object(raw: str) -> dict[str, Any] | None:
        text = raw.strip()
        if not text:
            return None
        if text.startswith("```"):
            lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
            text = "\n".join(lines).strip()

        candidates = [text]
        left = text.find("{")
        right = text.rfind("}")
        if left != -1 and right != -1 and right > left:
            candidates.append(text[left : right + 1])

        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed

        objects = NetworkAgent._extract_json_objects(text)
        if not objects:
            return None
        for obj in objects:
            if parse_tool_calls(obj):
                return obj
        for obj in reversed(objects):
            maybe_final = obj.get("final")
            if isinstance(maybe_final, str) and maybe_final.strip():
                return obj
        return objects[-1]

    @staticmethod
    def _payload_text(payload: Any) -> str:
        if isinstance(payload, dict):
            return str(payload.get("text") or "").strip()
        return str(payload or "").strip()

    @staticmethod
    def _tool_fallback(all_tool_results: list[dict[str, Any]]) -> str:
        successful = [str(item.get("tool")) for item in all_tool_results if item.get("ok") and item.get("tool")]
        unique_tools = sorted(set(successful))
        if not unique_tools:
            return "No final answer produced after tool loop."
        return (
            "Tool execution completed, but no final narrative was produced. "
            f"Tools run: {', '.join(unique_tools)}."
        )

    @staticmethod
    def _is_checkpoint_payload(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        return str(payload.get("type") or "").strip().lower() == "checkpoint"

    @staticmethod
    def _payload_preview(payload: Any, max_chars: int = 180) -> str:
        if isinstance(payload, dict):
            text = str(payload.get("text") or "").strip()
            if text:
                return text[:max_chars]
            try:
                compact = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
            except TypeError:
                compact = str(payload)
            return compact[:max_chars]
        return str(payload or "").strip()[:max_chars]

    async def _fetch_thread_messages_for_compaction(self, thread_id: str) -> list[dict[str, Any]]:
        rows_desc: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(rows_desc) < self._compaction_max_messages:
            page_size = min(200, self._compaction_max_messages - len(rows_desc))
            result = await self.node.get_thread_messages(
                thread_id=thread_id,
                limit=page_size,
                cursor=cursor,
            )
            messages = result.get("messages") if isinstance(result, dict) else None
            if not isinstance(messages, list) or not messages:
                break
            rows_desc.extend(item for item in messages if isinstance(item, dict))
            next_cursor = result.get("next_cursor") if isinstance(result, dict) else None
            cursor = str(next_cursor) if next_cursor else None
            if not cursor:
                break
        return rows_desc

    def _build_checkpoint_summary(
        self,
        *,
        covered_messages: list[dict[str, Any]],
        covers_start: int,
        covers_end: int,
        total_messages_seen: int,
    ) -> str:
        if not covered_messages:
            return "No historical messages were available for checkpoint coverage."

        sample = covered_messages[-12:]
        lines: list[str] = []
        for item in sample:
            actor = str(item.get("from_agent") or item.get("from_account_id") or "unknown")
            kind = str(item.get("kind") or "direct")
            payload_preview = self._payload_preview(item.get("payload"))
            if payload_preview:
                lines.append(f"- {actor} [{kind}]: {payload_preview}")
            else:
                lines.append(f"- {actor} [{kind}]")
        sample_text = "\n".join(lines)
        return (
            f"Checkpoint summary for thread history messages {covers_start}..{covers_end} "
            f"(observed total={total_messages_seen}).\n"
            "Recent covered sample:\n"
            f"{sample_text}"
        )

    async def _handle_compaction_required(self, *, msg: AgentMessage, thread_id: str) -> bool:
        if not self._compaction_auto_enabled:
            return False
        event = parse_compaction_required(msg)
        if event is None:
            return False

        rows_desc = await self._fetch_thread_messages_for_compaction(thread_id)
        rows = list(reversed(rows_desc))
        observed_total = max(event.message_count, len(rows))
        covers_start = max(1, event.latest_checkpoint_end + 1)
        covers_end = max(covers_start - 1, observed_total - self._compaction_keep_tail_messages)
        if covers_end < covers_start:
            return False

        covered_count = min(len(rows), covers_end)
        covered_messages = rows[:covered_count]
        summary = self._build_checkpoint_summary(
            covered_messages=covered_messages,
            covers_start=covers_start,
            covers_end=covers_end,
            total_messages_seen=observed_total,
        )
        checkpoint_payload = {
            "type": "checkpoint",
            "summary_version": "v1",
            "covers_start": covers_start,
            "covers_end": covers_end,
            "keep_tail_messages": self._compaction_keep_tail_messages,
            "summary": summary,
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "generated_by": self.profile.username,
        }
        target_account_id = self.node.account_id or msg.to_account_id or msg.from_account_id
        if not target_account_id:
            return False

        await self.node.send_to_account(
            to_account_id=target_account_id,
            payload=checkpoint_payload,
            kind="system",
            thread_id=thread_id,
            parent_message_id=msg.message_id,
            idempotency_key=f"checkpoint:{thread_id}:{covers_end}",
            require_delivery_ack=False,
        )
        print_event(
            self.profile.key.upper(),
            "A_CHECKPOINT",
            f"Checkpoint written: covers {covers_start}-{covers_end}.",
            peer=msg.from_agent,
            session_tag=self.node.session_tag,
            trace_id=msg.trace_id,
            thread_id=thread_id,
            parent_message_id=msg.message_id,
            status="checkpoint_written",
            extra={"observed_total": observed_total, "kept_tail": self._compaction_keep_tail_messages},
            max_chars=600,
        )
        return True

    def _network_inference_user_block(self, *, msg: AgentMessage, payload: dict[str, Any], user_text: str, thread_id: str) -> str:
        inference = {
            "from_agent": msg.from_agent,
            "from_session_tag": msg.from_session_tag,
            "from_account_id": msg.from_account_id,
            "kind": msg.kind,
            "trace_id": msg.trace_id,
            "thread_id": thread_id,
            "message_id": msg.message_id,
            "parent_message_id": msg.parent_message_id,
            "payload": payload,
            "text": user_text,
        }
        return (
            "Network inference (treat this as the user command):\n"
            f"{json.dumps(inference, default=str)}"
        )

    async def _run_tool_calls(self, calls: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        results: list[dict[str, Any]] = []
        denied: list[str] = []
        for call in calls:
            tool_name = str(call["name"])
            if tool_name not in self._allowed_tools:
                denied.append(tool_name)
                continue
            results.append(await self.tool_catalog.run(tool_name, call.get("args")))
        return results, denied

    async def _run_agent_loop(
        self,
        *,
        msg: AgentMessage,
        payload: dict[str, Any],
        user_text: str,
        thread_id: str,
        history: list[dict[str, str]],
    ) -> tuple[str, list[dict[str, Any]]]:
        requested_calls = parse_tool_calls(payload)
        tool_results: list[dict[str, Any]] = []
        if requested_calls:
            pre_results, pre_denied = await self._run_tool_calls(requested_calls)
            tool_results.extend(pre_results)
            if pre_denied:
                tool_results.append({"ok": False, "error": "tool_not_allowed", "tools": pre_denied})
            print_event(
                self.profile.key.upper(),
                "TOOLS_PRE",
                f"Executed {len(pre_results)} tool(s) from incoming payload.",
                peer=msg.from_agent,
                session_tag=self.node.session_tag,
                trace_id=msg.trace_id,
                thread_id=thread_id,
                message_id=msg.message_id,
                status="tool_calls_inbound",
                max_chars=600,
            )

        all_tool_results = list(tool_results)
        model_user_text = self._network_inference_user_block(msg=msg, payload=payload, user_text=user_text, thread_id=thread_id)
        response_text = ""

        for step in range(1, self._max_tool_loops + 1):
            raw_completion = await complete_text(
                model_cfg=self.profile.model,
                system_prompt=f"{self._system_prompt}\n\n{self._time_context_block()}\n\n{_TOOL_PLANNER_INSTRUCTION}",
                history=history,
                user_text=model_user_text,
                tool_manifest=self._tool_manifest,
                tool_results=all_tool_results,
                max_tokens_override=self._planner_max_tokens,
            )
            parsed = self._parse_json_object(raw_completion)
            if parsed is None:
                response_text = raw_completion.strip()
                break

            final_text = parsed.get("final")
            if isinstance(final_text, str) and final_text.strip():
                response_text = final_text.strip()
                break

            model_calls = parse_tool_calls(parsed)
            if not model_calls:
                response_text = raw_completion.strip()
                break

            loop_results, loop_denied = await self._run_tool_calls(model_calls)
            all_tool_results.extend(loop_results)
            if loop_denied:
                all_tool_results.append({"ok": False, "error": "tool_not_allowed", "tools": loop_denied})
            print_event(
                self.profile.key.upper(),
                "TOOLS_MODEL",
                f"Step {step}: executed {len(loop_results)} tool(s).",
                peer=msg.from_agent,
                session_tag=self.node.session_tag,
                trace_id=msg.trace_id,
                thread_id=thread_id,
                message_id=msg.message_id,
                status="tool_calls_model",
                extra={"step": step, "denied": len(loop_denied)},
                max_chars=600,
            )

        if not response_text:
            raw_finalization = await complete_text(
                model_cfg=self.profile.model,
                system_prompt=f"{self._system_prompt}\n\n{self._time_context_block()}\n\n{_FINALIZE_INSTRUCTION}",
                history=history,
                user_text=model_user_text,
                tool_manifest="- No additional tools allowed in this finalization step.",
                tool_results=all_tool_results,
                max_tokens_override=self._final_max_tokens,
            )
            parsed_finalization = self._parse_json_object(raw_finalization)
            if isinstance(parsed_finalization, dict):
                maybe_final = parsed_finalization.get("final")
                if isinstance(maybe_final, str) and maybe_final.strip():
                    response_text = maybe_final.strip()
                else:
                    response_text = self._tool_fallback(all_tool_results)
            elif raw_finalization.strip():
                response_text = raw_finalization.strip()
            if not response_text:
                response_text = self._tool_fallback(all_tool_results)

        return response_text, all_tool_results

    async def maybe_postprocess_response(
        self,
        *,
        msg: AgentMessage,
        payload: dict[str, Any],
        user_text: str,
        thread_id: str,
        response_text: str,
        all_tool_results: list[dict[str, Any]],
    ) -> str:
        return response_text

    def _wire_handler(self) -> None:
        @self.node.on_message
        async def _handle(msg: AgentMessage) -> None:
            payload = msg.payload if isinstance(msg.payload, dict) else {"text": str(msg.payload)}
            thread_id = msg.thread_id or f"thread_{msg.trace_id or msg.message_id}"

            if self._is_checkpoint_payload(payload):
                print_event(
                    self.profile.key.upper(),
                    "RECV",
                    "Checkpoint message received; skipping model loop.",
                    peer=msg.from_agent,
                    session_tag=msg.from_session_tag,
                    trace_id=msg.trace_id,
                    thread_id=thread_id,
                    message_id=msg.message_id,
                    status=msg.kind,
                    max_chars=600,
                )
                return

            if await self._handle_compaction_required(msg=msg, thread_id=thread_id):
                return

            user_text = str(payload.get("text") or "").strip()
            if not user_text:
                user_text = "No user text was provided."

            history = self._history_by_thread[thread_id]
            history.append({"role": "user", "content": f"{msg.from_agent}: {user_text}"})
            if len(history) > self.defaults.max_history_messages:
                del history[: len(history) - self.defaults.max_history_messages]

            response_text, all_tool_results = await self._run_agent_loop(
                msg=msg,
                payload=payload,
                user_text=user_text,
                thread_id=thread_id,
                history=history,
            )
            response_text = await self.maybe_postprocess_response(
                msg=msg,
                payload=payload,
                user_text=user_text,
                thread_id=thread_id,
                response_text=response_text,
                all_tool_results=all_tool_results,
            )

            history.append({"role": "assistant", "content": response_text})
            if len(history) > self.defaults.max_history_messages:
                del history[: len(history) - self.defaults.max_history_messages]

            print_event(
                self.profile.key.upper(),
                "RECV",
                user_text,
                peer=msg.from_agent,
                session_tag=msg.from_session_tag,
                trace_id=msg.trace_id,
                thread_id=thread_id,
                message_id=msg.message_id,
                status=msg.kind,
                max_chars=600,
            )
            print_event(
                self.profile.key.upper(),
                "RESP",
                response_text or "<empty>",
                peer=msg.from_agent,
                session_tag=self.node.session_tag,
                trace_id=msg.trace_id,
                thread_id=thread_id,
                parent_message_id=msg.message_id,
                status="reply",
                max_chars=600,
            )

            if msg.reply_to:
                await self.node.reply(
                    request=msg,
                    payload={
                        "text": response_text,
                        "agent_key": self.profile.key,
                        "tool_results": all_tool_results,
                    },
                    kind="reply",
                    thread_id=thread_id,
                    parent_message_id=msg.message_id,
                )

    async def start(self) -> None:
        await self.node.start()
        print_header(
            self.profile.key.upper(),
            agent_id=self.profile.agent_id,
            session_tag=self.node.session_tag,
            model=f"{self.profile.model.provider}:{self.profile.model.model}",
        )

    async def close(self) -> None:
        await self.node.close()
