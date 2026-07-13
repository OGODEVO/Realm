#!/usr/bin/env python3
"""Build a small ontology/semantic layer for the Olist e-commerce dataset.

This first slice focuses on delivery reliability. It loads the raw Olist CSVs
into SQLite, creates canonical delivery cases, computes derived metrics, and
ranks sellers with an explainable weighted score.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CSV_TABLES = {
    "olist_customers_dataset.csv": "customers",
    "olist_orders_dataset.csv": "orders",
    "olist_order_items_dataset.csv": "order_items",
    "olist_order_payments_dataset.csv": "payments",
    "olist_order_reviews_dataset.csv": "reviews",
    "olist_products_dataset.csv": "products",
    "olist_sellers_dataset.csv": "sellers",
    "product_category_name_translation.csv": "product_category_translation",
}

SELLER_SCORE_WEIGHTS = {
    "late_rate_percentile": 0.40,
    "avg_lateness_percentile": 0.25,
    "bad_review_rate_percentile": 0.25,
    "order_volume_percentile": 0.10,
}


@dataclass
class SellerMetric:
    seller_id: str
    seller_state: str | None
    order_count: int
    item_count: int
    late_order_count: int
    bad_review_count: int
    avg_lateness_days: float
    p95_lateness_days: float
    avg_review_score: float
    avg_late_order_review_score: float
    total_price: float
    total_freight: float
    avg_freight_ratio: float
    late_rate: float
    bad_review_rate: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build/query Olist semantic scorecards.")
    parser.add_argument("--input", type=Path, required=True, help="Directory containing Olist CSV files.")
    parser.add_argument("--db", type=Path, required=True, help="SQLite database path to create or query.")

    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="Load CSVs and build semantic views/tables.")
    build.add_argument("--force", action="store_true", help="Delete and rebuild existing database.")
    build.add_argument("--report", type=Path, help="Optional markdown report path.")

    top_sellers = subparsers.add_parser("top-sellers", help="Print top seller delivery pain scores.")
    top_sellers.add_argument("--limit", type=int, default=20)
    top_sellers.add_argument("--min-orders", type=int, default=50)
    top_sellers.add_argument("--json", action="store_true", help="Emit JSON instead of table text.")

    cases = subparsers.add_parser("late-cases", help="Print late delivery cases.")
    cases.add_argument("--limit", type=int, default=20)
    cases.add_argument("--seller-id")
    cases.add_argument("--min-lateness-days", type=float, default=1.0)
    cases.add_argument("--json", action="store_true", help="Emit JSON instead of table text.")

    subparsers.add_parser("summary", help="Print semantic layer summary.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_dir = args.input.expanduser().resolve()
    db_path = args.db.expanduser().resolve()
    if args.command == "build":
        build_database(input_dir, db_path, force=args.force)
        if args.report:
            args.report.expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
            args.report.expanduser().resolve().write_text(render_report(db_path), encoding="utf-8")
            print(f"Report: {args.report.expanduser().resolve()}")
        print(f"Database: {db_path}")
        return 0

    with connect(db_path) as conn:
        if args.command == "top-sellers":
            rows = fetch_top_sellers(conn, args.limit, args.min_orders)
            print_json_or_table(rows, args.json, top_seller_columns())
        elif args.command == "late-cases":
            rows = fetch_late_cases(conn, args.limit, args.seller_id, args.min_lateness_days)
            print_json_or_table(rows, args.json, late_case_columns())
        elif args.command == "summary":
            print(render_summary(conn))
    return 0


def build_database(input_dir: Path, db_path: Path, force: bool = False) -> None:
    if force and db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        for file_name, table_name in CSV_TABLES.items():
            load_csv(conn, input_dir / file_name, table_name)
        create_indexes(conn)
        create_delivery_cases(conn)
        create_seller_scorecard(conn)
        create_semantic_metadata(conn, input_dir)


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def load_csv(conn: sqlite3.Connection, path: Path, table_name: str) -> None:
    if not path.exists():
        raise FileNotFoundError(path)
    conn.execute(f"DROP TABLE IF EXISTS {table_name}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = list(reader.fieldnames or [])
        if not columns:
            raise ValueError(f"No columns found in {path}")
        quoted_columns = ", ".join(f'"{column}" TEXT' for column in columns)
        conn.execute(f"CREATE TABLE {table_name} ({quoted_columns})")
        placeholders = ", ".join("?" for _ in columns)
        quoted_names = ", ".join(f'"{column}"' for column in columns)
        rows = ([row.get(column, "") for column in columns] for row in reader)
        conn.executemany(f"INSERT INTO {table_name} ({quoted_names}) VALUES ({placeholders})", rows)
    conn.commit()


def create_indexes(conn: sqlite3.Connection) -> None:
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_customers_customer_id ON customers(customer_id)",
        "CREATE INDEX IF NOT EXISTS idx_orders_order_id ON orders(order_id)",
        "CREATE INDEX IF NOT EXISTS idx_orders_customer_id ON orders(customer_id)",
        "CREATE INDEX IF NOT EXISTS idx_items_order_id ON order_items(order_id)",
        "CREATE INDEX IF NOT EXISTS idx_items_seller_id ON order_items(seller_id)",
        "CREATE INDEX IF NOT EXISTS idx_items_product_id ON order_items(product_id)",
        "CREATE INDEX IF NOT EXISTS idx_reviews_order_id ON reviews(order_id)",
        "CREATE INDEX IF NOT EXISTS idx_products_product_id ON products(product_id)",
        "CREATE INDEX IF NOT EXISTS idx_sellers_seller_id ON sellers(seller_id)",
        "CREATE INDEX IF NOT EXISTS idx_translation_category ON product_category_translation(product_category_name)",
    ]
    for statement in indexes:
        conn.execute(statement)
    conn.commit()


def create_delivery_cases(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS delivery_cases")
    conn.execute(
        """
        CREATE TABLE delivery_cases AS
        WITH review_by_order AS (
            SELECT
                order_id,
                AVG(CAST(review_score AS REAL)) AS avg_review_score,
                MIN(CAST(review_score AS INTEGER)) AS min_review_score,
                COUNT(*) AS review_count,
                SUM(CASE WHEN CAST(review_score AS INTEGER) <= 2 THEN 1 ELSE 0 END) AS bad_review_count
            FROM reviews
            GROUP BY order_id
        )
        SELECT
            o.order_id,
            c.customer_id,
            c.customer_unique_id,
            c.customer_state,
            c.customer_city,
            c.customer_zip_code_prefix,
            i.order_item_id,
            i.seller_id,
            s.seller_state,
            s.seller_city,
            s.seller_zip_code_prefix,
            i.product_id,
            p.product_category_name,
            COALESCE(t.product_category_name_english, p.product_category_name) AS product_category_name_english,
            o.order_status,
            o.order_purchase_timestamp,
            o.order_approved_at,
            o.order_delivered_carrier_date,
            o.order_delivered_customer_date,
            o.order_estimated_delivery_date,
            i.shipping_limit_date,
            CAST(i.price AS REAL) AS price,
            CAST(i.freight_value AS REAL) AS freight_value,
            CASE WHEN CAST(i.price AS REAL) > 0 THEN CAST(i.freight_value AS REAL) / CAST(i.price AS REAL) END AS freight_ratio,
            CASE
                WHEN o.order_delivered_customer_date != '' AND o.order_estimated_delivery_date != ''
                THEN julianday(o.order_delivered_customer_date) - julianday(o.order_estimated_delivery_date)
            END AS lateness_days,
            CASE
                WHEN o.order_approved_at != '' AND o.order_purchase_timestamp != ''
                THEN (julianday(o.order_approved_at) - julianday(o.order_purchase_timestamp)) * 24.0
            END AS approval_latency_hours,
            CASE
                WHEN o.order_delivered_carrier_date != '' AND o.order_approved_at != ''
                THEN (julianday(o.order_delivered_carrier_date) - julianday(o.order_approved_at)) * 24.0
            END AS carrier_handoff_latency_hours,
            CASE
                WHEN o.order_delivered_customer_date != '' AND o.order_purchase_timestamp != ''
                THEN julianday(o.order_delivered_customer_date) - julianday(o.order_purchase_timestamp)
            END AS customer_delivery_latency_days,
            CASE
                WHEN o.order_delivered_customer_date != ''
                    AND o.order_estimated_delivery_date != ''
                    AND julianday(o.order_delivered_customer_date) > julianday(o.order_estimated_delivery_date)
                THEN 1 ELSE 0
            END AS is_late,
            CASE
                WHEN o.order_delivered_customer_date = '' OR o.order_estimated_delivery_date = '' THEN 'unknown'
                WHEN julianday(o.order_delivered_customer_date) <= julianday(o.order_estimated_delivery_date) THEN 'on_time'
                WHEN julianday(o.order_delivered_customer_date) - julianday(o.order_estimated_delivery_date) < 3 THEN 'minor_late'
                WHEN julianday(o.order_delivered_customer_date) - julianday(o.order_estimated_delivery_date) < 8 THEN 'late'
                WHEN julianday(o.order_delivered_customer_date) - julianday(o.order_estimated_delivery_date) < 15 THEN 'severe_late'
                ELSE 'critical_late'
            END AS delivery_severity,
            CASE WHEN c.customer_state = s.seller_state THEN 1 ELSE 0 END AS same_state_delivery,
            rb.avg_review_score,
            rb.min_review_score,
            COALESCE(rb.review_count, 0) AS review_count,
            COALESCE(rb.bad_review_count, 0) AS bad_review_count,
            CASE WHEN COALESCE(rb.bad_review_count, 0) > 0 THEN 1 ELSE 0 END AS has_bad_review
        FROM order_items i
        JOIN orders o ON o.order_id = i.order_id
        LEFT JOIN customers c ON c.customer_id = o.customer_id
        LEFT JOIN sellers s ON s.seller_id = i.seller_id
        LEFT JOIN products p ON p.product_id = i.product_id
        LEFT JOIN product_category_translation t ON t.product_category_name = p.product_category_name
        LEFT JOIN review_by_order rb ON rb.order_id = o.order_id
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_cases_seller ON delivery_cases(seller_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_cases_order ON delivery_cases(order_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_cases_late ON delivery_cases(is_late, lateness_days)")
    conn.commit()


def create_seller_scorecard(conn: sqlite3.Connection) -> None:
    seller_metrics = compute_seller_metrics(conn)
    rows = score_sellers(seller_metrics)
    conn.execute("DROP TABLE IF EXISTS seller_delivery_scorecard")
    conn.execute(
        """
        CREATE TABLE seller_delivery_scorecard (
            seller_id TEXT PRIMARY KEY,
            seller_state TEXT,
            order_count INTEGER,
            item_count INTEGER,
            late_order_count INTEGER,
            bad_review_count INTEGER,
            late_rate REAL,
            bad_review_rate REAL,
            avg_lateness_days REAL,
            p95_lateness_days REAL,
            avg_review_score REAL,
            avg_late_order_review_score REAL,
            total_price REAL,
            total_freight REAL,
            avg_freight_ratio REAL,
            late_rate_percentile REAL,
            avg_lateness_percentile REAL,
            bad_review_rate_percentile REAL,
            order_volume_percentile REAL,
            seller_delivery_pain_score REAL,
            recommended_action TEXT
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO seller_delivery_scorecard VALUES (
            :seller_id, :seller_state, :order_count, :item_count, :late_order_count, :bad_review_count,
            :late_rate, :bad_review_rate, :avg_lateness_days, :p95_lateness_days,
            :avg_review_score, :avg_late_order_review_score, :total_price, :total_freight, :avg_freight_ratio,
            :late_rate_percentile, :avg_lateness_percentile, :bad_review_rate_percentile,
            :order_volume_percentile, :seller_delivery_pain_score, :recommended_action
        )
        """,
        rows,
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_seller_scorecard_score ON seller_delivery_scorecard(seller_delivery_pain_score DESC)"
    )
    conn.commit()


def compute_seller_metrics(conn: sqlite3.Connection) -> list[SellerMetric]:
    rows = conn.execute(
        """
        SELECT
            seller_id,
            seller_state,
            COUNT(DISTINCT order_id) AS order_count,
            COUNT(*) AS item_count,
            COUNT(DISTINCT CASE WHEN is_late = 1 THEN order_id END) AS late_order_count,
            COUNT(DISTINCT CASE WHEN has_bad_review = 1 THEN order_id END) AS bad_review_count,
            AVG(CASE WHEN is_late = 1 THEN lateness_days END) AS avg_lateness_days,
            AVG(avg_review_score) AS avg_review_score,
            AVG(CASE WHEN is_late = 1 THEN avg_review_score END) AS avg_late_order_review_score,
            SUM(price) AS total_price,
            SUM(freight_value) AS total_freight,
            AVG(freight_ratio) AS avg_freight_ratio
        FROM delivery_cases
        WHERE seller_id IS NOT NULL AND seller_id != ''
        GROUP BY seller_id, seller_state
        """
    ).fetchall()
    metrics = []
    for row in rows:
        late_values = [
            float(value[0])
            for value in conn.execute(
                "SELECT lateness_days FROM delivery_cases WHERE seller_id = ? AND is_late = 1 AND lateness_days IS NOT NULL",
                (row["seller_id"],),
            ).fetchall()
        ]
        metrics.append(
            SellerMetric(
                seller_id=row["seller_id"],
                seller_state=row["seller_state"],
                order_count=int(row["order_count"] or 0),
                item_count=int(row["item_count"] or 0),
                late_order_count=int(row["late_order_count"] or 0),
                bad_review_count=int(row["bad_review_count"] or 0),
                avg_lateness_days=float(row["avg_lateness_days"] or 0.0),
                p95_lateness_days=percentile(late_values, 95),
                avg_review_score=float(row["avg_review_score"] or 0.0),
                avg_late_order_review_score=float(row["avg_late_order_review_score"] or 0.0),
                total_price=float(row["total_price"] or 0.0),
                total_freight=float(row["total_freight"] or 0.0),
                avg_freight_ratio=float(row["avg_freight_ratio"] or 0.0),
                late_rate=safe_div(row["late_order_count"], row["order_count"]),
                bad_review_rate=safe_div(row["bad_review_count"], row["order_count"]),
            )
        )
    return metrics


def score_sellers(metrics: list[SellerMetric]) -> list[dict[str, Any]]:
    late_rate_rank = percentile_rank({m.seller_id: m.late_rate for m in metrics})
    avg_lateness_rank = percentile_rank({m.seller_id: m.avg_lateness_days for m in metrics})
    bad_review_rank = percentile_rank({m.seller_id: m.bad_review_rate for m in metrics})
    volume_rank = percentile_rank({m.seller_id: m.order_count for m in metrics})

    rows = []
    for metric in metrics:
        score = (
            late_rate_rank[metric.seller_id] * SELLER_SCORE_WEIGHTS["late_rate_percentile"]
            + avg_lateness_rank[metric.seller_id] * SELLER_SCORE_WEIGHTS["avg_lateness_percentile"]
            + bad_review_rank[metric.seller_id] * SELLER_SCORE_WEIGHTS["bad_review_rate_percentile"]
            + volume_rank[metric.seller_id] * SELLER_SCORE_WEIGHTS["order_volume_percentile"]
        )
        rows.append(
            {
                "seller_id": metric.seller_id,
                "seller_state": metric.seller_state,
                "order_count": metric.order_count,
                "item_count": metric.item_count,
                "late_order_count": metric.late_order_count,
                "bad_review_count": metric.bad_review_count,
                "late_rate": round(metric.late_rate, 6),
                "bad_review_rate": round(metric.bad_review_rate, 6),
                "avg_lateness_days": round(metric.avg_lateness_days, 4),
                "p95_lateness_days": round(metric.p95_lateness_days, 4),
                "avg_review_score": round(metric.avg_review_score, 4),
                "avg_late_order_review_score": round(metric.avg_late_order_review_score, 4),
                "total_price": round(metric.total_price, 2),
                "total_freight": round(metric.total_freight, 2),
                "avg_freight_ratio": round(metric.avg_freight_ratio, 6),
                "late_rate_percentile": round(late_rate_rank[metric.seller_id], 4),
                "avg_lateness_percentile": round(avg_lateness_rank[metric.seller_id], 4),
                "bad_review_rate_percentile": round(bad_review_rank[metric.seller_id], 4),
                "order_volume_percentile": round(volume_rank[metric.seller_id], 4),
                "seller_delivery_pain_score": round(score, 4),
                "recommended_action": recommended_action(score, metric.order_count),
            }
        )
    return rows


def create_semantic_metadata(conn: sqlite3.Connection, input_dir: Path) -> None:
    conn.execute("DROP TABLE IF EXISTS semantic_metadata")
    conn.execute("CREATE TABLE semantic_metadata (key TEXT PRIMARY KEY, value TEXT)")
    values = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_dir": str(input_dir),
        "score_weights": json.dumps(SELLER_SCORE_WEIGHTS, sort_keys=True),
        "first_workflow": "delivery_reliability",
    }
    conn.executemany("INSERT INTO semantic_metadata VALUES (?, ?)", values.items())
    conn.commit()


def fetch_top_sellers(conn: sqlite3.Connection, limit: int, min_orders: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM seller_delivery_scorecard
        WHERE order_count >= ?
        ORDER BY seller_delivery_pain_score DESC, late_order_count DESC
        LIMIT ?
        """,
        (min_orders, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_late_cases(
    conn: sqlite3.Connection,
    limit: int,
    seller_id: str | None,
    min_lateness_days: float,
) -> list[dict[str, Any]]:
    params: list[Any] = [min_lateness_days]
    seller_filter = ""
    if seller_id:
        seller_filter = "AND seller_id = ?"
        params.append(seller_id)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            order_id,
            order_item_id,
            seller_id,
            product_id,
            seller_state,
            customer_state,
            product_category_name_english,
            delivery_severity,
            ROUND(lateness_days, 2) AS lateness_days,
            ROUND(avg_review_score, 2) AS avg_review_score,
            ROUND(price, 2) AS price,
            ROUND(freight_value, 2) AS freight_value,
            ROUND(freight_ratio, 2) AS freight_ratio
        FROM delivery_cases
        WHERE is_late = 1 AND lateness_days >= ? {seller_filter}
        ORDER BY lateness_days DESC, avg_review_score ASC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def render_summary(conn: sqlite3.Connection) -> str:
    summary = {}
    for table in ["customers", "orders", "order_items", "reviews", "products", "sellers", "delivery_cases"]:
        summary[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    delivery = conn.execute(
        """
        SELECT
            COUNT(DISTINCT order_id) AS orders,
            COUNT(DISTINCT CASE WHEN is_late = 1 THEN order_id END) AS late_orders,
            AVG(CASE WHEN is_late = 1 THEN lateness_days END) AS avg_lateness_days,
            AVG(CASE WHEN is_late = 1 THEN avg_review_score END) AS avg_late_review,
            AVG(CASE WHEN is_late = 0 THEN avg_review_score END) AS avg_on_time_review
        FROM delivery_cases
        """
    ).fetchone()
    return "\n".join(
        [
            "Olist Semantic Layer Summary",
            json.dumps(summary, indent=2, sort_keys=True),
            f"late_orders={delivery['late_orders']} of {delivery['orders']}",
            f"avg_lateness_days={round(delivery['avg_lateness_days'] or 0, 2)}",
            f"avg_late_review={round(delivery['avg_late_review'] or 0, 2)}",
            f"avg_on_time_review={round(delivery['avg_on_time_review'] or 0, 2)}",
        ]
    )


def render_report(db_path: Path) -> str:
    with connect(db_path) as conn:
        top = fetch_top_sellers(conn, limit=15, min_orders=50)
        summary = render_summary(conn)
    lines = ["# Olist Delivery Reliability Scorecard", "", "```", summary, "```", ""]
    lines.extend(
        [
            "## Seller Delivery Pain Score",
            "",
            "Formula:",
            "",
            "```text",
            "0.40 * late_rate_percentile",
            "+ 0.25 * avg_lateness_percentile",
            "+ 0.25 * bad_review_rate_percentile",
            "+ 0.10 * order_volume_percentile",
            "```",
            "",
            "| Rank | Seller | State | Orders | Late | Late Rate | Bad Review Rate | Avg Late Days | Score | Action |",
            "| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for index, row in enumerate(top, start=1):
        lines.append(
            f"| {index} | `{row['seller_id']}` | {row['seller_state'] or ''} | {row['order_count']} | "
            f"{row['late_order_count']} | {row['late_rate']:.2%} | {row['bad_review_rate']:.2%} | "
            f"{row['avg_lateness_days']:.2f} | {row['seller_delivery_pain_score']:.2f} | {row['recommended_action']} |"
        )
    return "\n".join(lines) + "\n"


def print_json_or_table(rows: list[dict[str, Any]], as_json: bool, columns: list[str]) -> None:
    if as_json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return
    if not rows:
        print("No rows.")
        return
    widths = {column: len(column) for column in columns}
    for row in rows:
        for column in columns:
            widths[column] = max(widths[column], len(format_value(row.get(column))))
    header = "  ".join(column.ljust(widths[column]) for column in columns)
    print(header)
    print("  ".join("-" * widths[column] for column in columns))
    for row in rows:
        print("  ".join(format_value(row.get(column)).ljust(widths[column]) for column in columns))


def top_seller_columns() -> list[str]:
    return [
        "seller_id",
        "seller_state",
        "order_count",
        "late_order_count",
        "late_rate",
        "bad_review_rate",
        "avg_lateness_days",
        "seller_delivery_pain_score",
        "recommended_action",
    ]


def late_case_columns() -> list[str]:
    return [
        "order_id",
        "order_item_id",
        "seller_id",
        "product_id",
        "customer_state",
        "seller_state",
        "product_category_name_english",
        "delivery_severity",
        "lateness_days",
        "avg_review_score",
        "freight_ratio",
    ]


def percentile_rank(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    sorted_items = sorted(values.items(), key=lambda item: (item[1], item[0]))
    denominator = max(len(sorted_items) - 1, 1)
    ranks = {}
    for index, (key, _value) in enumerate(sorted_items):
        ranks[key] = (index / denominator) * 100.0
    return ranks


def percentile(values: list[float], pct: int) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    index = round((len(sorted_values) - 1) * (pct / 100.0))
    return float(sorted_values[index])


def safe_div(numerator: Any, denominator: Any) -> float:
    numerator = float(numerator or 0.0)
    denominator = float(denominator or 0.0)
    if denominator == 0:
        return 0.0
    return numerator / denominator


def recommended_action(score: float, order_count: int) -> str:
    if order_count < 20:
        return "monitor_low_volume"
    if score >= 80:
        return "urgent_seller_sla_review"
    if score >= 65:
        return "seller_warning_and_delivery_estimate_review"
    if score >= 50:
        return "monitor_delivery_trend"
    return "no_action"


def format_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4f}"
    return "" if value is None else str(value)


if __name__ == "__main__":
    raise SystemExit(main())
