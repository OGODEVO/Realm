from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional
import json
import logging
import threading
import time
import concurrent.futures
import diskcache
from .nba_client import NBAClient
from .odds_client import OddsClient
from .team_lookup import resolve_team
from .log_context import slog
from .search_tools import get_nba_news

client = NBAClient()
odds_client = OddsClient()
logger = logging.getLogger(__name__)

# Shared thread pool for parallel API calls
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=10)

# NBA schedules use US Eastern Time
_ET = timezone(timedelta(hours=-5))

# Persistent disk cache (thread-safe, survives restarts)
_CACHE = diskcache.Cache("logs/cache")

def _get_today() -> str:
    return datetime.now(_ET).strftime("%Y-%m-%d")

def _get_current_season_year() -> str:
    # Simple logic: if month is > 9 (Oct), it's start of season (e.g. 2023 for 23-24). 
    # If month < 9, it's end of season (still 2023 for 23-24 season usually in API logic, check docs carefully).
    # Docs say: "2017-2018 season = 2017"
    now = datetime.now(_ET)
    if now.month >= 10:
        return str(now.year)
    else:
        return str(now.year - 1)


def _seconds_until_next_et_midnight() -> int:
    now = datetime.now(_ET)
    next_day = (now + timedelta(days=1)).date()
    next_midnight = datetime.combine(next_day, datetime.min.time(), tzinfo=_ET)
    return max(1, int((next_midnight - now).total_seconds()))


def _cache_key(tool_name: str, params: Dict[str, Any]) -> str:
    return f"{tool_name}:{json.dumps(params, sort_keys=True, default=str)}"


def _cache_get(key: str) -> Optional[str]:
    # diskcache automatically handles expiration during .get()
    return _CACHE.get(key)


def _cache_set(key: str, value: str, ttl_seconds: int) -> None:
    if ttl_seconds <= 0:
        return
    # Set the value with an expiration
    _CACHE.set(key, value, expire=ttl_seconds)


def _cached_json(
    tool_name: str,
    params: Dict[str, Any],
    ttl_seconds: int,
    fetcher,
) -> str:
    key = _cache_key(tool_name, params)
    cached = _cache_get(key)
    if cached is not None:
        slog.debug("cache.hit", tool=tool_name, cache_key=key)
        return cached

    slog.debug("cache.miss", tool=tool_name, cache_key=key)
    data = fetcher()
    payload = json.dumps(data)
    if isinstance(data, dict) and "error" in data:
        slog.warning("tool.fetch_error", tool=tool_name, error=data.get("error"))
        return payload

    _cache_set(key, payload, ttl_seconds)
    return payload


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _safe_div(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _team_metrics_from_box(score: Any, team_stats: Dict[str, Any]) -> Dict[str, float]:
    points = _to_float(score)
    fgm = _to_float(team_stats.get("field_goals_made"))
    fga = _to_float(team_stats.get("field_goals_attempted"))
    tpm = _to_float(team_stats.get("three_points_made"))
    tpa = _to_float(team_stats.get("three_points_attempted"))
    fta = _to_float(team_stats.get("free_throws_attempted"))
    orb = _to_float(team_stats.get("offensive_rebounds"))
    turnovers = _to_float(team_stats.get("turnovers"))
    possessions = fga + 0.44 * fta - orb + turnovers

    return {
        "points": points,
        "eFG": round(_safe_div(fgm + 0.5 * tpm, fga), 4),
        "threePA_rate": round(_safe_div(tpa, fga), 4),
        "FT_rate": round(_safe_div(fta, fga), 4),
        "estimated_possessions": round(possessions, 2),
        "OffRtg_est": round(_safe_div(points * 100.0, possessions), 2),
        "TO_rate": round(_safe_div(turnovers, possessions), 4),
        "offensive_rebounds": int(orb),
        "turnovers": int(turnovers),
    }


def _season_metrics(regular_season: Dict[str, Any]) -> Dict[str, Any]:
    fgm = _to_float(regular_season.get("field_goals_made"))
    fga = _to_float(regular_season.get("field_goals_attempted"))
    tpm = _to_float(regular_season.get("three_points_made"))
    tpa = _to_float(regular_season.get("three_points_attempted"))
    fta = _to_float(regular_season.get("free_throws_attempted"))
    orb = _to_float(regular_season.get("offensive_rebounds"))
    drb = _to_float(regular_season.get("defensive_rebounds"))
    trb = _to_float(regular_season.get("total_rebounds"))
    assists = _to_float(regular_season.get("assists"))
    steals = _to_float(regular_season.get("steals"))
    blocks = _to_float(regular_season.get("blocks"))
    turnovers = _to_float(regular_season.get("turnovers"))
    points = _to_float(regular_season.get("points"))
    wins = _to_float(regular_season.get("wins"))
    losses = _to_float(regular_season.get("losses"))
    games_played = _to_float(regular_season.get("games_played"))
    possessions = fga + 0.44 * fta - orb + turnovers
    played_for_pct = (wins + losses) if (wins + losses) > 0 else games_played

    return {
        "points_per_game": round(_safe_div(points, games_played), 2),
        "eFG": round(_safe_div(fgm + 0.5 * tpm, fga), 4),
        "threePA_rate": round(_safe_div(tpa, fga), 4),
        "FT_rate": round(_safe_div(fta, fga), 4),
        "OffRtg_est": round(_safe_div(points * 100.0, possessions), 2),
        "TO_rate": round(_safe_div(turnovers, possessions), 4),
        "offensive_rebounds_per_game": round(_safe_div(orb, games_played), 2),
        "defensive_rebounds_per_game": round(_safe_div(drb, games_played), 2),
        "total_rebounds_per_game": round(_safe_div(trb, games_played), 2),
        "assists_per_game": round(_safe_div(assists, games_played), 2),
        "steals_per_game": round(_safe_div(steals, games_played), 2),
        "blocks_per_game": round(_safe_div(blocks, games_played), 2),
        "turnovers_per_game": round(_safe_div(turnovers, games_played), 2),
        "wins": int(wins),
        "losses": int(losses),
        "win_pct": round(_safe_div(wins, played_for_pct), 3),
        "games_played": int(games_played),
    }


def _delta_metrics(live_metrics: Dict[str, Any], season_metrics: Dict[str, Any]) -> Dict[str, float]:
    keys = ["eFG", "threePA_rate", "FT_rate", "OffRtg_est", "TO_rate"]
    delta: Dict[str, float] = {}
    for key in keys:
        if key in live_metrics and key in season_metrics:
            delta[key] = round(_to_float(live_metrics[key]) - _to_float(season_metrics[key]), 4)
    return delta


def _extract_regular_season(team_stats_payload: Dict[str, Any]) -> Dict[str, Any]:
    rows = team_stats_payload.get("data", {}).get("NBA", [])
    if not isinstance(rows, list) or not rows:
        return {}
    row = rows[0]
    regular = row.get("regular_season", {})
    return regular if isinstance(regular, dict) else {}


def _roster_summary(team_name: str) -> Dict[str, Any]:
    team_id = resolve_team(team_name)
    if not team_id:
        return {"error": f"Could not resolve team '{team_name}'."}

    player_info = client.get_player_info(team_id=team_id)
    injuries = client.get_injuries(team_id=team_id)
    depth_chart = client.get_depth_charts(team_id=team_id)

    players = player_info.get("data", {}).get("NBA", [])
    if not isinstance(players, list):
        players = []
    active_count = 0
    inactive_count = 0
    for player in players:
        status = str(player.get("status", "")).upper()
        if status == "INACT":
            inactive_count += 1
        else:
            active_count += 1

    injuries_rows = injuries.get("data", {}).get("NBA", [])
    injury_list = []
    if isinstance(injuries_rows, list) and injuries_rows:
        injury_list = injuries_rows[0].get("injuries", []) or []

    starters: Dict[str, str] = {}
    depth_nba = depth_chart.get("data", {}).get("NBA", {})
    team_depth = None
    if isinstance(depth_nba, dict):
        team_depth = depth_nba.get(team_name)
        if team_depth is None and len(depth_nba) == 1:
            team_depth = list(depth_nba.values())[0]
    if isinstance(team_depth, dict):
        for pos in ["PG", "SG", "SF", "PF", "C"]:
            position_map = team_depth.get(pos, {})
            if isinstance(position_map, dict) and "1" in position_map:
                starters[pos] = position_map["1"].get("player", "Unknown")

    return {
        "team_name": team_name,
        "team_id": team_id,
        "roster_counts": {
            "players_returned": len(players),
            "active_or_unknown_status": active_count,
            "inactive": inactive_count,
        },
        "injuries": {
            "count": len(injury_list),
            "players": [item.get("player") for item in injury_list[:10] if isinstance(item, dict)],
        },
        "projected_starters": starters,
    }


def _match_event_by_teams(events: list, home_team: str, away_team: str) -> Optional[Dict[str, Any]]:
    home = home_team.lower().strip()
    away = away_team.lower().strip()
    for event in events:
        if not isinstance(event, dict):
            continue
        h = str(event.get("home_team", "")).lower().strip()
        a = str(event.get("away_team", "")).lower().strip()
        if h == home and a == away:
            return event
    for event in events:
        if not isinstance(event, dict):
            continue
        h = str(event.get("home_team", "")).lower().strip()
        a = str(event.get("away_team", "")).lower().strip()
        if (home in h and away in a) or (h in home and a in away):
            return event
    return None


def _is_team_match(outcome_name: str, team_name: str) -> bool:
    o = outcome_name.lower().strip()
    t = team_name.lower().strip()
    return o == t or o in t or t in o


def _to_optional_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _best_priced_outcome(rows: list[Dict[str, Any]]) -> Dict[str, Any]:
    priced = [row for row in rows if isinstance(row.get("price"), (int, float))]
    if not priced:
        return {}
    best = max(priced, key=lambda row: float(row["price"]))
    result = {
        "price": best.get("price"),
        "bookmaker": best.get("bookmaker"),
        "last_update": best.get("last_update"),
    }
    if best.get("point") is not None:
        result["point"] = best.get("point")
    return result


def _mean_or_none(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _normalize_market_lines(event: Dict[str, Any], home_team: str, away_team: str) -> Dict[str, Any]:
    books = event.get("bookmakers", []) or []
    home_h2h: list[Dict[str, Any]] = []
    away_h2h: list[Dict[str, Any]] = []
    home_spreads: list[Dict[str, Any]] = []
    away_spreads: list[Dict[str, Any]] = []
    totals_over: list[Dict[str, Any]] = []
    totals_under: list[Dict[str, Any]] = []
    updates: list[str] = []

    for book in books:
        if not isinstance(book, dict):
            continue
        book_title = str(book.get("title") or book.get("key") or "unknown")
        book_update = str(book.get("last_update") or "")
        if book_update:
            updates.append(book_update)
        for market in book.get("markets", []) or []:
            if not isinstance(market, dict):
                continue
            mkey = str(market.get("key") or "").strip()
            market_update = str(market.get("last_update") or book_update or "")
            if market_update:
                updates.append(market_update)
            for outcome in market.get("outcomes", []) or []:
                if not isinstance(outcome, dict):
                    continue
                name = str(outcome.get("name") or "").strip()
                price = _to_optional_float(outcome.get("price"))
                point = _to_optional_float(outcome.get("point"))
                row = {
                    "price": price,
                    "point": point,
                    "bookmaker": book_title,
                    "last_update": market_update or book_update,
                }
                if mkey == "h2h":
                    if _is_team_match(name, home_team):
                        home_h2h.append(row)
                    elif _is_team_match(name, away_team):
                        away_h2h.append(row)
                elif mkey == "spreads":
                    if _is_team_match(name, home_team):
                        home_spreads.append(row)
                    elif _is_team_match(name, away_team):
                        away_spreads.append(row)
                elif mkey == "totals":
                    lname = name.lower()
                    if lname == "over":
                        totals_over.append(row)
                    elif lname == "under":
                        totals_under.append(row)

    home_spread_points = [row["point"] for row in home_spreads if row.get("point") is not None]
    away_spread_points = [row["point"] for row in away_spreads if row.get("point") is not None]
    over_points = [row["point"] for row in totals_over if row.get("point") is not None]
    under_points = [row["point"] for row in totals_under if row.get("point") is not None]
    total_points = over_points if over_points else under_points

    moneyline = {
        "home": _best_priced_outcome(home_h2h),
        "away": _best_priced_outcome(away_h2h),
    }
    spreads = {
        "home": {
            "consensus_point": _mean_or_none(home_spread_points),
            "best_price": _best_priced_outcome(home_spreads),
        },
        "away": {
            "consensus_point": _mean_or_none(away_spread_points),
            "best_price": _best_priced_outcome(away_spreads),
        },
    }
    totals = {
        "consensus_total": _mean_or_none(total_points),
        "over_best_price": _best_priced_outcome(totals_over),
        "under_best_price": _best_priced_outcome(totals_under),
    }

    return {
        "available": bool(home_h2h or away_h2h or home_spreads or away_spreads or totals_over or totals_under),
        "as_of": max(updates) if updates else None,
        "moneyline": moneyline,
        "spreads": spreads,
        "totals": totals,
    }


def _market_snapshot(home_team: str, away_team: str, regions: str, markets: str) -> Dict[str, Any]:
    odds_data = odds_client.get_odds(
        sport="basketball_nba",
        regions=regions,
        markets=markets,
        date_format="iso",
        odds_format="american",
    )
    if "error" in odds_data:
        return odds_data

    events = odds_data.get("data", [])
    if not isinstance(events, list):
        events = []
    matched = _match_event_by_teams(events, home_team, away_team)
    if not matched:
        return {
            "warning": "No matching odds event found for this matchup.",
            "meta": odds_data.get("meta", {}),
        }

    book_count = len(matched.get("bookmakers", []) or [])
    market_keys = []
    if matched.get("bookmakers"):
        first = matched["bookmakers"][0]
        markets_block = first.get("markets", []) or []
        market_keys = [m.get("key") for m in markets_block if isinstance(m, dict) and m.get("key")]

    return {
        "event_id": matched.get("id"),
        "commence_time": matched.get("commence_time"),
        "home_team": matched.get("home_team"),
        "away_team": matched.get("away_team"),
        "bookmakers_count": book_count,
        "sample_market_keys": market_keys,
        "market_lines": _normalize_market_lines(matched, home_team, away_team),
        "meta": odds_data.get("meta", {}),
    }

# --- Agent Tools ---

def get_daily_schedule(date: str = None, team_name: str = None) -> str:
    """
    Get the NBA schedule for a specific date.
    
    Args:
        date (str, optional): Date in YYYY-MM-DD. Defaults to TODAY if not provided.
        team_name (str, optional): Filter by team name (e.g. "Lakers", "Warriors").
    """
    if not date: date = _get_today()
    
    team_id = None
    if team_name:
        team_id = resolve_team(team_name)
        if not team_id:
            return f"Error: Could not find team '{team_name}'. Please check spelling."

    ttl_seconds = _seconds_until_next_et_midnight() if date == _get_today() else 12 * 60 * 60
    return _cached_json(
        "get_daily_schedule",
        {"date": date, "team_id": team_id},
        ttl_seconds,
        lambda: client.get_schedule(date, team_id=team_id),
    )

def get_weekly_schedule(date: str = None, team_name: str = None) -> str:
    """
    Get the NBA schedule for 7 days starting from the given date.
    
    Args:
        date (str, optional): Start date in YYYY-MM-DD. Defaults to TODAY.
        team_name (str, optional): Filter by team name.
    """
    if not date: date = _get_today()
    
    team_id = None
    if team_name:
        team_id = resolve_team(team_name)
        if not team_id:
            return f"Error: Could not find team '{team_name}'."

    return _cached_json(
        "get_weekly_schedule",
        {"date": date, "team_id": team_id},
        6 * 60 * 60,
        lambda: client.get_weekly_schedule(date, team_id=team_id),
    )

def _find_game_id_for_team(date: str, team_id: int) -> str | None:
    """
    Internal helper: Fetches the daily schedule and finds the game_id 
    for a specific team on a given date. This removes the need for 
    the agent to chain get_daily_schedule -> get_live_scores.
    """
    schedule_json = _cached_json(
        "schedule_lookup_for_live",
        {"date": date, "team_id": team_id},
        _seconds_until_next_et_midnight() if date == _get_today() else 12 * 60 * 60,
        lambda: client.get_schedule(date, team_id=team_id),
    )
    schedule_data = json.loads(schedule_json)
    
    # Parse schedule response to find game_id
    if "error" in schedule_data:
        return None
    games = schedule_data.get("data", {}).get("NBA", [])
    if not games:
        return None
    
    # Return the first matching game_id (usually one per team per day)
    return games[0].get("game_ID")


def _find_scheduled_game_from_full_slate(date: str, team_id: int) -> Dict[str, Any]:
    """
    Fallback lookup when team-filtered schedule fetch fails:
    use cached full-slate schedule for the date and filter by team_id.
    """
    schedule_json = _cached_json(
        "get_daily_schedule",
        {"date": date, "team_id": None},
        _seconds_until_next_et_midnight() if date == _get_today() else 12 * 60 * 60,
        lambda: client.get_schedule(date, team_id=None),
    )
    try:
        schedule_data = json.loads(schedule_json)
    except Exception:
        return {}
    games = schedule_data.get("data", {}).get("NBA", [])
    if not isinstance(games, list):
        return {}
    for game in games:
        if not isinstance(game, dict):
            continue
        home_id = game.get("home_team_ID")
        away_id = game.get("away_team_ID")
        if home_id == team_id or away_id == team_id:
            return game
    return {}


def get_live_scores(date: str = None, team_name: str = None) -> str:
    """
    Get live scores and full boxscore data for games.
    
    SMART ORCHESTRATION: If you provide a team_name, this tool will 
    automatically look up today's schedule to find the correct game_id,
    then fetch the live boxscore for that specific game.
    No need to call get_daily_schedule first.
    
    Args:
        date (str, optional): Date in YYYY-MM-DD. Defaults to TODAY.
        team_name (str, optional): Team name (e.g. "Lakers", "Warriors").
                                   HIGHLY RECOMMENDED to get a specific game's boxscore.
    """
    if not date: date = _get_today()
    
    team_id = None
    game_id = None
    
    if team_name:
        team_id = resolve_team(team_name)
        if not team_id:
            return f"Error: Could not resolve team '{team_name}' to an ID."
        
        # SMART: Auto-resolve game_id from the daily schedule
        game_id = _find_game_id_for_team(date, team_id)
        if not game_id:
            return json.dumps({
                "info": f"No games found for {team_name} on {date}.",
                "suggestion": "Try a different date or check the weekly schedule."
            })
        # API requires EITHER team_id OR game_id, not both
        team_id = None

    data = client.get_live_data(date, team_id=team_id, game_id=game_id)
    fallback_key = f"live_fallback_{date}_{game_id or ''}_{team_id or ''}"
    # Give the LLM clear instructions on what 304 means if they hit it
    if data.get("status") == "No data updates (304)":
        cached_str = _cache_get(fallback_key)
        if cached_str:
            try:
                cached_data = json.loads(cached_str)
                cached_data["_cached_fallback"] = True
                cached_data["_info"] = "The game is live but no new statistics have updated since the last poll. Returning the last known boxscore."
                return json.dumps(cached_data)
            except Exception:
                pass
        return json.dumps({
            "info": "The game is live but no new statistics have updated since the last API poll. "
                    "Wait a few minutes and try again if necessary."
        })
    elif "error" not in data:
        _cache_set(fallback_key, json.dumps(data), 6 * 60 * 60)
        
    return json.dumps(data)

def get_team_details(team_name: str = None) -> str:
    """
    Get general team info (Arena, Conference, etc.).
    
    Args:
        team_name (str, optional): Team name (e.g. "Celtics"). Returns all teams if omitted.
    """
    team_id = None
    if team_name:
        team_id = resolve_team(team_name)
        if not team_id:
            return f"Error: Could not resolve team '{team_name}'."
            
    return _cached_json(
        "get_team_details",
        {"team_id": team_id},
        7 * 24 * 60 * 60,
        lambda: client.get_team_info(team_id=team_id),
    )

def get_team_stats(team_name: str = None, year: str = None) -> str:
    """
    Get season-level team statistics.
    
    Args:
        team_name (str, optional): Team name (e.g. "Nuggets"). 
        year (str, optional): Season start year (e.g. "2023" for 23-24). Defaults to CURRENT season.
    """
    if not year: year = _get_current_season_year()
    
    team_id = None
    if team_name:
        team_id = resolve_team(team_name)
        if not team_id:
            return f"Error: Could not resolve team '{team_name}'."
            
    ttl_seconds = 12 * 60 * 60 if year == _get_current_season_year() else 24 * 60 * 60
    return _cached_json(
        "get_team_stats",
        {"year": year, "team_id": team_id},
        ttl_seconds,
        lambda: client.get_team_stats(year, team_id=team_id),
    )

def get_player_info(team_name: str = None) -> str:
    """
    Get list of players and their details (Position, Height, Age).
    
    Args:
        team_name (str, optional): Filter by team name. 
                                   HIGHLY RECOMMENDED to avoid listing every player in the league.
    """
    team_id = None
    if team_name:
        team_id = resolve_team(team_name)
        if not team_id:
            return f"Error: Could not resolve team '{team_name}'."
            
    return _cached_json(
        "get_player_info",
        {"team_id": team_id},
        12 * 60 * 60,
        lambda: client.get_player_info(team_id=team_id),
    )

def get_player_stats(team_name: str = None, year: str = None) -> str:
    """
    Get season-level player statistics.
    
    Args:
        team_name (str, optional): Filter by team name. 
        year (str, optional): Season start year. Defaults to CURRENT season.
    """
    if not year: year = _get_current_season_year()
    
    team_id = None
    if team_name:
        team_id = resolve_team(team_name)
        if not team_id:
            return f"Error: Could not resolve team '{team_name}'."
            
    ttl_seconds = 12 * 60 * 60 if year == _get_current_season_year() else 24 * 60 * 60
    return _cached_json(
        "get_player_stats",
        {"year": year, "team_id": team_id},
        ttl_seconds,
        lambda: client.get_player_stats(year, team_id=team_id),
    )

def get_injuries(team_name: str = None) -> str:
    """
    Get injury reports.
    
    Args:
        team_name (str, optional): Filter by team name.
    """
    team_id = None
    if team_name:
        team_id = resolve_team(team_name)
        if not team_id:
            return f"Error: Could not resolve team '{team_name}'."
            
    return _cached_json(
        "get_injuries",
        {"team_id": team_id},
        5 * 60,
        lambda: client.get_injuries(team_id=team_id),
    )

def get_depth_chart(team_name: str) -> str:
    """
    Get depth chart for a specific team.
    
    Args:
        team_name (str): Team name (e.g. "Bucks"). REQUIRED.
    """
    team_id = resolve_team(team_name)
    if not team_id:
        return f"Error: Could not resolve team '{team_name}'."
            
    return _cached_json(
        "get_depth_chart",
        {"team_id": team_id},
        10 * 60,
        lambda: client.get_depth_charts(team_id=team_id),
    )


def _depth_chart_team_block(depth_chart_payload: Dict[str, Any], team_name: str) -> Dict[str, Any]:
    depth_nba = depth_chart_payload.get("data", {}).get("NBA", {})
    if isinstance(depth_nba, dict):
        if team_name in depth_nba and isinstance(depth_nba[team_name], dict):
            return depth_nba[team_name]
        if len(depth_nba) == 1:
            only_value = list(depth_nba.values())[0]
            if isinstance(only_value, dict):
                return only_value
    return {}


def _rotation_players_from_depth(team_depth: Dict[str, Any], max_depth: int = 3) -> list[str]:
    players: list[str] = []
    seen = set()
    for pos in ["PG", "SG", "SF", "PF", "C"]:
        pos_map = team_depth.get(pos, {})
        if not isinstance(pos_map, dict):
            continue
        for slot in sorted(pos_map.keys(), key=lambda k: int(k) if str(k).isdigit() else 999):
            if str(slot).isdigit() and int(slot) > max_depth:
                continue
            item = pos_map.get(slot, {})
            if not isinstance(item, dict):
                continue
            player = str(item.get("player", "")).strip()
            if player and player not in seen:
                seen.add(player)
                players.append(player)
    return players


def get_roster_context(team_name: str, include_raw: bool = False) -> str:
    """
    Get a roster integrity bundle for one team with a cleaned summary by default.
    The summary prioritizes depth chart + injuries to reduce stale/historical player noise.

    Args:
        team_name (str): Team name (e.g. "Lakers"). REQUIRED.
        include_raw (bool): Include raw API payloads (player_info/depth_chart/injuries).
    """
    team_id = resolve_team(team_name)
    if not team_id:
        return f"Error: Could not resolve team '{team_name}'."

    def _build_payload() -> Dict[str, Any]:
        # Launch parallel fetches
        future_info = _EXECUTOR.submit(client.get_player_info, team_id=team_id)
        future_depth = _EXECUTOR.submit(client.get_depth_charts, team_id=team_id)
        future_injuries = _EXECUTOR.submit(client.get_injuries, team_id=team_id)

        # Wait for results
        player_info = future_info.result()
        depth_chart = future_depth.result()
        injuries = future_injuries.result()

        players = player_info.get("data", {}).get("NBA", [])
        if not isinstance(players, list):
            players = []
        active_players = [
            p.get("player")
            for p in players
            if isinstance(p, dict) and str(p.get("status", "")).upper() != "INACT" and p.get("player")
        ]
        inactive_count = len(
            [p for p in players if isinstance(p, dict) and str(p.get("status", "")).upper() == "INACT"]
        )

        team_depth = _depth_chart_team_block(depth_chart, team_name)
        projected_starters: Dict[str, str] = {}
        if isinstance(team_depth, dict):
            for pos in ["PG", "SG", "SF", "PF", "C"]:
                pos_map = team_depth.get(pos, {})
                if isinstance(pos_map, dict) and "1" in pos_map and isinstance(pos_map["1"], dict):
                    projected_starters[pos] = pos_map["1"].get("player", "Unknown")
        rotation_players = _rotation_players_from_depth(team_depth, max_depth=3)

        injuries_rows = injuries.get("data", {}).get("NBA", [])
        injury_list = []
        if isinstance(injuries_rows, list) and injuries_rows:
            injury_list = injuries_rows[0].get("injuries", []) or []
        injury_players = [i.get("player") for i in injury_list if isinstance(i, dict) and i.get("player")]

        payload: Dict[str, Any] = {
            "team_name": team_name,
            "team_id": team_id,
            "summary": {
                "projected_starters": projected_starters,
                "rotation_players_depth_1_to_3": rotation_players,
                "active_players_from_player_info": active_players,
                "injury_players": injury_players,
                "counts": {
                    "player_info_rows": len(players),
                    "player_info_inactive_rows": inactive_count,
                    "injury_count": len(injury_players),
                },
                "source_quality_note": (
                    "Depth chart + injuries are prioritized for current roster calls. "
                    "player_info may include inactive/historical rows."
                ),
            },
        }

        if include_raw:
            payload["raw"] = {
                "player_info": player_info,
                "depth_chart": depth_chart,
                "injuries": injuries,
            }
        return payload

    return _cached_json(
        "get_roster_context",
        {"team_id": team_id, "include_raw": include_raw},
        10 * 60,
        _build_payload,
    )


def get_pregame_context(
    team_name: str,
    date: str = None,
    year: str = None,
    include_roster: bool = True,
    include_market: bool = True,
    regions: str = "us",
    markets: str = "h2h,spreads,totals",
) -> str:
    """
    Unified workflow tool: **THE PRE-GAME WEAPON**.
    1) Finds the team's game and opponent on the requested date
    2) Pulls season team stats for both teams
    3) Optionally attaches roster snapshots for both teams
    4) Optionally attaches market snapshot
    """
    if not date:
        date = _get_today()
    if not year:
        year = _get_current_season_year()

    target_team_id = resolve_team(team_name)
    if not target_team_id:
        return json.dumps({"error": f"Could not resolve team '{team_name}'."})

    schedule_data = client.get_schedule(date, team_id=target_team_id)
    games = schedule_data.get("data", {}).get("NBA", [])
    game: Dict[str, Any] = {}
    if isinstance(games, list) and games:
        game = games[0]
    else:
        game = _find_scheduled_game_from_full_slate(date, target_team_id)
    if not game:
        return json.dumps({
            "error": f"No scheduled game found for {team_name} on {date}.",
            "schedule_result": schedule_data,
        })

    game_id = game.get("game_ID")
    if not game_id:
        return json.dumps({"error": "Could not determine game_ID from schedule.", "schedule_game": game})

    home_name = game.get("home_team")
    away_name = game.get("away_team")
    home_id = game.get("home_team_ID")
    away_id = game.get("away_team_ID")

    # Launch parallel fetches for season stats, rosters, and market
    future_home_stats = _EXECUTOR.submit(client.get_team_stats, year, team_id=home_id) if home_id else None
    future_away_stats = _EXECUTOR.submit(client.get_team_stats, year, team_id=away_id) if away_id else None
    
    future_home_roster = None
    if include_roster and home_name:
        future_home_roster = _EXECUTOR.submit(_roster_summary, home_name)
    
    future_away_roster = None
    if include_roster and away_name:
        future_away_roster = _EXECUTOR.submit(_roster_summary, away_name)

    future_market = None
    if include_market and home_name and away_name:
        future_market = _EXECUTOR.submit(_market_snapshot, home_name, away_name, regions=regions, markets=markets)

    # Collect results
    home_season_raw = future_home_stats.result() if future_home_stats else {}
    away_season_raw = future_away_stats.result() if future_away_stats else {}

    home_regular = _extract_regular_season(home_season_raw) if isinstance(home_season_raw, dict) else {}
    away_regular = _extract_regular_season(away_season_raw) if isinstance(away_season_raw, dict) else {}

    home_season_metrics = _season_metrics(home_regular)
    away_season_metrics = _season_metrics(away_regular)

    response: Dict[str, Any] = {
        "query": {
            "team_name": team_name,
            "team_id": target_team_id,
            "date": date,
            "season_year": year,
        },
        "game": {
            "game_id": game_id,
            "event_name": game.get("event_name"),
            "status": game.get("status"),
            "home_team": home_name,
            "away_team": away_name,
        },
        "season_context": {
            "home": {
                "team": home_name,
                "season_metrics": home_season_metrics,
            },
            "away": {
                "team": away_name,
                "season_metrics": away_season_metrics,
            },
        },
        "notes": [
            "Use season_metrics to compare baseline efficiency and pace (estimated_possessions/OffRtg_est).",
            "CRITICAL: The 'game' object above contains the event_name and status. DO NOT ask the user for game status or timing.",
        ],
    }

    if include_roster:
        if future_home_roster:
            response.setdefault("roster", {})["home"] = future_home_roster.result()
        if future_away_roster:
            response.setdefault("roster", {})["away"] = future_away_roster.result()

    if future_market:
        response["market"] = future_market.result()
        market_lines = response["market"].get("market_lines") if isinstance(response.get("market"), dict) else None
        if isinstance(market_lines, dict):
            response["market_lines"] = market_lines

    return json.dumps(response)


def get_live_vs_season_context(
    team_name: str,
    date: str = None,
    year: str = None,
    include_roster: bool = True,
    include_market: bool = True,
    regions: str = "us",
    markets: str = "h2h,spreads,totals",
) -> str:
    """
    Unified workflow tool: **THE HALFTIME WEAPON**.
    1) Finds the team's game on the requested date
    2) Pulls live/final box stats for that game
    3) Pulls season team stats for both teams
    4) Computes live-vs-season metric deltas (Perfect for fading outliers at halftime)
    5) Optionally attaches roster and market snapshots
    """
    if not date:
        date = _get_today()
    if not year:
        year = _get_current_season_year()

    target_team_id = resolve_team(team_name)
    if not target_team_id:
        return json.dumps({"error": f"Could not resolve team '{team_name}'."})

    schedule_data = client.get_schedule(date, team_id=target_team_id)
    games = schedule_data.get("data", {}).get("NBA", [])
    if not isinstance(games, list) or not games:
        return json.dumps({
            "error": f"No scheduled game found for {team_name} on {date}.",
            "schedule_result": schedule_data,
        })

    game = games[0]
    game_id = game.get("game_ID")
    if not game_id:
        return json.dumps({"error": "Could not determine game_ID from schedule.", "schedule_game": game})

    live_data = client.get_live_data(date, game_id=game_id)
    fallback_key = f"live_fallback_{date}_{game_id}_live_data"
    
    if live_data.get("status") == "No data updates (304)":
        cached_str = _cache_get(fallback_key)
        if cached_str:
            try:
                live_data = json.loads(cached_str)
                live_data["_cached_fallback"] = True
            except Exception:
                return json.dumps({
                    "info": f"No new data/updates available for game_id {game_id} since last poll (304 Not Modified).",
                    "suggestion": "Wait a minute and try again."
                })
        else:
            return json.dumps({
                "info": f"No new data/updates available for game_id {game_id} since last poll (304 Not Modified).",
                "suggestion": "Wait a minute and try again."
            })
    elif "error" not in live_data:
        _cache_set(fallback_key, json.dumps(live_data), 6 * 60 * 60)
        
    live_games = live_data.get("data", {}).get("NBA", [])
    if not isinstance(live_games, list) or not live_games:
        return json.dumps({
            "error": f"Live/box data not available or malformed for game_id {game_id}.",
            "live_result": live_data,
        })

    game_box = live_games[0]
    full_box = game_box.get("full_box", {})
    home_box = full_box.get("home_team", {})
    away_box = full_box.get("away_team", {})

    home_name = game_box.get("home_team_name") or game.get("home_team")
    away_name = game_box.get("away_team_name") or game.get("away_team")
    home_id = home_box.get("team_id") or game.get("home_team_ID")
    away_id = away_box.get("team_id") or game.get("away_team_ID")

    # Launch parallel fetches for season stats, rosters, and market
    future_home_stats = _EXECUTOR.submit(client.get_team_stats, year, team_id=home_id) if home_id else None
    future_away_stats = _EXECUTOR.submit(client.get_team_stats, year, team_id=away_id) if away_id else None
    
    future_home_roster = None
    if include_roster and home_name:
        future_home_roster = _EXECUTOR.submit(_roster_summary, home_name)
    
    future_away_roster = None
    if include_roster and away_name:
        future_away_roster = _EXECUTOR.submit(_roster_summary, away_name)

    future_market = None
    if include_market and home_name and away_name:
        future_market = _EXECUTOR.submit(_market_snapshot, home_name, away_name, regions=regions, markets=markets)

    # Collect results
    home_season_raw = future_home_stats.result() if future_home_stats else {}
    away_season_raw = future_away_stats.result() if future_away_stats else {}

    home_regular = _extract_regular_season(home_season_raw) if isinstance(home_season_raw, dict) else {}
    away_regular = _extract_regular_season(away_season_raw) if isinstance(away_season_raw, dict) else {}

    home_live_metrics = _team_metrics_from_box(home_box.get("score"), home_box.get("team_stats", {}))
    away_live_metrics = _team_metrics_from_box(away_box.get("score"), away_box.get("team_stats", {}))
    home_season_metrics = _season_metrics(home_regular)
    away_season_metrics = _season_metrics(away_regular)

    response: Dict[str, Any] = {
        "query": {
            "team_name": team_name,
            "team_id": target_team_id,
            "date": date,
            "season_year": year,
        },
        "game": {
            "game_id": game_id,
            "event_name": game_box.get("event_name") or game.get("event_name"),
            "status": game_box.get("status") or game.get("status"),
            "game_status": game_box.get("game_status"),
            "quarter": full_box.get("current", {}).get("Quarter"),
            "time_remaining": full_box.get("current", {}).get("TimeRemaining"),
            "home_team": home_name,
            "away_team": away_name,
            "home_score": home_box.get("score"),
            "away_score": away_box.get("score"),
        },
        "live_vs_season": {
            "home": {
                "team": home_name,
                "live_metrics": home_live_metrics,
                "season_metrics": home_season_metrics,
                "delta_live_minus_season": _delta_metrics(home_live_metrics, home_season_metrics),
            },
            "away": {
                "team": away_name,
                "live_metrics": away_live_metrics,
                "season_metrics": away_season_metrics,
                "delta_live_minus_season": _delta_metrics(away_live_metrics, away_season_metrics),
            },
        },
        "notes": [
            "Use delta_live_minus_season for regression/context checks.",
            "Live/final team_stats are game-level; season metrics are full-season baselines.",
            "CRITICAL: The 'game' object above contains the current quarter, time_remaining, and scores. DO NOT ask the user for this information.",
        ],
    }

    if include_roster:
        if future_home_roster:
            response.setdefault("roster", {})["home"] = future_home_roster.result()
        if future_away_roster:
            response.setdefault("roster", {})["away"] = future_away_roster.result()

    if future_market:
        response["market"] = future_market.result()

    return json.dumps(response)


def get_market_odds(
    sport: str = "basketball_nba",
    regions: str = "us",
    markets: str = "h2h",
    date_format: str = "iso",
    odds_format: str = "american",
    team_name: str = None,
    event_ids: str = None,
    bookmakers: str = None,
    commence_time_from: str = None,
    commence_time_to: str = None,
    include_links: bool = None,
    include_sids: bool = None,
    include_bet_limits: bool = None,
    include_rotation_numbers: bool = None,
) -> str:
    """
    Get market odds (moneyline / spread / totals) from Odds API.

    Args:
        sport (str, optional): Sport key. Default "basketball_nba".
        regions (str, optional): Region list, e.g. "us" or "us,eu".
        markets (str, optional): "h2h", "spreads", "totals", comma-separated allowed.
        date_format (str, optional): "iso" or "unix".
        odds_format (str, optional): "american" or "decimal".
        team_name (str, optional): Optional team filter. Returns only matching events.
        event_ids (str, optional): Comma-separated event IDs.
        bookmakers (str, optional): Comma-separated bookmaker keys.
        commence_time_from (str, optional): ISO timestamp lower bound.
        commence_time_to (str, optional): ISO timestamp upper bound.
        include_links/include_sids/include_bet_limits/include_rotation_numbers (bool, optional): API flags.
    """
    cache_key = _cache_key(
        "get_market_odds",
        {
            "sport": sport,
            "regions": regions,
            "markets": markets,
            "date_format": date_format,
            "odds_format": odds_format,
            "team_name": team_name.lower().strip() if isinstance(team_name, str) else team_name,
            "event_ids": event_ids,
            "bookmakers": bookmakers,
            "commence_time_from": commence_time_from,
            "commence_time_to": commence_time_to,
            "include_links": include_links,
            "include_sids": include_sids,
            "include_bet_limits": include_bet_limits,
            "include_rotation_numbers": include_rotation_numbers,
        },
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = odds_client.get_odds(
        sport=sport,
        regions=regions,
        markets=markets,
        date_format=date_format,
        odds_format=odds_format,
        event_ids=event_ids,
        bookmakers=bookmakers,
        commence_time_from=commence_time_from,
        commence_time_to=commence_time_to,
        include_links=include_links,
        include_sids=include_sids,
        include_bet_limits=include_bet_limits,
        include_rotation_numbers=include_rotation_numbers,
    )

    if "error" in data:
        return json.dumps(data)

    if team_name:
        needle = team_name.strip().lower()
        events = data.get("data", [])
        filtered = []
        for event in events:
            home = str(event.get("home_team", "")).lower()
            away = str(event.get("away_team", "")).lower()
            if needle in home or needle in away:
                filtered.append(event)

        data["data"] = filtered
        data["meta"] = data.get("meta", {})
        data["meta"]["team_filter"] = team_name
        data["meta"]["events_returned"] = len(filtered)

    payload = json.dumps(data)
    _cache_set(cache_key, payload, 30)
    return payload

# Tool Definitions for OpenAI
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_daily_schedule",
            "description": "Get NBA games schedule for a specific date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD. Default: Today."},
                    "team_name": {"type": "string", "description": "Team filter (e.g. 'Lakers')."}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_live_scores",
            "description": "Get live scores and full boxscores. Automatically finds the correct game when you provide a team name - no need to look up game IDs first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD. Default: Today."},
                    "team_name": {"type": "string", "description": "Team name (e.g. 'Lakers'). Highly recommended."}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_live_vs_season_context",
            "description": "Best-practice live workflow: game snapshot + live team metrics + season baselines + deltas (+ optional roster and market context).",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Team name (e.g. 'Lakers'). REQUIRED."},
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD. Default: Today (ET)."},
                    "year": {"type": "string", "description": "Season start year. Defaults to current season."},
                    "include_roster": {"type": "boolean", "description": "Include roster summaries for both teams."},
                    "include_market": {"type": "boolean", "description": "Include market snapshot for this matchup."},
                    "regions": {"type": "string", "description": "Odds regions when include_market=true (e.g. 'us')."},
                    "markets": {"type": "string", "description": "Odds markets when include_market=true (e.g. 'h2h,spreads,totals')."}
                },
                "required": ["team_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_team_stats",
            "description": "Get season stats for a team.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Team name (e.g. 'Denver')."},
                    "year": {"type": "string", "description": "Season start year. DO NOT SET THIS unless the user asks for a specific past season. Current season is used automatically."}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_player_info",
            "description": "Get player roster info for a team. Use this to verify current personnel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Team name (e.g. 'Lakers'). Highly recommended."}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_player_stats",
            "description": "Get player season stats.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Team name (e.g. 'Warriors'). Filter by team."},
                    "year": {"type": "string", "description": "Season start year. DO NOT SET THIS unless the user asks for a specific past season. Current season is used automatically."}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_injuries",
            "description": "Get injury report.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Team name (e.g. 'Heat')."}
                }
            }
        }
    },
     {
        "type": "function",
        "function": {
            "name": "get_depth_chart",
            "description": "Get team depth chart / rotation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Team name (e.g. 'Bucks')."}
                },
                "required": ["team_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_roster_context",
            "description": "Get cleaned roster context for a team (starters, rotation, injuries). Use this first for roster-sensitive analysis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Team name (e.g. 'Lakers')."},
                    "include_raw": {"type": "boolean", "description": "Set true only if raw payload debugging is needed."}
                },
                "required": ["team_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_odds",
            "description": "Get live betting market odds (h2h/spreads/totals). Use this before making market-based claims.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sport": {"type": "string", "description": "Sport key, default 'basketball_nba'. Use 'upcoming' for cross-sport upcoming events."},
                    "regions": {"type": "string", "description": "Bookmaker regions, e.g. 'us' or 'us,eu'."},
                    "markets": {"type": "string", "description": "Comma-separated markets: h2h,spreads,totals,outrights."},
                    "date_format": {"type": "string", "description": "Timestamp format: iso or unix."},
                    "odds_format": {"type": "string", "description": "Odds format: american or decimal."},
                    "team_name": {"type": "string", "description": "Optional team filter (e.g. 'Lakers')."},
                    "event_ids": {"type": "string", "description": "Optional comma-separated event ids."},
                    "bookmakers": {"type": "string", "description": "Optional comma-separated bookmaker keys."},
                    "commence_time_from": {"type": "string", "description": "Optional ISO-8601 lower bound."},
                    "commence_time_to": {"type": "string", "description": "Optional ISO-8601 upper bound."},
                    "include_links": {"type": "boolean", "description": "Include bookmaker deep links if available."},
                    "include_sids": {"type": "boolean", "description": "Include source IDs if available."},
                    "include_bet_limits": {"type": "boolean", "description": "Include bet limits if available."},
                    "include_rotation_numbers": {"type": "boolean", "description": "Include rotation numbers if available."}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_nba_news",
            "description": "Search for real-time NBA news, injury reports, trade rumors, and team chemistry/vibes. Do NOT use for stats.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query (e.g. 'Luka Doncic injury status', 'Lakers locker room vibes')."}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_pregame_context",
            "description": "Unified workflow tool: Finds the opponent, pulls season stats for both teams, and optionally attaches rosters and odds. PRIMARY PRE-GAME ANALYTICS TOOL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "A target team name, e.g. 'Nuggets'."},
                    "date": {"type": "string", "description": "Date YYYY-MM-DD. Defaults to today."},
                    "year": {"type": "string", "description": "Season start year. Defaults to current."},
                    "include_roster": {"type": "boolean", "description": "Include injury/depth chart info (Default True)."},
                    "include_market": {"type": "boolean", "description": "Include betting lines (Default True)."}
                },
                "required": ["team_name"]
            }
        }
    }
]

AVAILABLE_TOOLS = {
    "get_daily_schedule": get_daily_schedule,
    "get_live_scores": get_live_scores,
    "get_live_vs_season_context": get_live_vs_season_context,
    "get_team_stats": get_team_stats,
    "get_player_info": get_player_info,
    "get_player_stats": get_player_stats,
    "get_injuries": get_injuries,
    "get_depth_chart": get_depth_chart,
    "get_roster_context": get_roster_context,
    "get_market_odds": get_market_odds,
    "get_nba_news": get_nba_news,
    "get_pregame_context": get_pregame_context
}
