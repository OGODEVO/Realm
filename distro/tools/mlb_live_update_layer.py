#!/usr/bin/env python3
"""Plug live Cardinals-Cubs data into the completed dataset.

This updates forward-compatible rows for lineups, umpire, live weather, and live
market data. It does not score, project, or make picks.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path


BASE = Path("/Users/klyexy/.local/share/realm/artifacts/mlb_hacky/2026-07-03")
DATASET = BASE / "cardinals_cubs_completed_dataset.csv"
READINESS = BASE / "cardinals_cubs_feature_readiness.csv"
USER_LIVE_UPDATE = BASE / "user_live_update_1358.json"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def dataset_row(category: str, feature: str, cardinals: str, cubs: str, game: str, source: str, provenance: str, notes: str) -> dict[str, str]:
    return {
        "category": category,
        "feature": feature,
        "cardinals_value": cardinals,
        "cubs_value": cubs,
        "game_value": game,
        "source_file": source,
        "source_lines": provenance,
        "notes": notes,
    }


def replace_rows(rows: list[dict[str, str]], additions: list[dict[str, str]]) -> list[dict[str, str]]:
    keys = {(row["category"], row["feature"]) for row in additions}
    kept = [row for row in rows if (row["category"], row["feature"]) not in keys]
    return kept + additions


def player_lookup() -> dict[str, dict[str, str]]:
    return {row["player_id"]: row for row in read_csv(BASE / "cardinals_cubs_player_metrics.csv")}


def lineup_string(team: dict, players_by_id: dict[str, dict[str, str]]) -> str:
    parts = []
    for index, player_id in enumerate(team.get("battingOrder", []), 1):
        player = team["players"][f"ID{player_id}"]
        person = player["person"]
        metrics = players_by_id.get(str(player_id), {})
        bats = metrics.get("bats", "?")
        position = player.get("position", {}).get("abbreviation") or metrics.get("position", "")
        ops = metrics.get("ops", "")
        parts.append(f"{index}. {person['fullName']} {position} bats {bats} OPS {ops}".strip())
    return " | ".join(parts)


def handedness_summary(team: dict, players_by_id: dict[str, dict[str, str]]) -> str:
    counts = Counter()
    detail = []
    for player_id in team.get("battingOrder", []):
        player = team["players"][f"ID{player_id}"]
        name = player["person"]["fullName"]
        bats = players_by_id.get(str(player_id), {}).get("bats", "?")
        counts[bats] += 1
        detail.append(f"{name}:{bats}")
    count_text = ", ".join(f"{side}={counts[side]}" for side in sorted(counts))
    return f"{count_text}; " + "; ".join(detail)


def extract_espn_game() -> dict:
    data = json.loads((BASE / "espn_scoreboard_refresh.json").read_text())
    for event in data.get("events", []):
        if "Cardinals" in event.get("name", "") and "Cubs" in event.get("name", ""):
            return event
    raise RuntimeError("Cardinals-Cubs event not found in ESPN refresh")


def build_live_rows() -> list[dict[str, str]]:
    live = json.loads((BASE / "mlb_live_feed_824659.json").read_text())
    box = json.loads((BASE / "mlb_boxscore_824659.json").read_text())
    pitchers = json.loads((BASE / "mlb_probable_pitcher_people.json").read_text())
    espn_event = extract_espn_game()
    players_by_id = player_lookup()

    away = box["teams"]["away"]
    home = box["teams"]["home"]
    officials = box.get("officials", [])
    home_plate = next((row for row in officials if row.get("officialType") == "Home Plate"), {})
    hp_name = home_plate.get("official", {}).get("fullName", "")
    hp_id = str(home_plate.get("official", {}).get("id", ""))

    pitcher_hands = {str(p["id"]): p.get("pitchHand", {}).get("code", "?") for p in pitchers.get("people", [])}
    matchup_context = f"Cardinals bats vs Cubs starter David Peterson ({pitcher_hands.get('656849', '?')}HP); Cubs bats vs Cardinals starter Andre Pallante ({pitcher_hands.get('669467', '?')}HP)"

    odds = espn_event["competitions"][0].get("odds", [{}])[0]
    moneyline = odds.get("moneyline", {})
    spread = odds.get("pointSpread", {})
    total = odds.get("total", {})
    weather = espn_event.get("weather", {})
    mlb_weather = live.get("gameData", {}).get("weather", {})

    return [
        dataset_row("market_live", "moneyline_live", moneyline.get("away", {}).get("close", {}).get("odds", ""), moneyline.get("home", {}).get("close", {}).get("odds", ""), "", "espn_scoreboard_refresh.json", "events[].competitions[0].odds[0].moneyline.close", "DraftKings via ESPN refresh"),
        dataset_row("market_live", "moneyline_open_to_live", moneyline.get("away", {}).get("open", {}).get("odds", ""), moneyline.get("home", {}).get("open", {}).get("odds", ""), f"live details {odds.get('details', '')}", "espn_scoreboard_refresh.json", "events[].competitions[0].odds[0].moneyline.open/close", "Line movement reference; not a pick"),
        dataset_row("market_live", "runline_live", f"{spread.get('away', {}).get('close', {}).get('line', '')} {spread.get('away', {}).get('close', {}).get('odds', '')}".strip(), f"{spread.get('home', {}).get('close', {}).get('line', '')} {spread.get('home', {}).get('close', {}).get('odds', '')}".strip(), "", "espn_scoreboard_refresh.json", "events[].competitions[0].odds[0].pointSpread.close", "DraftKings via ESPN refresh"),
        dataset_row("market_live", "total_live", total.get("over", {}).get("close", {}).get("odds", ""), total.get("under", {}).get("close", {}).get("odds", ""), str(odds.get("overUnder", "")), "espn_scoreboard_refresh.json", "events[].competitions[0].odds[0].total.close + overUnder", "Over odds in Cardinals column; Under odds in Cubs column"),
        dataset_row("market_live", "total_open_to_live", total.get("over", {}).get("open", {}).get("odds", ""), total.get("under", {}).get("open", {}).get("odds", ""), f"open {total.get('over', {}).get('open', {}).get('line', '')}/{total.get('under', {}).get('open', {}).get('line', '')}; live {total.get('over', {}).get('close', {}).get('line', '')}/{total.get('under', {}).get('close', {}).get('line', '')}", "espn_scoreboard_refresh.json", "events[].competitions[0].odds[0].total.open/close", "Line movement reference; not a pick"),
        dataset_row("weather_live", "mlb_weather", "", "", f"{mlb_weather.get('condition', '')}; {mlb_weather.get('temp', '')}F; wind {mlb_weather.get('wind', '')}", "mlb_live_feed_824659.json", "gameData.weather", "MLB live feed weather"),
        dataset_row("weather_live", "espn_weather_refresh", "", "", f"{weather.get('displayValue', '')}; {weather.get('temperature', '')}F", "espn_scoreboard_refresh.json", "event.weather", "ESPN/AccuWeather refresh"),
        dataset_row("lineups", "official_batting_orders", lineup_string(away, players_by_id), lineup_string(home, players_by_id), "", "mlb_boxscore_824659.json", "teams.away/home.battingOrder + players", "Official batting orders from MLB boxscore"),
        dataset_row("lineups", "handedness_splits", handedness_summary(away, players_by_id), handedness_summary(home, players_by_id), matchup_context, "mlb_boxscore_824659.json / cardinals_cubs_player_metrics.csv / mlb_probable_pitcher_people.json", "battingOrder + player_metrics.bats + people.pitchHand", "Lineup handedness from official orders and collected player metrics"),
        dataset_row("umpire", "home_plate_umpire", "", "", f"{hp_name} ({hp_id})", "mlb_boxscore_824659.json", "officials[officialType=Home Plate]", "Home plate umpire assignment from MLB boxscore"),
        dataset_row("bullpen", "bullpen_roster_live", ", ".join(str(x) for x in away.get("bullpen", [])), ", ".join(str(x) for x in home.get("bullpen", [])), "", "mlb_boxscore_824659.json", "teams.away/home.bullpen", "Bullpen roster IDs available; exact pitch counts/rest still missing"),
    ]


def build_user_live_rows() -> list[dict[str, str]]:
    if not USER_LIVE_UPDATE.exists():
        return []

    update = json.loads(USER_LIVE_UPDATE.read_text())
    moneyline = update["moneyline"]
    run_line = update["run_line"]
    alt = update["alternate_run_lines"]
    totals = update["totals"]
    weather = update["weather"]
    umpires = update["game_info"]["umpires"]
    source = USER_LIVE_UPDATE.name
    stamp = update.get("timestamp_local", "")

    total_ladder = " | ".join(
        f"O{line} {prices['over']} / U{line} {prices['under']}" for line, prices in totals.items()
    )
    alt_ladder = " | ".join(f"{name.replace('_', ' ')} {price}" for name, price in alt.items())

    return [
        dataset_row("market_user_live", "moneyline_decimal", moneyline["st_louis_cardinals"], moneyline["chicago_cubs"], "", source, "user supplied odds at " + stamp, "Decimal odds; Cardinals in Cardinals column, Cubs in Cubs column"),
        dataset_row("market_user_live", "runline_decimal", run_line["cardinals_+1.5"], run_line["cubs_-1.5"], "", source, "user supplied odds at " + stamp, "Primary run line decimal odds"),
        dataset_row("market_user_live", "alternate_runline_ladder_decimal", "", "", alt_ladder, source, "user supplied odds at " + stamp, "Alternate run line ladder, preserved as supplied"),
        dataset_row("market_user_live", "total_ladder_decimal", "", "", total_ladder, source, "user supplied odds at " + stamp, "Full O/U ladder, preserved as supplied"),
        dataset_row("market_user_live", "total_10_5_decimal", totals["10.5"]["over"], totals["10.5"]["under"], "10.5", source, "user supplied odds at " + stamp, "Main total line from user source; Over odds in Cardinals column, Under odds in Cubs column"),
        dataset_row("weather_user_live", "weather_snapshot", "", "", f"{weather['conditions']}; {weather['temperature_f']}F; wind {weather['wind']}; precip {weather['precipitation']}", source, "user supplied weather at " + stamp, "User-provided live weather snapshot; preserved alongside MLB/ESPN weather"),
        dataset_row("weather_user_live", "temperature_f", "", "", weather["temperature_f"], source, "user supplied weather at " + stamp, "User-provided temperature"),
        dataset_row("weather_user_live", "conditions", "", "", weather["conditions"], source, "user supplied weather at " + stamp, "User-provided condition"),
        dataset_row("weather_user_live", "wind", "", "", weather["wind"], source, "user supplied weather at " + stamp, "User-provided wind; no ballpark direction effect inferred"),
        dataset_row("weather_user_live", "precipitation", "", "", weather["precipitation"], source, "user supplied weather at " + stamp, "User-provided precipitation chance"),
        dataset_row("umpire_user_live", "crew", "", "", f"HP {umpires['home_plate']}; 1B {umpires['first_base']}; 2B {umpires['second_base']}; 3B {umpires['third_base']}", source, "user supplied umpire crew at " + stamp, "Matches MLB boxscore umpire crew by last name"),
    ]


def update_readiness() -> None:
    rows = read_csv(READINESS)
    fields = list(rows[0].keys())
    replacements = {
        ("lineups", "official_batting_orders"): {
            "current_status": "current",
            "current_source": "mlb_boxscore_824659.json",
            "use_now": "yes",
            "replace_later": "yes",
            "confidence": "high",
            "replacement_trigger": "lineup scratch or late change",
            "notes": "Official MLB batting orders are available and plugged into lineups.official_batting_orders",
        },
        ("lineups", "projected_lineups"): {
            "current_status": "superseded",
            "current_source": "mlb_boxscore_824659.json",
            "use_now": "no",
            "replace_later": "no",
            "confidence": "none",
            "replacement_trigger": "none",
            "notes": "Projected lineups no longer needed because official batting orders are available",
        },
        ("umpire", "home_plate_umpire"): {
            "current_status": "current",
            "current_source": "mlb_boxscore_824659.json",
            "use_now": "yes",
            "replace_later": "yes",
            "confidence": "high",
            "replacement_trigger": "umpire crew correction",
            "notes": "Home plate umpire is available from MLB boxscore",
        },
        ("bullpen", "exact_reliever_pitch_counts"): {
            "current_status": "optional_downgraded",
            "current_source": "none",
            "use_now": "no",
            "replace_later": "yes",
            "confidence": "none",
            "replacement_trigger": "optional pitch-count source fetched",
            "notes": "Downgraded from blocker. Exact pitch counts/rest remain useful enrichment but are not required for current sports-metric readiness.",
        },
        ("bullpen", "recent_game_workload_proxy"): {
            "current_status": "current_limited",
            "current_source": "mlb_recent_json / mlb_boxscore_824659.json",
            "use_now": "yes",
            "replace_later": "yes",
            "confidence": "low",
            "replacement_trigger": "optional exact reliever pitch counts fetched",
            "notes": "Use recent workload proxy plus live bullpen roster IDs. Exact pitch counts are optional enrichment, not a blocker.",
        },
    }
    for row in rows:
        key = (row["feature_group"], row["feature"])
        if key in replacements:
            row.update(replacements[key])
    write_csv(READINESS, fields, rows)


def main() -> None:
    rows = read_csv(DATASET)
    fields = list(rows[0].keys())
    rows = replace_rows(rows, build_live_rows() + build_user_live_rows())
    write_csv(DATASET, fields, rows)
    update_readiness()
    print(f"dataset_rows={len(rows)}")
    print("updated live rows: market_live, market_user_live, weather_live, weather_user_live, lineups, umpire, bullpen_roster_live")


if __name__ == "__main__":
    main()
