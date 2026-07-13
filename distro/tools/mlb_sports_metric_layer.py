#!/usr/bin/env python3
"""Build a sports semantic layer for MLB total-market analysis.

This converts raw collected facts into named sports concepts. It does not score,
project runs, or make picks. The output is a forward-compatible metric layer:
missing metrics keep stable IDs until their inputs arrive.
"""

from __future__ import annotations

import csv
from pathlib import Path


BASE = Path("/Users/klyexy/.local/share/realm/artifacts/mlb_hacky/2026-07-03")
COMPLETED_DATASET = BASE / "cardinals_cubs_completed_dataset.csv"
FEATURE_READINESS = BASE / "cardinals_cubs_feature_readiness.csv"
METRICS_CSV = BASE / "cardinals_cubs_sports_metrics.csv"
METRICS_MD = BASE / "cardinals_cubs_sports_metrics.md"
DICTIONARY_CSV = BASE / "cardinals_cubs_sports_metric_dictionary.csv"
VALIDATION_CSV = BASE / "cardinals_cubs_sports_metric_validation.csv"
VALIDATION_MD = BASE / "cardinals_cubs_sports_metric_validation.md"
ENGINE_READ_CSV = BASE / "cardinals_cubs_engine_read.csv"
ENGINE_READ_MD = BASE / "cardinals_cubs_engine_read.md"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class DatasetLookup:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = {(r["category"], r["feature"]): r for r in rows}

    def has_row(self, category: str, feature: str) -> bool:
        return (category, feature) in self.rows

    def row(self, category: str, feature: str) -> dict[str, str]:
        return self.rows.get(
            (category, feature),
            {
                "cardinals_value": "",
                "cubs_value": "",
                "game_value": "",
                "source_file": "none",
                "source_lines": "",
                "notes": "not collected",
            },
        )

    def pair(self, category: str, feature: str) -> str:
        r = self.row(category, feature)
        left = r.get("cardinals_value", "")
        right = r.get("cubs_value", "")
        game = r.get("game_value", "")
        if left or right:
            return f"Cardinals: {left}; Cubs: {right}"
        return game

    def source(self, *items: tuple[str, str]) -> str:
        sources = []
        for category, feature in items:
            source = self.row(category, feature).get("source_file", "none")
            if source and source not in sources:
                sources.append(source)
        return " / ".join(sources) or "none"


DICTIONARY = [
    {
        "metric_id": "total_market_anchor",
        "metric_name": "Total Market Anchor",
        "market": "total",
        "metric_family": "market",
        "definition": "The current sportsbook total and over/under prices. This is the number a later model compares against, not a directional pick by itself.",
        "required_inputs": "current total line, over price, under price",
        "future_update_trigger": "line movement before bet placement",
    },
    {
        "metric_id": "wrigley_wind_run_boost",
        "metric_name": "Wrigley Wind Run Boost",
        "market": "total",
        "metric_family": "weather",
        "definition": "Wind direction and speed at Wrigley that can change fly-ball carry and home-run conditions.",
        "required_inputs": "wind speed, wind direction/effect, late-game wind",
        "future_update_trigger": "weather refresh closer to first pitch",
    },
    {
        "metric_id": "hr_carry_environment",
        "metric_name": "Home Run Carry Environment",
        "market": "total",
        "metric_family": "weather",
        "definition": "External home-run environment signal such as HRForce plus temperature and wind context.",
        "required_inputs": "HRForce or equivalent, temperature, wind",
        "future_update_trigger": "weather refresh closer to first pitch",
    },
    {
        "metric_id": "weather_volatility_risk",
        "metric_name": "Weather Volatility Risk",
        "market": "total",
        "metric_family": "weather",
        "definition": "Rain/thunderstorm conflict or delay risk that can change starter length and bullpen usage.",
        "required_inputs": "rain chance, condition, delay risk",
        "future_update_trigger": "weather refresh closer to first pitch",
    },
    {
        "metric_id": "ballpark_run_environment",
        "metric_name": "Ballpark Run Environment",
        "market": "total",
        "metric_family": "venue",
        "definition": "Park and venue context that tells the model whether run creation is weather-sensitive or stable.",
        "required_inputs": "venue, indoor/outdoor, park context",
        "future_update_trigger": "none unless venue changes",
    },
    {
        "metric_id": "starter_run_risk",
        "metric_name": "Starter Run Risk",
        "market": "total",
        "metric_family": "starting_pitching",
        "definition": "Whether the probable starters create or suppress early scoring risk through ERA, WHIP, walks, home runs, and strikeout profile.",
        "required_inputs": "confirmed starters, ERA, WHIP, K, BB, HR, K/9, BB/9, HR/9",
        "future_update_trigger": "official starter confirmation or stat feed refresh",
    },
    {
        "metric_id": "starter_contact_profile",
        "metric_name": "Starter Contact Profile",
        "market": "total",
        "metric_family": "starting_pitching",
        "definition": "Whether starters are likely to put balls in play or miss bats. More balls in play can raise total volatility.",
        "required_inputs": "K/9, BB/9, HR/9, WHIP",
        "future_update_trigger": "official/stat feed refresh",
    },
    {
        "metric_id": "team_offense_base",
        "metric_name": "Team Offense Base",
        "market": "total",
        "metric_family": "offense",
        "definition": "Baseline team run creation from runs per game, OBP, SLG, OPS, and home runs.",
        "required_inputs": "runs per game, OBP, SLG, OPS, HR",
        "future_update_trigger": "stat feed refresh",
    },
    {
        "metric_id": "recent_run_environment",
        "metric_name": "Recent Run Environment",
        "market": "total",
        "metric_family": "recent_form",
        "definition": "Recent scoring and run prevention trends before the game.",
        "required_inputs": "recent runs for, recent runs against, recent sample size",
        "future_update_trigger": "new game result or better recency window",
    },
    {
        "metric_id": "power_bat_concentration",
        "metric_name": "Power Bat Concentration",
        "market": "total",
        "metric_family": "player_offense",
        "definition": "Whether top HR/OPS/RBI threats are available in the player pool and later confirmed in the lineup.",
        "required_inputs": "top HR leaders, top OPS leaders, lineup status",
        "future_update_trigger": "official lineups posted",
    },
    {
        "metric_id": "bvp_power_signal",
        "metric_name": "BvP Power Signal",
        "market": "total",
        "metric_family": "player_matchup",
        "definition": "Batter-vs-pitcher power or OPS history. It should stay low confidence unless sample size and lineup confirmation are good.",
        "required_inputs": "BvP PA, BvP OPS, BvP HR, official lineup",
        "future_update_trigger": "official lineups posted",
    },
    {
        "metric_id": "lineup_run_creation",
        "metric_name": "Confirmed Lineup Run Creation",
        "market": "total",
        "metric_family": "lineup",
        "definition": "Run creation quality of the actual batting orders, not the roster proxy.",
        "required_inputs": "official batting orders, player OBP, SLG, OPS, batting order slots",
        "future_update_trigger": "official lineups posted",
    },
    {
        "metric_id": "lineup_handedness_matchup",
        "metric_name": "Lineup Handedness Matchup",
        "market": "total",
        "metric_family": "lineup",
        "definition": "How the confirmed lineup handedness matches the opposing starter and likely bullpen lanes.",
        "required_inputs": "official batting orders, batter handedness, pitcher handedness, split stats if available",
        "future_update_trigger": "official lineups posted",
    },
    {
        "metric_id": "bullpen_availability",
        "metric_name": "Bullpen Availability",
        "market": "total",
        "metric_family": "bullpen",
        "definition": "Whether bullpen context is available enough for a current read. Live bullpen roster and recent workload proxy are usable; exact pitch counts are optional enrichment.",
        "required_inputs": "live bullpen roster, recent workload proxy; optional exact pitch counts/rest",
        "future_update_trigger": "optional pitch-count source fetched",
    },
    {
        "metric_id": "umpire_zone_environment",
        "metric_name": "Umpire Zone Environment",
        "market": "total",
        "metric_family": "umpire",
        "definition": "Home plate umpire tendency that can expand/squeeze the zone and affect walks, strikeouts, and run environment.",
        "required_inputs": "home plate umpire, zone tendency, over/under history if available",
        "future_update_trigger": "umpire assignment posted",
    },
]


REQUIRED_FEATURE_ROWS = {
    "total_market_anchor": [
        ("market_user_live", "total_10_5_decimal"),
        ("market_user_live", "total_ladder_decimal"),
        ("market_live", "total_live"),
    ],
    "wrigley_wind_run_boost": [
        ("weather_user_live", "wind"),
    ],
    "hr_carry_environment": [
        ("weather_user_live", "temperature_f"),
        ("weather_user_live", "conditions"),
    ],
    "weather_volatility_risk": [
        ("weather_user_live", "weather_snapshot"),
        ("weather_user_live", "precipitation"),
    ],
    "ballpark_run_environment": [("game_metadata", "venue"), ("venue_web", "indoor")],
    "starter_run_risk": [
        ("starting_pitcher_metrics_api", "era"),
        ("starting_pitcher_metrics_api", "whip"),
        ("starting_pitcher_metrics_api", "strikeouts_walks_home_runs"),
    ],
    "starter_contact_profile": [("starting_pitcher_metrics_api", "rate_stats")],
    "team_offense_base": [
        ("overview", "runs_per_game"),
        ("batting_running", "ops"),
        ("batting_running", "home_runs"),
    ],
    "recent_run_environment": [
        ("recent_game_web", "cardinals_recent_aggregate"),
        ("recent_game_web", "cubs_recent_aggregate"),
    ],
    "power_bat_concentration": [
        ("player_leaders", "home_runs_top_3"),
        ("player_leaders", "ops_top_3"),
    ],
    "bvp_power_signal": [
        ("bvp_pre_lineup", "home_run_history"),
        ("bvp_pre_lineup", "top_ops_matchups"),
    ],
    "lineup_run_creation": [("lineups", "official_batting_orders")],
    "lineup_handedness_matchup": [("lineups", "official_batting_orders"), ("lineups", "handedness_splits")],
    "bullpen_availability": [("bullpen", "bullpen_roster_live")],
    "umpire_zone_environment": [("umpire", "home_plate_umpire")],
}


def metric_row(
    metric_id: str,
    status: str,
    current_value: str,
    sports_read: str,
    direction: str,
    confidence: str,
    source_files: str,
    source_features: str,
    missing_inputs: str,
    replacement_trigger: str,
    notes: str,
) -> dict[str, str]:
    definition = next(item for item in DICTIONARY if item["metric_id"] == metric_id)
    return {
        "metric_id": metric_id,
        "metric_name": definition["metric_name"],
        "market": definition["market"],
        "metric_family": definition["metric_family"],
        "status": status,
        "current_value": current_value,
        "sports_read": sports_read,
        "direction": direction,
        "confidence": confidence,
        "source_files": source_files,
        "source_features": source_features,
        "missing_inputs": missing_inputs,
        "replacement_trigger": replacement_trigger,
        "notes": notes,
    }


def build_metrics(lookup: DatasetLookup) -> list[dict[str, str]]:
    total_close = lookup.row("market_web", "total_close")
    total_live = lookup.row("market_live", "total_live")
    total_user = lookup.row("market_user_live", "total_10_5_decimal")
    total_ladder = lookup.row("market_user_live", "total_ladder_decimal")
    total_value = "; ".join(
        [
            f"user 10.5 decimal: over {total_user['cardinals_value']} / under {total_user['cubs_value']}",
            f"user ladder: {total_ladder['game_value']}",
            f"ESPN/DK live {total_live['game_value']}: over {total_live['cardinals_value']} / under {total_live['cubs_value']}",
            f"original ESPN/DK {total_close['game_value']}: over {total_close['cardinals_value']} / under {total_close['cubs_value']}",
        ]
    )

    wind_value = "; ".join(
        [
            "authoritative user weather wind: " + lookup.row("weather_user_live", "wind")["game_value"],
            "venue: " + lookup.row("game_metadata", "venue")["game_value"],
        ]
    )
    hr_value = "; ".join(
        [
            "authoritative user temp: " + lookup.row("weather_user_live", "temperature_f")["game_value"] + "F",
            "conditions: " + lookup.row("weather_user_live", "conditions")["game_value"],
            "older HRForce context: " + lookup.row("weather_web", "wrigley_hrforce")["game_value"],
        ]
    )
    weather_conflict = "; ".join(
        [
            "authoritative user weather: " + lookup.row("weather_user_live", "weather_snapshot")["game_value"],
            "precipitation: " + lookup.row("weather_user_live", "precipitation")["game_value"],
            "older API feeds retained as context only",
        ]
    )
    starter_rates = lookup.pair("starting_pitcher_metrics_api", "rate_stats")
    starter_run = "; ".join(
        [
            lookup.pair("starting_pitcher_metrics_api", "era"),
            lookup.pair("starting_pitcher_metrics_api", "whip"),
            lookup.pair("starting_pitcher_metrics_api", "strikeouts_walks_home_runs"),
        ]
    )
    offense_base = "; ".join(
        [
            lookup.pair("overview", "runs_per_game"),
            lookup.pair("batting_running", "ops"),
            lookup.pair("batting_running", "home_runs"),
        ]
    )
    power_bats = "; ".join(
        [
            "HR: " + lookup.pair("player_leaders", "home_runs_top_3"),
            "OPS: " + lookup.pair("player_leaders", "ops_top_3"),
        ]
    )
    recent_environment = "; ".join(
        [
            "Cardinals: " + lookup.row("recent_game_web", "cardinals_recent_aggregate")["cardinals_value"],
            "Cubs: " + lookup.row("recent_game_web", "cubs_recent_aggregate")["cubs_value"],
        ]
    )
    official_lineup = lookup.pair("lineups", "official_batting_orders")
    handedness = lookup.pair("lineups", "handedness_splits")
    umpire = lookup.row("umpire", "home_plate_umpire")["game_value"]

    return [
        metric_row(
            "total_market_anchor",
            "current",
            total_value,
            "This is the market anchor only. It tells us the number to compare later, not whether to take over or under.",
            "context_only",
            "high",
            lookup.source(("market_user_live", "total_10_5_decimal"), ("market_user_live", "total_ladder_decimal"), ("market_live", "total_live")),
            "market_user_live.total_10_5_decimal; market_user_live.total_ladder_decimal; market_live.total_live",
            "final line before bet placement",
            "line movement before bet placement",
            "Do not treat the total line itself as a pick. User decimal odds and ESPN/DK American odds are both preserved.",
        ),
        metric_row(
            "wrigley_wind_run_boost",
            "current_authoritative",
            wind_value,
            "Use the user-provided live wind as authoritative for the current read. Older weather feeds are retained only as context.",
            "current_context",
            "medium",
            lookup.source(("weather_user_live", "wind")),
            "weather_user_live.wind",
            "optional newer weather snapshot",
            "new user/weather source update",
            "Authoritative weather per user direction: 11 mph WSW.",
        ),
        metric_row(
            "hr_carry_environment",
            "current_authoritative",
            hr_value,
            "Use the user-provided 67F partly cloudy weather as current. Older HRForce remains context only, not a blocker.",
            "current_context",
            "medium",
            lookup.source(("weather_user_live", "temperature_f"), ("weather_user_live", "conditions")),
            "weather_user_live.temperature_f; weather_user_live.conditions",
            "optional newer weather snapshot",
            "new user/weather source update",
            "Authoritative weather per user direction: 67F and partly cloudy. Older HRForce is context only.",
        ),
        metric_row(
            "weather_volatility_risk",
            "current_authoritative",
            weather_conflict,
            "Use the user-provided 0% precipitation and partly cloudy condition as authoritative; volatility is not blocked by older conflicting feeds.",
            "current_context_low_precip",
            "high",
            lookup.source(("weather_user_live", "weather_snapshot"), ("weather_user_live", "precipitation")),
            "weather_user_live.weather_snapshot; weather_user_live.precipitation",
            "optional newer weather snapshot",
            "new user/weather source update",
            "Authoritative weather per user direction: partly cloudy, 0% precipitation.",
        ),
        metric_row(
            "ballpark_run_environment",
            "current",
            f"{lookup.row('game_metadata', 'venue')['game_value']}; indoor={lookup.row('venue_web', 'indoor')['game_value']}",
            "Outdoor Wrigley means weather matters more than in a dome. This is context for weather metrics.",
            "context_weather_sensitive",
            "high",
            lookup.source(("game_metadata", "venue"), ("venue_web", "indoor")),
            "game_metadata.venue; venue_web.indoor",
            "none",
            "none",
            "Not a standalone over/under signal without weather.",
        ),
        metric_row(
            "starter_run_risk",
            "current",
            starter_run,
            "Watch early run risk through starter profiles. Peterson's ERA/WHIP profile is the clearer run-risk flag; Pallante is less risky but not a shutdown profile.",
            "over_watch_mixed",
            "medium",
            lookup.source(("starting_pitcher_metrics_api", "era"), ("starting_pitcher_metrics_api", "whip")),
            "starting_pitcher_metrics_api.era; starting_pitcher_metrics_api.whip; starting_pitcher_metrics_api.strikeouts_walks_home_runs",
            "official starter confirmation",
            "official starter confirmation or stat feed refresh",
            "This reads profiles only. No run projection is made.",
        ),
        metric_row(
            "starter_contact_profile",
            "current",
            starter_rates,
            "Mixed contact profile. Use this to explain whether starter risk comes from walks, balls in play, or home-run exposure.",
            "mixed_watch",
            "high",
            lookup.source(("starting_pitcher_metrics_api", "rate_stats")),
            "starting_pitcher_metrics_api.rate_stats",
            "official starter confirmation",
            "official/stat feed refresh",
            "Rate stats are available; final interpretation should include lineup handedness later.",
        ),
        metric_row(
            "team_offense_base",
            "current_screenshot",
            offense_base,
            "Cubs show the stronger base offense profile in the collected data. That is an over-watch input, especially if lineup confirms key bats.",
            "over_watch",
            "medium",
            lookup.source(("overview", "runs_per_game"), ("batting_running", "ops"), ("batting_running", "home_runs")),
            "overview.runs_per_game; batting_running.ops; batting_running.home_runs",
            "official/stat feed refresh and official lineups",
            "official lineups posted",
            "Roster/team-level signal must be refined with actual batting orders.",
        ),
        metric_row(
            "recent_run_environment",
            "computed_proxy",
            recent_environment,
            "Recent form points to a high-scoring Cubs environment and weaker Cardinals recent run prevention. Keep as a proxy until fresher/game-state data arrives.",
            "over_watch",
            "medium",
            "mlb_cardinals_recent.json / mlb_cubs_recent.json",
            "recent_game_web.cardinals_recent_aggregate; recent_game_web.cubs_recent_aggregate",
            "updated recency window if needed",
            "new game result / better recency window",
            "Recent samples can be noisy; useful as context, not projection.",
        ),
        metric_row(
            "power_bat_concentration",
            "current_roster_proxy",
            power_bats,
            "Power exists in both player pools. This becomes much more important when official lineups confirm which bats are active and where they hit.",
            "over_watch_pending_lineups",
            "medium",
            lookup.source(("player_leaders", "home_runs_top_3"), ("player_leaders", "ops_top_3")),
            "player_leaders.home_runs_top_3; player_leaders.ops_top_3",
            "official batting orders",
            "official lineups posted",
            "Forward-compatible roster proxy. Replace with confirmed lineup strength later.",
        ),
        metric_row(
            "bvp_power_signal",
            "current_limited",
            lookup.pair("bvp_pre_lineup", "home_run_history") + "; " + lookup.pair("bvp_pre_lineup", "top_ops_matchups"),
            "BvP has some power flags, but sample sizes are small and lineups are not confirmed. Keep low confidence.",
            "low_confidence_over_watch",
            "low",
            lookup.source(("bvp_pre_lineup", "home_run_history"), ("bvp_pre_lineup", "top_ops_matchups")),
            "bvp_pre_lineup.home_run_history; bvp_pre_lineup.top_ops_matchups",
            "official batting orders and larger sample context",
            "official lineups posted",
            "Do not let BvP dominate any decision.",
        ),
        metric_row(
            "lineup_run_creation",
            "current",
            official_lineup,
            "Official lineups are now plugged in. Use this as the lineup run-creation concept instead of the roster-only proxy.",
            "current_context",
            "high",
            lookup.source(("lineups", "official_batting_orders")),
            "lineups.official_batting_orders plus player metrics",
            "late scratches or lineup changes",
            "lineup scratch or late change",
            "Official MLB batting orders with collected player OPS are available. Still no hidden projection.",
        ),
        metric_row(
            "lineup_handedness_matchup",
            "current",
            handedness,
            "Lineup handedness is now plugged in against the listed starter handedness. Split-specific production can be added later if collected.",
            "current_context",
            "medium",
            lookup.source(("lineups", "official_batting_orders"), ("lineups", "handedness_splits")),
            "lineups.official_batting_orders; lineups.handedness_splits",
            "batter split stats if available",
            "split stats source fetched or lineup scratch",
            "Batter handedness and starter hands are available; platoon split production is not yet collected.",
        ),
        metric_row(
            "bullpen_availability",
            "current_limited",
            lookup.pair("bullpen", "bullpen_roster_live"),
            "Bullpen roster availability is plugged in. Exact pitch counts/rest are downgraded to optional improvement, not a blocker.",
            "current_context_limited",
            "low",
            lookup.source(("bullpen", "bullpen_roster_live")),
            "bullpen.bullpen_roster_live",
            "exact reliever pitch counts/rest if found later",
            "optional pitch-count source fetched",
            "Downgraded per user direction: enough bullpen context for current read; exact pitch counts remain future enrichment.",
        ),
        metric_row(
            "umpire_zone_environment",
            "current_assignment",
            umpire,
            "Home plate umpire assignment is now available. Zone tendency/history still needs a separate source before it becomes directional.",
            "current_context_needs_tendency",
            "medium",
            lookup.source(("umpire", "home_plate_umpire")),
            "umpire.home_plate_umpire",
            "umpire zone tendency or over/under history",
            "umpire tendency source fetched",
            "Assignment is current; tendency data is not collected, so this should not be over/under directional yet.",
        ),
    ]


def write_markdown(metrics: list[dict[str, str]]) -> None:
    lines = [
        "# Cardinals-Cubs Sports Metrics Layer",
        "",
        "This converts the current data into named sports concepts for the total market. It does not do math, project runs, or make a pick.",
        "",
        "## How To Use",
        "",
        "- Treat `metric_id` as the stable plug-in point.",
        "- When missing data arrives, update the same metric row instead of inventing a new concept.",
        "- Use `direction` as a watch label, not a bet signal.",
        "- Use `status`, `confidence`, and `missing_inputs` to decide whether the metric is ready.",
        "- Final scoring, if any, must come from a visible script/formula, not hidden AI math.",
        "",
        "## Forward-Compatible Slots",
        "",
        "- `lineup_run_creation`: official batting orders plus player metrics.",
        "- `lineup_handedness_matchup`: batter handedness and splits versus starter/bullpen arms.",
        "- `bullpen_availability`: live bullpen roster plus recent workload proxy; exact pitch counts are optional enrichment.",
        "- `umpire_zone_environment`: home plate umpire zone/run tendency.",
        "- `total_market_anchor`: final line and odds before placement.",
        "",
        "## Current Over Watch Concepts",
        "",
    ]
    for row in metrics:
        if "over_watch" in row["direction"]:
            lines.append(f"- `{row['metric_id']}`: {row['sports_read']} Current: {row['current_value']}")

    lines.extend(["", "## Monitor Or Plug Later", ""])
    for row in metrics:
        if row["direction"] in {"monitor", "plug_later", "monitor_until_exact"}:
            lines.append(f"- `{row['metric_id']}`: status `{row['status']}`. Missing: {row['missing_inputs']}.")

    lines.extend(
        [
            "",
            "## Full Metric Table",
            "",
            "| Metric ID | Family | Status | Direction | Confidence | Current Value | Sports Read | Missing Inputs |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in metrics:
        lines.append(
            f"| {md(row['metric_id'])} | {md(row['metric_family'])} | {md(row['status'])} | {md(row['direction'])} | {md(row['confidence'])} | {md(row['current_value'])} | {md(row['sports_read'])} | {md(row['missing_inputs'])} |"
        )
    lines.append("")
    METRICS_MD.write_text("\n".join(lines), encoding="utf-8")


def feature_value(row: dict[str, str]) -> str:
    values = []
    if row.get("cardinals_value"):
        values.append("Cardinals: " + row["cardinals_value"])
    if row.get("cubs_value"):
        values.append("Cubs: " + row["cubs_value"])
    if row.get("game_value"):
        values.append("Game: " + row["game_value"])
    return "; ".join(values)


def md(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def build_validation(lookup: DatasetLookup, metrics: list[dict[str, str]]) -> list[dict[str, str]]:
    metric_lookup = {row["metric_id"]: row for row in metrics}
    rows: list[dict[str, str]] = []
    for metric_id, features in REQUIRED_FEATURE_ROWS.items():
        metric = metric_lookup[metric_id]
        found_count = 0
        value_count = 0
        for category, feature in features:
            exists = lookup.has_row(category, feature)
            source_row = lookup.row(category, feature)
            value = feature_value(source_row)
            if exists:
                found_count += 1
            if value:
                value_count += 1

            rows.append(
                {
                    "metric_id": metric_id,
                    "metric_status": metric["status"],
                    "metric_direction": metric["direction"],
                    "required_feature": f"{category}.{feature}",
                    "feature_found": "yes" if exists else "no",
                    "value_found": "yes" if value else "no",
                    "current_value": value,
                    "source_file": source_row.get("source_file", "none"),
                    "source_lines": source_row.get("source_lines", ""),
                    "notes": source_row.get("notes", ""),
                }
            )

    return rows


def write_validation_markdown(validation_rows: list[dict[str, str]]) -> None:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in validation_rows:
        grouped.setdefault(row["metric_id"], []).append(row)

    lines = [
        "# Cardinals-Cubs Sports Metric Validation",
        "",
        "This verifies how each sports metric calls the completed dataset. It does not score, project runs, or make a pick.",
        "",
        "## How Verification Works",
        "",
        "- Each metric has a fixed dependency list of `category.feature` rows.",
        "- The validator checks whether each row exists in `cardinals_cubs_completed_dataset.csv`.",
        "- It then checks whether the row has a Cardinals, Cubs, or game value.",
        "- Source file, source lines, and notes are copied from the dataset for provenance.",
        "- Missing future inputs stay missing until they are plugged in; optional enrichment does not block metric readiness.",
        "",
        "## Metric Summary",
        "",
        "| Metric ID | Feature Rows | Found | With Values | Status |",
        "| --- | --- | --- | --- | --- |",
    ]
    for metric_id, rows in grouped.items():
        total = len(rows)
        found = sum(1 for r in rows if r["feature_found"] == "yes")
        valued = sum(1 for r in rows if r["value_found"] == "yes")
        status = "ready" if total == found == valued else "missing_inputs"
        if found and found < total:
            status = "partial"
        if found == total and valued < total:
            status = "blank_values"
        lines.append(f"| {md(metric_id)} | {total} | {found} | {valued} | {status} |")

    lines.extend(
        [
            "",
            "## Missing Or Future Inputs",
            "",
        ]
    )
    for row in validation_rows:
        if row["feature_found"] == "no" or row["value_found"] == "no":
            lines.append(f"- `{row['metric_id']}` needs `{row['required_feature']}`: found={row['feature_found']}, value={row['value_found']}.")

    lines.extend(
        [
            "",
            "## Full Validation Table",
            "",
            "| Metric ID | Required Feature | Found | Value | Current Value | Source | Lines |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in validation_rows:
        lines.append(
            f"| {md(row['metric_id'])} | {md(row['required_feature'])} | {md(row['feature_found'])} | {md(row['value_found'])} | {md(row['current_value'])} | {md(row['source_file'])} | {md(row['source_lines'])} |"
        )
    lines.append("")
    VALIDATION_MD.write_text("\n".join(lines), encoding="utf-8")


def build_engine_read(metrics: list[dict[str, str]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for metric in metrics:
        metric_id = metric["metric_id"]
        direction = metric["direction"]
        if metric_id == "total_market_anchor":
            section = "market_anchor"
        elif metric["metric_family"] == "weather":
            section = "authoritative_weather"
        elif "over_watch" in direction:
            section = "over_watch_concepts"
        elif direction.startswith("current_context"):
            section = "ready_context"
        elif metric["confidence"] == "low" or "limited" in metric["status"]:
            section = "limited_context"
        else:
            section = "supporting_context"

        rows.append(
            {
                "section": section,
                "metric_id": metric_id,
                "status": metric["status"],
                "direction": direction,
                "confidence": metric["confidence"],
                "engine_read": metric["sports_read"],
                "current_value": metric["current_value"],
            }
        )
    return rows


def write_engine_read(rows: list[dict[str, str]]) -> None:
    fields = ["section", "metric_id", "status", "direction", "confidence", "engine_read", "current_value"]
    write_csv(ENGINE_READ_CSV, fields, rows)

    sections = [
        ("market_anchor", "Market Anchor"),
        ("authoritative_weather", "Authoritative Weather"),
        ("over_watch_concepts", "Over Watch Concepts"),
        ("ready_context", "Ready Context"),
        ("limited_context", "Limited Context"),
        ("supporting_context", "Supporting Context"),
    ]
    lines = [
        "# Cardinals-Cubs Engine Read",
        "",
        "This is the no-math engine read. It does not project runs, create an edge, or make a pick.",
        "",
        "Weather policy: use `user_live_update_1358.json` as authoritative current weather. Older API/weather feeds are retained as context only.",
        "",
    ]
    for key, title in sections:
        section_rows = [row for row in rows if row["section"] == key]
        if not section_rows:
            continue
        lines.extend([f"## {title}", ""])
        for row in section_rows:
            lines.append(f"- `{row['metric_id']}` ({row['status']}, {row['confidence']}): {row['engine_read']} Current: {row['current_value']}")
        lines.append("")
    ENGINE_READ_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    lookup = DatasetLookup(read_csv(COMPLETED_DATASET))
    read_csv(FEATURE_READINESS)  # validates expected upstream file exists
    metrics = build_metrics(lookup)
    validation_rows = build_validation(lookup, metrics)
    engine_rows = build_engine_read(metrics)

    dictionary_fields = [
        "metric_id",
        "metric_name",
        "market",
        "metric_family",
        "definition",
        "required_inputs",
        "future_update_trigger",
    ]
    metric_fields = [
        "metric_id",
        "metric_name",
        "market",
        "metric_family",
        "status",
        "current_value",
        "sports_read",
        "direction",
        "confidence",
        "source_files",
        "source_features",
        "missing_inputs",
        "replacement_trigger",
        "notes",
    ]
    validation_fields = [
        "metric_id",
        "metric_status",
        "metric_direction",
        "required_feature",
        "feature_found",
        "value_found",
        "current_value",
        "source_file",
        "source_lines",
        "notes",
    ]
    write_csv(DICTIONARY_CSV, dictionary_fields, DICTIONARY)
    write_csv(METRICS_CSV, metric_fields, metrics)
    write_csv(VALIDATION_CSV, validation_fields, validation_rows)
    write_markdown(metrics)
    write_validation_markdown(validation_rows)
    write_engine_read(engine_rows)

    print(f"dictionary_rows={len(DICTIONARY)}")
    print(f"sports_metric_rows={len(metrics)}")
    print(f"validation_rows={len(validation_rows)}")
    print(f"engine_read_rows={len(engine_rows)}")
    print(f"metrics_csv={METRICS_CSV}")
    print(f"metrics_md={METRICS_MD}")
    print(f"validation_csv={VALIDATION_CSV}")
    print(f"validation_md={VALIDATION_MD}")
    print(f"engine_read_csv={ENGINE_READ_CSV}")
    print(f"engine_read_md={ENGINE_READ_MD}")


if __name__ == "__main__":
    main()
