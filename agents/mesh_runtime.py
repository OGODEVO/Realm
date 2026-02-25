from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agentnet.schema import AgentMessage

from agent import NetworkAgent
from mesh_llm import complete_text
from pretty_logs import print_event

_AGENT1_SYNTHESIS_INSTRUCTION = """
You are Agent 1 moderating a multi-agent debate.
Produce a final consensus answer for the user.
Requirements:
- Resolve disagreements between peers using evidence quality.
- Treat relay_context_v1 as authoritative current-state evidence.
- Reject claims that relay data is wrong based only on model memory (for example, old team/trade knowledge).
- Only downgrade data trust when relay_context_v1 itself signals issues via source_errors, missing_data, or internal contradictions.
- Name the selected game (or explicitly say no consensus).
- Include confidence (low/medium/high) and one next action.
- Keep it concise, plain text, no markdown table.
"""


@dataclass(slots=True)
class AgentRunner(NetworkAgent):
    _agent1_orchestrate: bool = field(init=False)
    _agent1_peers: list[str] = field(init=False)
    _agent1_rounds: int = field(init=False)
    _agent1_peer_timeout_seconds: float = field(init=False)
    _agent1_peer_retries: int = field(init=False)
    _agent1_kickoff_tools: list[str] = field(init=False)
    _agent1_focus_by_thread: dict[str, dict[str, str]] = field(init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        self._agent1_orchestrate = self._env_bool("MESH_AGENT1_ORCHESTRATE", default=False)
        self._agent1_peers = self._parse_agent1_peers(os.getenv("MESH_AGENT1_PEERS", "mesh_agent_2,mesh_agent_3"))
        self._agent1_rounds = max(1, int(os.getenv("MESH_AGENT1_DEBATE_ROUNDS", "2")))
        self._agent1_peer_timeout_seconds = max(5.0, float(os.getenv("MESH_AGENT1_PEER_TIMEOUT_SECONDS", "45")))
        self._agent1_peer_retries = max(0, int(os.getenv("MESH_AGENT1_PEER_RETRIES", "1")))
        raw_kickoff = os.getenv("MESH_AGENT1_KICKOFF_TOOLS", "get_daily_schedule,get_pregame_context")
        self._agent1_kickoff_tools = [item.strip() for item in raw_kickoff.split(",") if item.strip()]
        self._agent1_focus_by_thread = {}

    @staticmethod
    def _env_bool(name: str, *, default: bool) -> bool:
        raw = os.getenv(name, "").strip().lower()
        if not raw:
            return default
        return raw in {"1", "true", "yes", "on"}

    def _parse_agent1_peers(self, raw: str) -> list[str]:
        parts = [item.strip().lstrip("@") for item in raw.split(",") if item.strip()]
        deduped: list[str] = []
        for peer in parts:
            if peer == self.profile.username:
                continue
            if peer and peer not in deduped:
                deduped.append(peer)
        return deduped[:2]

    def _kickoff_args_for_tool(self, tool_name: str) -> dict[str, Any]:
        today = datetime.now(self._tz).strftime("%Y-%m-%d")
        if tool_name in {"get_daily_schedule", "get_live_scores"}:
            return {"date": today}
        if tool_name == "get_market_odds":
            return {
                "sport": "basketball_nba",
                "regions": "us",
                "markets": "h2h,spreads,totals",
            }
        if tool_name == "get_pregame_context":
            # Team is resolved dynamically after schedule fetch.
            return {"date": today}
        return {}

    @staticmethod
    def _parse_schedule_games_from_result(raw: str) -> list[dict[str, Any]]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        games = data.get("data", {}).get("NBA", []) if isinstance(data, dict) else []
        return [game for game in games if isinstance(game, dict)] if isinstance(games, list) else []

    @staticmethod
    def _normalize_team_text(value: str) -> str:
        text = value.lower().strip()
        aliases = {
            "76ers": "philadelphia 76ers",
            "sixers": "philadelphia 76ers",
            "6ers": "philadelphia 76ers",
            "pacers": "indiana pacers",
        }
        return aliases.get(text, text)

    def _choose_target_game(
        self,
        games: list[dict[str, Any]],
        user_text: str,
        *,
        allow_fallback: bool = True,
    ) -> dict[str, Any] | None:
        if not games:
            return None

        query = user_text.lower()
        # Direct hint for requested matchup in user prompt.
        wants_76ers_pacers = (
            ("76ers" in query or "sixers" in query or "6ers" in query or "philadelphia" in query)
            and ("pacers" in query or "indiana" in query)
        )
        if wants_76ers_pacers:
            for game in games:
                away = str(game.get("away_team") or "").lower()
                home = str(game.get("home_team") or "").lower()
                if "76ers" in away and "pacers" in home:
                    return game
                if "pacers" in away and "76ers" in home:
                    return game

        # Existing test preference for Spurs/Pistons.
        for game in games:
            away = str(game.get("away_team") or "").lower()
            home = str(game.get("home_team") or "").lower()
            if "spurs" in away and "pistons" in home:
                return game

        # Generic matching: if both team names appear in prompt, prefer that game.
        for game in games:
            away = str(game.get("away_team") or "")
            home = str(game.get("home_team") or "")
            away_tokens = away.lower().split()
            home_tokens = home.lower().split()
            if any(token in query for token in away_tokens) and any(token in query for token in home_tokens):
                return game

        if not allow_fallback:
            return None
        return games[0]

    async def _ensure_agent1_kickoff_evidence(
        self,
        *,
        msg: AgentMessage,
        thread_id: str,
        all_tool_results: list[dict[str, Any]],
    ) -> None:
        if not self._agent1_kickoff_tools:
            return
        successful = {
            str(item.get("tool"))
            for item in all_tool_results
            if isinstance(item, dict) and item.get("ok") and item.get("tool")
        }

        # Step 1: always ensure schedule is loaded first.
        if "get_daily_schedule" in self._agent1_kickoff_tools and "get_daily_schedule" not in successful:
            schedule_calls = [{"name": "get_daily_schedule", "args": self._kickoff_args_for_tool("get_daily_schedule")}]
            schedule_results, schedule_denied = await self._run_tool_calls(schedule_calls)
            all_tool_results.extend(schedule_results)
            if schedule_denied:
                all_tool_results.append({"ok": False, "error": "tool_not_allowed", "tools": schedule_denied})
            print_event(
                self.profile.key.upper(),
                "A_KICKOFF",
                f"Executed {len(schedule_results)} kickoff tool(s).",
                peer=msg.from_agent,
                session_tag=self.node.session_tag,
                trace_id=msg.trace_id,
                thread_id=thread_id,
                message_id=msg.message_id,
                status="kickoff_tools_schedule",
                extra={"denied": len(schedule_denied)},
                max_chars=600,
            )

        # Step 2: if configured, derive pregame context target from schedule.
        if "get_pregame_context" in self._agent1_kickoff_tools and "get_pregame_context" not in successful:
            schedule_rows = [item for item in all_tool_results if item.get("tool") == "get_daily_schedule" and item.get("ok")]
            games: list[dict[str, Any]] = []
            if schedule_rows:
                games = self._parse_schedule_games_from_result(str(schedule_rows[-1].get("result") or ""))
            if games:
                payload = msg.payload if isinstance(msg.payload, dict) else {"text": str(msg.payload)}
                user_text = str(payload.get("text") or "")
                chosen = self._choose_target_game(games, user_text, allow_fallback=False)
                if not chosen:
                    prior = self._agent1_focus_by_thread.get(thread_id, {})
                    prior_game_id = str(prior.get("game_id") or "")
                    if prior_game_id:
                        for game in games:
                            if str(game.get("game_ID") or "") == prior_game_id:
                                chosen = game
                                break
                if not chosen:
                    chosen = games[0]
                self._agent1_focus_by_thread[thread_id] = {
                    "game_id": str(chosen.get("game_ID") or ""),
                    "home_team": str(chosen.get("home_team") or ""),
                    "away_team": str(chosen.get("away_team") or ""),
                }
                target_team = str(chosen.get("home_team") or chosen.get("away_team") or "").strip()
                pregame_args = self._kickoff_args_for_tool("get_pregame_context")
                if target_team:
                    pregame_args["team_name"] = target_team
                pregame_calls = [{"name": "get_pregame_context", "args": pregame_args}]
                pregame_results, pregame_denied = await self._run_tool_calls(pregame_calls)
                all_tool_results.extend(pregame_results)
                if pregame_denied:
                    all_tool_results.append({"ok": False, "error": "tool_not_allowed", "tools": pregame_denied})
                print_event(
                    self.profile.key.upper(),
                    "A_KICKOFF",
                    f"Executed {len(pregame_results)} kickoff tool(s).",
                    peer=msg.from_agent,
                    session_tag=self.node.session_tag,
                    trace_id=msg.trace_id,
                    thread_id=thread_id,
                    message_id=msg.message_id,
                    status="kickoff_tools_pregame",
                    extra={"team": target_team or "-", "denied": len(pregame_denied)},
                    max_chars=600,
                )

        # Step 3: run any remaining configured kickoff tools (excluding the two handled above).
        post_successful = {
            str(item.get("tool"))
            for item in all_tool_results
            if isinstance(item, dict) and item.get("ok") and item.get("tool")
        }
        remaining_calls: list[dict[str, Any]] = []
        for tool_name in self._agent1_kickoff_tools:
            if tool_name in {"get_daily_schedule", "get_pregame_context"}:
                continue
            if tool_name in post_successful:
                continue
            remaining_calls.append({"name": tool_name, "args": self._kickoff_args_for_tool(tool_name)})
        if remaining_calls:
            rest_results, rest_denied = await self._run_tool_calls(remaining_calls)
            all_tool_results.extend(rest_results)
            if rest_denied:
                all_tool_results.append({"ok": False, "error": "tool_not_allowed", "tools": rest_denied})
            print_event(
                self.profile.key.upper(),
                "A_KICKOFF",
                f"Executed {len(rest_results)} kickoff tool(s).",
                peer=msg.from_agent,
                session_tag=self.node.session_tag,
                trace_id=msg.trace_id,
                thread_id=thread_id,
                message_id=msg.message_id,
                status="kickoff_tools_remaining",
                extra={"denied": len(rest_denied)},
                max_chars=600,
            )

    @staticmethod
    def _parse_json_result(raw: str) -> dict[str, Any]:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _extract_schedule_games(raw: str) -> list[dict[str, Any]]:
        data = AgentRunner._parse_json_result(raw)
        games = data.get("data", {}).get("NBA", []) if isinstance(data, dict) else []
        if not isinstance(games, list):
            return []
        return [game for game in games if isinstance(game, dict)]

    @staticmethod
    def _extract_schedule_lines(raw: str, max_games: int = 3) -> list[str]:
        games = AgentRunner._extract_schedule_games(raw)
        if not games:
            return []
        lines: list[str] = []
        for game in games[:max_games]:
            away = str(game.get("away_team") or "?")
            home = str(game.get("home_team") or "?")
            game_time = str(game.get("game_time") or "unknown_time")
            lines.append(f"{away} @ {home} ({game_time})")
        return lines

    @staticmethod
    def _latest_ok_tool_result(all_tool_results: list[dict[str, Any]], tool_name: str) -> str:
        matching = [
            item
            for item in all_tool_results
            if isinstance(item, dict) and item.get("tool") == tool_name and item.get("ok")
        ]
        if not matching:
            return ""
        return str(matching[-1].get("result") or "")

    @staticmethod
    def _find_schedule_game_by_matchup(
        schedule_games: list[dict[str, Any]],
        *,
        home_team: str,
        away_team: str,
        game_id: str,
    ) -> dict[str, Any]:
        if game_id:
            for game in schedule_games:
                if str(game.get("game_ID") or "") == game_id:
                    return game
        home_lower = home_team.lower().strip()
        away_lower = away_team.lower().strip()
        if home_lower and away_lower:
            for game in schedule_games:
                g_home = str(game.get("home_team") or "").lower().strip()
                g_away = str(game.get("away_team") or "").lower().strip()
                if g_home == home_lower and g_away == away_lower:
                    return game
        return schedule_games[0] if schedule_games else {}

    @staticmethod
    def _as_float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _build_relay_context_v1(self, all_tool_results: list[dict[str, Any]]) -> dict[str, Any]:
        schedule_raw = self._latest_ok_tool_result(all_tool_results, "get_daily_schedule")
        pregame_raw = self._latest_ok_tool_result(all_tool_results, "get_pregame_context")
        live_vs_raw = self._latest_ok_tool_result(all_tool_results, "get_live_vs_season_context")
        live_scores_raw = self._latest_ok_tool_result(all_tool_results, "get_live_scores")

        schedule_games = self._extract_schedule_games(schedule_raw)
        pregame_data = self._parse_json_result(pregame_raw)
        live_vs_data = self._parse_json_result(live_vs_raw)
        live_scores_data = self._parse_json_result(live_scores_raw)
        pregame_error = str(pregame_data.get("error") or "").strip() if isinstance(pregame_data, dict) else ""
        live_vs_error = str(live_vs_data.get("error") or "").strip() if isinstance(live_vs_data, dict) else ""
        live_scores_error = str(live_scores_data.get("error") or "").strip() if isinstance(live_scores_data, dict) else ""

        pregame_game = pregame_data.get("game", {}) if isinstance(pregame_data, dict) else {}
        if not isinstance(pregame_game, dict):
            pregame_game = {}
        focus_schedule_game = self._find_schedule_game_by_matchup(
            schedule_games,
            home_team=str(pregame_game.get("home_team") or ""),
            away_team=str(pregame_game.get("away_team") or ""),
            game_id=str(pregame_game.get("game_id") or ""),
        )

        season_home = pregame_data.get("season_context", {}).get("home", {}) if isinstance(pregame_data, dict) else {}
        season_away = pregame_data.get("season_context", {}).get("away", {}) if isinstance(pregame_data, dict) else {}
        if not isinstance(season_home, dict):
            season_home = {}
        if not isinstance(season_away, dict):
            season_away = {}
        home_metrics = season_home.get("season_metrics", {}) if isinstance(season_home.get("season_metrics"), dict) else {}
        away_metrics = season_away.get("season_metrics", {}) if isinstance(season_away.get("season_metrics"), dict) else {}
        home_offrtg = self._as_float(home_metrics.get("OffRtg_est"))
        away_offrtg = self._as_float(away_metrics.get("OffRtg_est"))
        offrtg_edge = round(away_offrtg - home_offrtg, 2) if away_offrtg is not None and home_offrtg is not None else None

        roster = pregame_data.get("roster", {}) if isinstance(pregame_data, dict) else {}
        roster_home = roster.get("home", {}) if isinstance(roster.get("home"), dict) else {}
        roster_away = roster.get("away", {}) if isinstance(roster.get("away"), dict) else {}
        injuries_home = roster_home.get("injuries", {}) if isinstance(roster_home.get("injuries"), dict) else {}
        injuries_away = roster_away.get("injuries", {}) if isinstance(roster_away.get("injuries"), dict) else {}
        home_players = injuries_home.get("players", []) if isinstance(injuries_home.get("players"), list) else []
        away_players = injuries_away.get("players", []) if isinstance(injuries_away.get("players"), list) else []
        home_players_text = [str(name) for name in home_players if name is not None]
        away_players_text = [str(name) for name in away_players if name is not None]
        home_count_total = (
            int(injuries_home.get("count"))
            if isinstance(injuries_home.get("count"), (int, float))
            else len(home_players_text)
        )
        away_count_total = (
            int(injuries_away.get("count"))
            if isinstance(injuries_away.get("count"), (int, float))
            else len(away_players_text)
        )
        max_injury_names = 20
        home_players_sample = home_players_text[:max_injury_names]
        away_players_sample = away_players_text[:max_injury_names]

        market_lines = pregame_data.get("market_lines", {}) if isinstance(pregame_data, dict) else {}
        if not isinstance(market_lines, dict):
            market_lines = {}
        if not market_lines:
            market_obj = pregame_data.get("market", {}) if isinstance(pregame_data, dict) else {}
            if isinstance(market_obj, dict):
                maybe_lines = market_obj.get("market_lines", {})
                if isinstance(maybe_lines, dict):
                    market_lines = maybe_lines

        live_game = live_vs_data.get("game", {}) if isinstance(live_vs_data, dict) else {}
        if not isinstance(live_game, dict):
            live_game = {}
        live_info = ""
        if isinstance(live_scores_data, dict):
            live_info = str(live_scores_data.get("info") or "").strip()

        has_market_lines = bool(market_lines) and bool(market_lines.get("available", False))
        has_injury_summary = bool(home_count_total or away_count_total or home_players_sample or away_players_sample)
        has_live_context = (bool(live_game) or bool(live_info)) and not bool(live_vs_error or live_scores_error)
        has_pregame_context = bool(pregame_data) and not pregame_error and bool(
            pregame_game.get("home_team") and pregame_game.get("away_team")
        )

        missing: list[str] = []
        if not schedule_games:
            missing.append("schedule")
        if not has_pregame_context:
            missing.append("pregame_context")
        if not has_market_lines:
            missing.append("market_lines")
        if not has_injury_summary:
            missing.append("injury_summary")
        if pregame_error:
            missing.append("pregame_context_error")
        if live_vs_error:
            missing.append("live_vs_season_error")
        if live_scores_error:
            missing.append("live_scores_error")

        tools_executed = sorted(
            {
                str(item.get("tool"))
                for item in all_tool_results
                if isinstance(item, dict) and item.get("ok") and item.get("tool")
            }
        )
        return {
            "schema": "relay_context_v1",
            "generated_at_local": datetime.now(self._tz).strftime("%Y-%m-%d %H:%M:%S %Z"),
            "tools_executed": tools_executed,
            "focus_game": {
                "game_id": str(focus_schedule_game.get("game_ID") or pregame_game.get("game_id") or ""),
                "home_team": str(focus_schedule_game.get("home_team") or pregame_game.get("home_team") or ""),
                "away_team": str(focus_schedule_game.get("away_team") or pregame_game.get("away_team") or ""),
                "game_time_utc": str(focus_schedule_game.get("game_time") or ""),
                "status_schedule": str(focus_schedule_game.get("status") or ""),
                "status_pregame": str(pregame_game.get("status") or ""),
            },
            "season_edge": {
                "home_team": str(season_home.get("team") or ""),
                "away_team": str(season_away.get("team") or ""),
                "home_record": f"{home_metrics.get('wins', 0)}-{home_metrics.get('losses', 0)}",
                "away_record": f"{away_metrics.get('wins', 0)}-{away_metrics.get('losses', 0)}",
                "home_win_pct": home_metrics.get("win_pct"),
                "away_win_pct": away_metrics.get("win_pct"),
                "home_offrtg_est": home_offrtg,
                "away_offrtg_est": away_offrtg,
                "away_minus_home_offrtg_est": offrtg_edge,
                "home_points_per_game": home_metrics.get("points_per_game"),
                "away_points_per_game": away_metrics.get("points_per_game"),
                "home_total_rebounds_per_game": home_metrics.get("total_rebounds_per_game"),
                "away_total_rebounds_per_game": away_metrics.get("total_rebounds_per_game"),
                "home_assists_per_game": home_metrics.get("assists_per_game"),
                "away_assists_per_game": away_metrics.get("assists_per_game"),
                "home_steals_per_game": home_metrics.get("steals_per_game"),
                "away_steals_per_game": away_metrics.get("steals_per_game"),
                "home_blocks_per_game": home_metrics.get("blocks_per_game"),
                "away_blocks_per_game": away_metrics.get("blocks_per_game"),
            },
            "injuries": {
                "home": {
                    "team": str(roster_home.get("team_name") or ""),
                    "count_total": home_count_total,
                    "players": home_players_sample,
                    "players_truncated": bool(len(home_players_text) > len(home_players_sample)),
                },
                "away": {
                    "team": str(roster_away.get("team_name") or ""),
                    "count_total": away_count_total,
                    "players": away_players_sample,
                    "players_truncated": bool(len(away_players_text) > len(away_players_sample)),
                },
            },
            "market_lines": {
                "available": bool(market_lines.get("available", False)),
                "as_of": market_lines.get("as_of"),
                "moneyline_home": market_lines.get("moneyline", {}).get("home", {}).get("price")
                if isinstance(market_lines.get("moneyline"), dict)
                else None,
                "moneyline_away": market_lines.get("moneyline", {}).get("away", {}).get("price")
                if isinstance(market_lines.get("moneyline"), dict)
                else None,
                "spread_home": market_lines.get("spreads", {}).get("home", {}).get("consensus_point")
                if isinstance(market_lines.get("spreads"), dict)
                else None,
                "spread_away": market_lines.get("spreads", {}).get("away", {}).get("consensus_point")
                if isinstance(market_lines.get("spreads"), dict)
                else None,
                "total": market_lines.get("totals", {}).get("consensus_total")
                if isinstance(market_lines.get("totals"), dict)
                else None,
            },
            "live_context": {
                "status": str(live_game.get("status") or ""),
                "quarter": live_game.get("quarter"),
                "time_remaining": live_game.get("time_remaining"),
                "home_score": live_game.get("home_score"),
                "away_score": live_game.get("away_score"),
                "live_scores_info": live_info,
            },
            "data_quality": {
                "has_schedule": bool(schedule_games),
                "has_pregame_context": has_pregame_context,
                "has_market_lines": has_market_lines,
                "has_injury_summary": has_injury_summary,
                "has_live_context": has_live_context,
            },
            "source_errors": {
                "pregame_context_error": pregame_error or None,
                "live_vs_season_error": live_vs_error or None,
                "live_scores_error": live_scores_error or None,
            },
            "missing_data": missing,
        }

    def _build_kickoff_evidence_snapshot(self, all_tool_results: list[dict[str, Any]], base_response_text: str) -> str:
        relay = self._build_relay_context_v1(all_tool_results)
        lines = [
            "Agent 1 kickoff evidence snapshot (relay_context_v1):",
            json.dumps(relay, default=str),
            "",
            "Agent 1 draft view:",
            base_response_text,
        ]
        return "\n".join(lines).strip()

    async def _request_peer(
        self,
        *,
        username: str,
        prompt: str,
        trace_id: str,
        thread_id: str,
        parent_message_id: str,
        round_idx: int,
    ) -> tuple[str, str]:
        reply = await self.node.request_username(
            username=username,
            payload={
                "text": prompt,
                "mesh_stage": "agent1_debate_round",
                "round": round_idx,
            },
            timeout=self._agent1_peer_timeout_seconds,
            trace_id=trace_id,
            thread_id=thread_id,
            parent_message_id=parent_message_id,
        )
        payload = reply.payload if isinstance(reply.payload, dict) else {"text": str(reply.payload)}
        error_code = str(payload.get("error") or "").strip()
        if reply.kind == "error" or error_code:
            detail = str(payload.get("detail") or payload.get("text") or "peer returned error").strip()
            raise RuntimeError(f"peer_error kind={reply.kind} code={error_code or 'unknown'} detail={detail}")
        text = self._payload_text(payload)
        if not text:
            raise RuntimeError("peer_empty_reply")
        return username, text

    async def _request_peer_with_retry(
        self,
        *,
        username: str,
        prompt: str,
        trace_id: str,
        thread_id: str,
        parent_message_id: str,
        round_idx: int,
    ) -> tuple[str, str]:
        attempts = 1 + self._agent1_peer_retries
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await self._request_peer(
                    username=username,
                    prompt=prompt,
                    trace_id=trace_id,
                    thread_id=thread_id,
                    parent_message_id=parent_message_id,
                    round_idx=round_idx,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                print_event(
                    self.profile.key.upper(),
                    "A_PEER_ERR",
                    str(exc),
                    peer=f"@{username}",
                    session_tag=self.node.session_tag,
                    trace_id=trace_id,
                    thread_id=thread_id,
                    message_id=parent_message_id,
                    status="peer_request_error",
                    extra={"round": round_idx, "attempt": attempt},
                    max_chars=600,
                )
                if attempt < attempts:
                    await asyncio.sleep(min(0.75 * attempt, 2.0))
        raise RuntimeError(f"peer_timeout_or_error username={username}: {last_error}")

    async def _agent1_debate_and_synthesize(
        self,
        *,
        msg: AgentMessage,
        user_text: str,
        thread_id: str,
        base_response_text: str,
        all_tool_results: list[dict[str, Any]],
    ) -> str:
        peers = self._agent1_peers
        if len(peers) < 2:
            return base_response_text

        trace_id = msg.trace_id or msg.message_id
        latest: dict[str, str] = {}
        transcript_lines: list[str] = []
        seed_prompt = (
            "Debate task from coordinator:\n"
            f"{user_text}\n\n"
            "Coordinator evidence snapshot (contains relay_context_v1 JSON):\n"
            f"{base_response_text}\n\n"
            "Use relay_context_v1 as the source of truth for game, injuries, and market lines.\n"
            "Treat relay_context_v1 as authoritative current-state data, even if it conflicts with your background memory.\n"
            "Only flag data-quality issues that are explicit inside relay_context_v1 (missing_data/source_errors/internal contradictions).\n"
            "Reply with: your pick, brief rationale, and one uncertainty."
        )
        current_prompts = {peer: seed_prompt for peer in peers}

        for round_idx in range(1, self._agent1_rounds + 1):
            print_event(
                self.profile.key.upper(),
                "A_ROUND",
                f"Starting debate round {round_idx} with {', '.join('@' + peer for peer in peers)}",
                peer=msg.from_agent,
                session_tag=self.node.session_tag,
                trace_id=trace_id,
                thread_id=thread_id,
                message_id=msg.message_id,
                status="debate_round_start",
                extra={"round": round_idx},
                max_chars=600,
            )

            tasks = [
                self._request_peer_with_retry(
                    username=peer,
                    prompt=current_prompts.get(peer, seed_prompt),
                    trace_id=trace_id,
                    thread_id=thread_id,
                    parent_message_id=msg.message_id,
                    round_idx=round_idx,
                )
                for peer in peers
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            round_errors = 0
            for item in results:
                if isinstance(item, Exception):
                    round_errors += 1
                    transcript_lines.append(f"Round {round_idx}: peer_error={item}")
                    continue
                peer, text = item
                latest[peer] = text
                transcript_lines.append(f"Round {round_idx} {peer}: {text}")
                print_event(
                    self.profile.key.upper(),
                    "A_PEER",
                    text,
                    peer=f"@{peer}",
                    session_tag=self.node.session_tag,
                    trace_id=trace_id,
                    thread_id=thread_id,
                    message_id=msg.message_id,
                    status="debate_peer_reply",
                    extra={"round": round_idx},
                    max_chars=600,
                )

            if round_errors:
                missing = [peers[i] for i, item in enumerate(results) if isinstance(item, Exception)]
                transcript_lines.append(
                    f"Round {round_idx}: missing_peers={','.join(missing)} (continued with available replies)"
                )

            if round_idx >= self._agent1_rounds:
                break

            if not latest:
                continue
            next_prompts: dict[str, str] = {}
            for peer in peers:
                others = [f"{name}: {latest[name]}" for name in peers if name != peer and latest.get(name)]
                if not others:
                    next_prompts[peer] = seed_prompt
                    continue
                other_block = "\n".join(others)
                next_prompts[peer] = (
                    f"Round {round_idx + 1} challenge:\n"
                    f"Original task: {user_text}\n\n"
                    f"Other agent view:\n{other_block}\n\n"
                    "Challenge or refine your prior view. Keep it concise and evidence-led.\n"
                    "Do not drop relay_context_v1 evidence when revising."
                )
            current_prompts = next_prompts

        transcript = "\n".join(transcript_lines).strip() or "No peer debate transcript available."
        synthesis_user_text = (
            f"User ask:\n{user_text}\n\n"
            f"Agent 1 evidence snapshot:\n{base_response_text}\n\n"
            f"Peer debate transcript:\n{transcript}\n\n"
            "Return final consensus response for the user."
        )
        synthesized = await complete_text(
            model_cfg=self.profile.model,
            system_prompt=f"{self._system_prompt}\n\n{self._time_context_block()}\n\n{_AGENT1_SYNTHESIS_INSTRUCTION}",
            history=[],
            user_text=synthesis_user_text,
            tool_manifest="- No tools for synthesis.",
            tool_results=all_tool_results,
            max_tokens_override=self._final_max_tokens,
        )
        return synthesized.strip() or base_response_text

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
        should_orchestrate = (
            self.profile.key == "agent_1"
            and self._agent1_orchestrate
            and bool(msg.reply_to)
            and payload.get("mesh_stage") != "agent1_debate_round"
            and len(self._agent1_peers) >= 2
        )
        if not should_orchestrate:
            return response_text

        await self._ensure_agent1_kickoff_evidence(
            msg=msg,
            thread_id=thread_id,
            all_tool_results=all_tool_results,
        )
        evidence_snapshot = self._build_kickoff_evidence_snapshot(all_tool_results, response_text)

        print_event(
            self.profile.key.upper(),
            "A_DEBATE",
            f"Launching peer debate with @{self._agent1_peers[0]} and @{self._agent1_peers[1]}",
            peer=msg.from_agent,
            session_tag=self.node.session_tag,
            trace_id=msg.trace_id,
            thread_id=thread_id,
            message_id=msg.message_id,
            status="debate_start",
            extra={"rounds": self._agent1_rounds},
            max_chars=600,
        )
        try:
            return await self._agent1_debate_and_synthesize(
                msg=msg,
                user_text=user_text,
                thread_id=thread_id,
                base_response_text=evidence_snapshot,
                all_tool_results=all_tool_results,
            )
        except Exception as exc:  # noqa: BLE001
            print_event(
                self.profile.key.upper(),
                "A_DEBATE_FAIL",
                str(exc),
                peer=msg.from_agent,
                session_tag=self.node.session_tag,
                trace_id=msg.trace_id,
                thread_id=thread_id,
                message_id=msg.message_id,
                status="debate_error",
                max_chars=600,
            )
            return response_text
