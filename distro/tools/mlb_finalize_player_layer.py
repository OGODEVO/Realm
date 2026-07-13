#!/usr/bin/env python3
"""Integrate player-level MLB artifacts into the Cardinals-Cubs data layer.

This script is intentionally narrow: it reads the already-collected CSV artifacts
and appends sourced summary rows to the completed dataset/readiness sheets.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path


BASE = Path("/Users/klyexy/.local/share/realm/artifacts/mlb_hacky/2026-07-03")
DATASET = BASE / "cardinals_cubs_completed_dataset.csv"
DATASET_MD = BASE / "cardinals_cubs_completed_dataset.md"
READINESS = BASE / "cardinals_cubs_feature_readiness.csv"
READINESS_MD = BASE / "cardinals_cubs_feature_readiness.md"
BLUEPRINT = BASE / "cardinals_cubs_scoring_blueprint.csv"
BLUEPRINT_MD = BASE / "cardinals_cubs_scoring_blueprint.md"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def as_float(value: str) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def top_values(leaders: list[dict[str, str]], team: str, board: str, count: int = 3) -> str:
    rows = [r for r in leaders if r["team"] == team and r["leaderboard"] == board]
    rows.sort(key=lambda r: int(r["rank"] or 99))
    return "; ".join(f"{r['player']} {r['value']}" for r in rows[:count])


def row(category: str, feature: str, cardinals: str, cubs: str, game: str, source: str, lines: str, notes: str) -> dict[str, str]:
    return {
        "category": category,
        "feature": feature,
        "cardinals_value": cardinals,
        "cubs_value": cubs,
        "game_value": game,
        "source_file": source,
        "source_lines": lines,
        "notes": notes,
    }


def readiness_row(
    feature_group: str,
    feature: str,
    market: str,
    moneyline_weight: str,
    total_weight: str,
    current_status: str,
    current_source: str,
    use_now: str,
    replace_later: str,
    confidence: str,
    replacement_trigger: str,
    notes: str,
) -> dict[str, str]:
    return {
        "feature_group": feature_group,
        "feature": feature,
        "market": market,
        "moneyline_weight": moneyline_weight,
        "total_weight": total_weight,
        "current_status": current_status,
        "current_source": current_source,
        "use_now": use_now,
        "replace_later": replace_later,
        "confidence": confidence,
        "replacement_trigger": replacement_trigger,
        "notes": notes,
    }


def markdown_table(rows: list[dict[str, str]]) -> list[str]:
    lines = ["| Feature | Cardinals | Cubs | Game | Source | Provenance | Notes |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        lines.append(
            "| {feature} | {cardinals_value} | {cubs_value} | {game_value} | {source_file} | {source_lines} | {notes} |".format(**r)
        )
    return lines


def write_dataset_markdown(rows: list[dict[str, str]]) -> None:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in rows:
        grouped[item["category"]].append(item)

    order = []
    for item in rows:
        if item["category"] not in order:
            order.append(item["category"])

    lines = [
        "# Cardinals-Cubs Completed Dataset",
        "",
        "This is the completed data layer for `STL @ CHC` before feature scoring or picks. It merges screenshot-extracted facts with web/API facts needed to fill missing odds, weather, recent-game context, player metrics, starter metrics, and pre-lineup BvP.",
        "",
        "No pick is made in this file.",
        "",
        "## Artifacts",
        "",
        "- Screenshot-only CSV: `cardinals_cubs_game_dataset.csv`",
        "- Completed CSV: `cardinals_cubs_completed_dataset.csv`",
        "- Raw screenshot OCR: `cardinals_cubs_screenshots_ocr_raw.txt`",
        "- ESPN raw JSON: `espn_scoreboard.json`",
        "- MLB recent Cardinals JSON: `mlb_cardinals_recent.json`",
        "- MLB recent Cubs JSON: `mlb_cubs_recent.json`",
        "- Wrigley weather excerpt: `wrigley_weather_source_excerpt.md`",
        "- Player metrics CSV: `cardinals_cubs_player_metrics.csv`",
        "- Top player leaders CSV: `cardinals_cubs_top_player_leaders.csv`",
        "- Starting pitcher metrics CSV: `cardinals_cubs_starting_pitcher_metrics.csv`",
        "- BvP metrics CSV: `cardinals_cubs_bvp_metrics.csv`",
        "",
    ]
    for category in order:
        title = category.replace("_", " ").title()
        lines.extend([f"## {title}", ""])
        lines.extend(markdown_table(grouped[category]))
        lines.append("")
    DATASET_MD.write_text("\n".join(lines), encoding="utf-8")


def write_readiness_markdown(rows: list[dict[str, str]]) -> None:
    lines = [
        "# Cardinals-Cubs Feature Readiness",
        "",
        "No pick is made in this file. It only records which features are usable now, which are estimated, and what should replace them later.",
        "",
        "| Feature Group | Feature | Market | ML Weight | Total Weight | Status | Source | Use Now | Replace Later | Confidence | Replacement Trigger | Notes |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        lines.append(
            "| {feature_group} | {feature} | {market} | {moneyline_weight} | {total_weight} | {current_status} | {current_source} | {use_now} | {replace_later} | {confidence} | {replacement_trigger} | {notes} |".format(**r)
        )
    lines.append("")
    READINESS_MD.write_text("\n".join(lines), encoding="utf-8")


def write_scoring_blueprint(readiness_rows: list[dict[str, str]]) -> None:
    confidence_multiplier = {"high": 1.0, "medium": 0.75, "low": 0.5, "none": 0.0}
    markets = [("Moneyline", "moneyline_weight"), ("Total", "total_weight")]
    rows: list[dict[str, str]] = []

    for market_name, weight_key in markets:
        active = [
            r
            for r in readiness_rows
            if r["use_now"] == "yes"
            and r["market"] in (market_name, "Both")
            and as_float(r[weight_key]) > 0
        ]
        raw_sum = sum(as_float(r[weight_key]) for r in active) or 1.0
        for r in active:
            raw_weight = as_float(r[weight_key])
            normalized = raw_weight / raw_sum
            multiplier = confidence_multiplier.get(r["confidence"], 0.0)
            rows.append(
                {
                    "market": market_name,
                    "feature_group": r["feature_group"],
                    "feature": r["feature"],
                    "raw_weight": f"{raw_weight:.4f}",
                    "normalized_weight": f"{normalized:.4f}",
                    "confidence": r["confidence"],
                    "confidence_multiplier": f"{multiplier:.2f}",
                    "confidence_adjusted_weight": f"{normalized * multiplier:.4f}",
                    "status": r["current_status"],
                    "source": r["current_source"],
                    "replacement_trigger": r["replacement_trigger"],
                    "notes": r["notes"],
                }
            )

    fields = [
        "market",
        "feature_group",
        "feature",
        "raw_weight",
        "normalized_weight",
        "confidence",
        "confidence_multiplier",
        "confidence_adjusted_weight",
        "status",
        "source",
        "replacement_trigger",
        "notes",
    ]
    write_csv(BLUEPRINT, fields, rows)

    lines = [
        "# Cardinals-Cubs Scoring Blueprint",
        "",
        "No pick is made in this file. This is the model contract for the next scoring step.",
        "",
        "## Rules",
        "",
        "- Use only rows with `use_now=yes` from `cardinals_cubs_feature_readiness.csv`.",
        "- Normalize active weights separately for Moneyline and Total.",
        "- Apply confidence as a diagnostic multiplier, not as a hidden replacement for the raw weights.",
        "- Replace roster/player proxies when official lineups post.",
        "- Exact bullpen pitch counts/rest are optional enrichment; current limited bullpen context is usable.",
        "- Do not score unavailable fields as zero; exclude them until collected.",
        "",
        "## Current Gaps Before Final Bet Decision",
        "",
        "- Official batting orders are current from MLB boxscore; monitor only for late scratches.",
        "- Home plate umpire assignment is current; zone tendency/history remains optional enrichment.",
        "- Exact bullpen reliever pitch counts/rest are optional enrichment, not a current blocker.",
        "- BvP is current-limited because it uses selected top roster hitters, not confirmed lineups.",
        "",
    ]
    for market_name in ["Moneyline", "Total"]:
        market_rows = [r for r in rows if r["market"] == market_name]
        raw_total = sum(as_float(r["raw_weight"]) for r in market_rows)
        adjusted_total = sum(as_float(r["confidence_adjusted_weight"]) for r in market_rows)
        lines.extend([f"## {market_name}", "", f"- Active raw weight sum before normalization: `{raw_total:.2f}`", f"- Confidence-adjusted normalized sum: `{adjusted_total:.2f}`", ""])
        lines.extend([
            "| Feature Group | Feature | Raw | Normalized | Confidence | Adjusted | Status | Source |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ])
        for r in market_rows:
            lines.append(
                f"| {r['feature_group']} | {r['feature']} | {r['raw_weight']} | {r['normalized_weight']} | {r['confidence']} | {r['confidence_adjusted_weight']} | {r['status']} | {r['source']} |"
            )
        lines.append("")
    BLUEPRINT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    dataset_rows = read_csv(DATASET)
    dataset_fields = list(dataset_rows[0].keys())
    leaders = read_csv(BASE / "cardinals_cubs_top_player_leaders.csv")
    hitters = read_csv(BASE / "cardinals_cubs_player_metrics.csv")
    starters = read_csv(BASE / "cardinals_cubs_starting_pitcher_metrics.csv")
    bvp = read_csv(BASE / "cardinals_cubs_bvp_metrics.csv")

    starter_by_team = {r["team"]: r for r in starters}
    bvp_by_team: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in bvp:
        bvp_by_team[item["batter_team"]].append(item)

    def best_bvp(team: str) -> str:
        rows = [r for r in bvp_by_team[team] if as_float(r["plateAppearances"]) > 0]
        rows.sort(key=lambda r: (as_float(r["ops"]), as_float(r["plateAppearances"])), reverse=True)
        return "; ".join(f"{r['batter']} OPS {r['ops']} ({r['plateAppearances']} PA)" for r in rows[:3])

    def bvp_hr(team: str) -> str:
        rows = [r for r in bvp_by_team[team] if as_float(r["homeRuns"]) > 0]
        rows.sort(key=lambda r: as_float(r["homeRuns"]), reverse=True)
        return "; ".join(f"{r['batter']} {r['homeRuns']} HR" for r in rows) or "none in collected BvP sample"

    integration_rows = [
        row("player_metrics", "hitter_rows_collected", "", "", str(len(hitters)), "cardinals_cubs_player_metrics.csv", "all rows", "MLB StatsAPI roster hitting metrics for both teams"),
        row("player_metrics", "leader_rows_collected", "", "", str(len(leaders)), "cardinals_cubs_top_player_leaders.csv", "all rows", "computed MLB roster leaderboards plus ESPN embedded leaders"),
        row("player_metrics", "bvp_rows_collected", "", "", str(len(bvp)), "cardinals_cubs_bvp_metrics.csv", "all rows", "pre-lineup selected roster hitters only; replace with official lineup BvP when lineups post"),
        row("player_leaders", "runs_top_3", top_values(leaders, "Cardinals", "runs"), top_values(leaders, "Cubs", "runs"), "", "cardinals_cubs_top_player_leaders.csv", "leaderboard=runs", "computed from MLB StatsAPI roster hitting"),
        row("player_leaders", "home_runs_top_3", top_values(leaders, "Cardinals", "home_runs"), top_values(leaders, "Cubs", "home_runs"), "", "cardinals_cubs_top_player_leaders.csv", "leaderboard=home_runs", "computed from MLB StatsAPI roster hitting"),
        row("player_leaders", "rbi_top_3", top_values(leaders, "Cardinals", "rbi"), top_values(leaders, "Cubs", "rbi"), "", "cardinals_cubs_top_player_leaders.csv", "leaderboard=rbi", "computed from MLB StatsAPI roster hitting"),
        row("player_leaders", "ops_top_3", top_values(leaders, "Cardinals", "ops"), top_values(leaders, "Cubs", "ops"), "", "cardinals_cubs_top_player_leaders.csv", "leaderboard=ops", "computed from MLB StatsAPI roster hitting; minimum 50 PA"),
        row("starting_pitcher_metrics_api", "era", starter_by_team["Cardinals"]["era"], starter_by_team["Cubs"]["era"], "", "cardinals_cubs_starting_pitcher_metrics.csv", "era", "MLB StatsAPI season pitching; confirms screenshot values"),
        row("starting_pitcher_metrics_api", "whip", starter_by_team["Cardinals"]["whip"], starter_by_team["Cubs"]["whip"], "", "cardinals_cubs_starting_pitcher_metrics.csv", "whip", "MLB StatsAPI season pitching; confirms screenshot values"),
        row("starting_pitcher_metrics_api", "strikeouts_walks_home_runs", f"{starter_by_team['Cardinals']['strikeOuts']} K / {starter_by_team['Cardinals']['baseOnBalls']} BB / {starter_by_team['Cardinals']['homeRuns']} HR", f"{starter_by_team['Cubs']['strikeOuts']} K / {starter_by_team['Cubs']['baseOnBalls']} BB / {starter_by_team['Cubs']['homeRuns']} HR", "", "cardinals_cubs_starting_pitcher_metrics.csv", "strikeOuts/baseOnBalls/homeRuns", "MLB StatsAPI season pitching"),
        row("starting_pitcher_metrics_api", "rate_stats", f"K/9 {starter_by_team['Cardinals']['strikeoutsPer9Inn']} / BB/9 {starter_by_team['Cardinals']['walksPer9Inn']} / HR/9 {starter_by_team['Cardinals']['homeRunsPer9']}", f"K/9 {starter_by_team['Cubs']['strikeoutsPer9Inn']} / BB/9 {starter_by_team['Cubs']['walksPer9Inn']} / HR/9 {starter_by_team['Cubs']['homeRunsPer9']}", "", "cardinals_cubs_starting_pitcher_metrics.csv", "strikeoutsPer9Inn/walksPer9Inn/homeRunsPer9", "MLB StatsAPI season pitching"),
        row("bvp_pre_lineup", "top_ops_matchups", best_bvp("Cardinals"), best_bvp("Cubs"), "", "cardinals_cubs_bvp_metrics.csv", "selected top roster hitters", "Batter-vs-pitcher for selected top roster hitters; not official lineup"),
        row("bvp_pre_lineup", "home_run_history", bvp_hr("Cardinals"), bvp_hr("Cubs"), "", "cardinals_cubs_bvp_metrics.csv", "homeRuns > 0", "BvP HR history in collected sample"),
    ]

    existing_keys = {(r["category"], r["feature"]) for r in dataset_rows}
    dataset_rows.extend(r for r in integration_rows if (r["category"], r["feature"]) not in existing_keys)
    write_csv(DATASET, dataset_fields, dataset_rows)
    write_dataset_markdown(dataset_rows)

    readiness_rows = read_csv(READINESS)
    readiness_fields = list(readiness_rows[0].keys())
    readiness_additions = [
        readiness_row("player_metrics", "roster_hitting_metrics", "Both", "0.04", "0.04", "current", "cardinals_cubs_player_metrics.csv", "yes", "yes", "medium", "official lineups posted / stat feed refresh", "Roster hitting metrics are available, but confirmed lineup weights should replace roster-level proxy."),
        readiness_row("player_metrics", "top_scorers_power", "Both", "0.03", "0.06", "current", "cardinals_cubs_top_player_leaders.csv", "yes", "yes", "medium", "official lineups posted", "Top runs, HR, RBI, and OPS leaders computed from MLB StatsAPI roster hitting."),
        readiness_row("player_metrics", "starter_api_rate_stats", "Both", "0.04", "0.04", "current", "cardinals_cubs_starting_pitcher_metrics.csv", "yes", "yes", "high", "official/stat feed refresh", "Adds K/9, BB/9, HR/9, K/BB, pitches per inning, and confirms screenshot starter stats."),
        readiness_row("bvp", "selected_roster_bvp", "Both", "0.02", "0.03", "current_limited", "cardinals_cubs_bvp_metrics.csv", "yes", "yes", "low", "official lineups posted", "StatsAPI vsPlayer data collected for selected top roster hitters only; small samples and not lineup-confirmed."),
    ]
    existing_readiness = {(r["feature_group"], r["feature"]) for r in readiness_rows}
    readiness_rows.extend(r for r in readiness_additions if (r["feature_group"], r["feature"]) not in existing_readiness)
    write_csv(READINESS, readiness_fields, readiness_rows)
    write_readiness_markdown(readiness_rows)
    write_scoring_blueprint(readiness_rows)

    print(f"completed_dataset_rows={len(dataset_rows)}")
    print(f"feature_readiness_rows={len(readiness_rows)}")
    print(f"scoring_blueprint_rows={len(read_csv(BLUEPRINT))}")
    print(f"player_metric_rows={len(hitters)}")
    print(f"leader_rows={len(leaders)}")
    print(f"bvp_rows={len(bvp)}")


if __name__ == "__main__":
    main()
