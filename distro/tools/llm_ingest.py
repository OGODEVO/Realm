#!/usr/bin/env python3
"""Create LLM-ready profiles and markdown summaries for common data files.

Usage:
    python tools/llm_ingest.py --input ./data --output ./artifacts/llm_ready/my_dataset

Supported inputs: CSV, JSON, JSONL, TXT, MD, and PDF when PyMuPDF or
pdfplumber is already installed. The tool intentionally uses the Python
standard library for core profiling so it can run in lightweight agent
environments without a dependency install step.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SUPPORTED_EXTENSIONS = {".csv", ".json", ".jsonl", ".txt", ".md", ".pdf"}
ID_NAME_RE = re.compile(r"(^id$|_id$|id_|_uuid$|uuid$|key$|code$|zip_code|prefix$)", re.I)
FK_NAME_RE = re.compile(r"(_id$|uuid$|customer_id|order_id|product_id|seller_id|zip_code_prefix)", re.I)
BOOL_VALUES = {"true", "false", "yes", "no", "y", "n", "0", "1"}
MAX_SAMPLE_VALUES = 8
MAX_TEXT_PREVIEW = 600
MAX_FIELD_EXAMPLES = 5
MAX_JSON_RECORDS = 10000
MAX_FILE_SIZE_BYTES = 500 * 1024 * 1024


@dataclass
class ColumnStats:
    name: str
    row_count: int = 0
    missing_count: int = 0
    unique_values: set[str] = field(default_factory=set)
    samples: list[str] = field(default_factory=list)
    type_counts: Counter[str] = field(default_factory=Counter)
    numeric_min: float | None = None
    numeric_max: float | None = None
    datetime_min: str | None = None
    datetime_max: str | None = None
    total_length: int = 0

    def add(self, raw_value: Any) -> None:
        self.row_count += 1
        value = "" if raw_value is None else str(raw_value).strip()
        if value == "":
            self.missing_count += 1
            return

        self.unique_values.add(value)
        self.total_length += len(value)
        if len(self.samples) < MAX_SAMPLE_VALUES and value not in self.samples:
            self.samples.append(value)

        primitive, parsed = infer_cell_type(value)
        self.type_counts[primitive] += 1
        if primitive in {"integer", "float"}:
            number = float(parsed)
            self.numeric_min = number if self.numeric_min is None else min(self.numeric_min, number)
            self.numeric_max = number if self.numeric_max is None else max(self.numeric_max, number)
        elif primitive == "datetime":
            iso_value = parsed.isoformat()
            self.datetime_min = iso_value if self.datetime_min is None else min(self.datetime_min, iso_value)
            self.datetime_max = iso_value if self.datetime_max is None else max(self.datetime_max, iso_value)

    def profile(self) -> dict[str, Any]:
        observed_count = self.row_count - self.missing_count
        unique_count = len(self.unique_values)
        unique_pct = pct(unique_count, observed_count)
        missing_pct = pct(self.missing_count, self.row_count)
        inferred_type = infer_column_type(self, observed_count, unique_count)

        result: dict[str, Any] = {
            "name": self.name,
            "inferred_type": inferred_type,
            "missing_count": self.missing_count,
            "missing_pct": missing_pct,
            "unique_count": unique_count,
            "unique_pct": unique_pct,
            "sample_values": self.samples,
        }
        if self.numeric_min is not None and self.numeric_max is not None and inferred_type in {"integer", "float"}:
            result["min"] = normalize_number(self.numeric_min, inferred_type)
            result["max"] = normalize_number(self.numeric_max, inferred_type)
        if self.datetime_min is not None and self.datetime_max is not None and inferred_type == "datetime":
            result["min"] = self.datetime_min
            result["max"] = self.datetime_max
        return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Profile CSV/JSON/TXT/MD/PDF files into LLM-ready markdown and JSON bundles."
    )
    parser.add_argument("--input", required=True, type=Path, help="Input file or directory to process.")
    parser.add_argument("--output", required=True, type=Path, help="Output directory for manifest and profiles.")
    parser.add_argument(
        "--max-file-size-mb",
        type=int,
        default=MAX_FILE_SIZE_BYTES // (1024 * 1024),
        help="Skip supported files larger than this size. Default: 500.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = args.input.expanduser().resolve()
    output_dir = args.output.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    max_file_size = args.max_file_size_mb * 1024 * 1024
    manifest: dict[str, Any] = {
        "generated_at": now_iso(),
        "input": str(input_path),
        "output": str(output_dir),
        "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
        "pdf_support": detect_pdf_support()[0],
        "files": [],
        "errors": [],
    }

    for source_path in discover_files(input_path, max_file_size):
        try:
            profile = profile_file(source_path, input_path)
            stem = safe_output_stem(source_path, input_path)
            json_path = output_dir / f"{stem}.profile.json"
            md_path = output_dir / f"{stem}.summary.md"
            write_json(json_path, profile)
            md_path.write_text(render_markdown(profile), encoding="utf-8")
            manifest["files"].append(
                {
                    "source_path": str(source_path),
                    "relative_path": profile["relative_path"],
                    "file_type": profile["file_type"],
                    "profile_path": str(json_path),
                    "markdown_path": str(md_path),
                    "status": profile.get("status", "ok"),
                    "error": profile.get("error"),
                }
            )
        except Exception as exc:  # Keep folder runs useful even when one file is malformed.
            error = {"source_path": str(source_path), "error": f"{type(exc).__name__}: {exc}"}
            manifest["errors"].append(error)

    manifest_path = output_dir / "manifest.json"
    write_json(manifest_path, manifest)
    print(f"Manifest: {manifest_path}")
    print(f"Processed files: {len(manifest['files'])}")
    print(f"Errors: {len(manifest['errors'])}")
    return 1 if manifest["errors"] else 0


def discover_files(input_path: Path, max_file_size: int) -> list[Path]:
    if input_path.is_file():
        candidates = [input_path]
    else:
        candidates = []
        for path in sorted(input_path.rglob("*")):
            if not path.is_file() or any(part.startswith(".") for part in path.relative_to(input_path).parts):
                continue
            candidates.append(path)

    files: list[Path] = []
    for path in candidates:
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        try:
            if path.stat().st_size > max_file_size:
                continue
        except OSError:
            continue
        files.append(path)
    return files


def profile_file(path: Path, input_root: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        profile = profile_csv(path)
    elif suffix == ".json":
        profile = profile_json(path)
    elif suffix == ".jsonl":
        profile = profile_jsonl(path)
    elif suffix in {".txt", ".md"}:
        profile = profile_text(path)
    elif suffix == ".pdf":
        profile = profile_pdf(path)
    else:
        raise ValueError(f"Unsupported extension: {suffix}")

    profile.update(
        {
            "source_path": str(path),
            "relative_path": relative_path(path, input_root),
            "file_name": path.name,
            "file_size_bytes": path.stat().st_size,
            "generated_at": now_iso(),
        }
    )
    return profile


def profile_csv(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(8192)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample)
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(handle, dialect=dialect)
        columns = list(reader.fieldnames or [])
        stats = {column: ColumnStats(column) for column in columns}
        row_count = 0
        for row in reader:
            row_count += 1
            for column in columns:
                stats[column].add(row.get(column))

    column_profiles = [stats[column].profile() for column in columns]
    primary_keys = candidate_primary_keys(column_profiles, row_count)
    foreign_keys = candidate_foreign_keys(column_profiles, primary_keys)
    return {
        "file_type": "csv",
        "status": "ok",
        "row_count": row_count,
        "column_count": len(columns),
        "columns": columns,
        "column_profiles": column_profiles,
        "candidate_primary_keys": primary_keys,
        "candidate_foreign_keys_by_name": foreign_keys,
        "purpose_guess": guess_purpose(path),
    }


def profile_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data if isinstance(data, list) else [data]
    return profile_json_records(records, path, root_kind=type(data).__name__)


def profile_jsonl(path: Path) -> dict[str, Any]:
    records = []
    with path.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if index >= MAX_JSON_RECORDS:
                break
            line = line.strip()
            if line:
                records.append(json.loads(line))
    profile = profile_json_records(records, path, root_kind="jsonl")
    profile["sampled_record_limit"] = MAX_JSON_RECORDS
    return profile


def profile_json_records(records: list[Any], path: Path, root_kind: str) -> dict[str, Any]:
    top_level_keys: Counter[str] = Counter()
    field_types: dict[str, Counter[str]] = defaultdict(Counter)
    examples: dict[str, list[Any]] = defaultdict(list)
    object_records = [record for record in records if isinstance(record, dict)]
    for record in object_records[:MAX_JSON_RECORDS]:
        for key, value in record.items():
            top_level_keys[key] += 1
            field_types[key][json_type_name(value)] += 1
            if len(examples[key]) < MAX_FIELD_EXAMPLES and value not in examples[key]:
                examples[key].append(value)

    return {
        "file_type": "json" if path.suffix.lower() == ".json" else "jsonl",
        "status": "ok",
        "root_kind": root_kind,
        "record_count": len(records),
        "object_record_count": len(object_records),
        "top_level_keys": sorted(top_level_keys.keys()),
        "field_type_summary": {
            key: {"types": dict(counter), "examples": examples[key]}
            for key, counter in sorted(field_types.items())
        },
        "purpose_guess": guess_purpose(path),
    }


def profile_text(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    headings = extract_headings(lines)
    chunks = summarize_text_chunks(text)
    return {
        "file_type": "markdown" if path.suffix.lower() == ".md" else "text",
        "status": "ok",
        "char_count": len(text),
        "line_count": len(lines),
        "headings": headings,
        "chunks": chunks,
        "purpose_guess": guess_purpose(path),
    }


def profile_pdf(path: Path) -> dict[str, Any]:
    support, backend = detect_pdf_support()
    if not support:
        return {
            "file_type": "pdf",
            "status": "unsupported_pdf_without_dependency",
            "error": "Install PyMuPDF (`fitz`) or pdfplumber to enable PDF text extraction.",
            "purpose_guess": guess_purpose(path),
        }
    if backend == "fitz":
        import fitz  # type: ignore

        doc = fitz.open(path)
        pages = [page.get_text("text") for page in doc]
    else:
        import pdfplumber  # type: ignore

        with pdfplumber.open(path) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
    text = "\n\n".join(pages)
    return {
        "file_type": "pdf",
        "status": "ok",
        "pdf_backend": backend,
        "page_count": len(pages),
        "char_count": len(text),
        "pages": [shorten(page, MAX_TEXT_PREVIEW) for page in pages[:20]],
        "purpose_guess": guess_purpose(path),
    }


def detect_pdf_support() -> tuple[bool, str | None]:
    try:
        import fitz  # noqa: F401

        return True, "fitz"
    except Exception:
        pass
    try:
        import pdfplumber  # noqa: F401

        return True, "pdfplumber"
    except Exception:
        return False, None


def infer_cell_type(value: str) -> tuple[str, Any]:
    lower = value.lower()
    if lower in BOOL_VALUES:
        return "boolean", lower in {"true", "yes", "y", "1"}
    try:
        if re.fullmatch(r"[-+]?\d+", value):
            return "integer", int(value)
    except ValueError:
        pass
    try:
        if re.fullmatch(r"[-+]?(\d+\.\d*|\d*\.\d+)([eE][-+]?\d+)?", value) or re.fullmatch(
            r"[-+]?\d+[eE][-+]?\d+", value
        ):
            number = float(value)
            if math.isfinite(number):
                return "float", number
    except ValueError:
        pass
    parsed_dt = parse_datetime(value)
    if parsed_dt is not None:
        return "datetime", parsed_dt
    return "string", value


def infer_column_type(stats: ColumnStats, observed_count: int, unique_count: int) -> str:
    if observed_count == 0:
        return "empty"
    counts = stats.type_counts
    if counts["string"] == 0:
        if counts["datetime"] == observed_count:
            return "datetime"
        if counts["boolean"] == observed_count:
            return "boolean"
        if counts["float"] > 0 and counts["integer"] + counts["float"] == observed_count:
            return "float"
        if counts["integer"] == observed_count:
            if ID_NAME_RE.search(stats.name):
                return "string_id"
            return "integer"
    if counts["string"] == observed_count:
        unique_ratio = unique_count / observed_count if observed_count else 0
        avg_length = stats.total_length / observed_count if observed_count else 0
        if ID_NAME_RE.search(stats.name) or (unique_ratio > 0.9 and avg_length >= 8):
            return "string_id"
        if unique_count <= 100 or unique_ratio <= 0.05:
            return "category"
        return "string"
    dominant_type, dominant_count = counts.most_common(1)[0]
    if dominant_count / observed_count >= 0.95:
        return dominant_type
    return "mixed"


def parse_datetime(value: str) -> datetime | None:
    if not re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", value):
        return None
    candidates = [value, value.replace("Z", "+00:00")]
    formats = [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
    ]
    for candidate in candidates:
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            pass
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return None


def candidate_primary_keys(column_profiles: list[dict[str, Any]], row_count: int) -> list[str]:
    keys = []
    for column in column_profiles:
        if row_count == 0:
            continue
        if column["missing_count"] == 0 and column["unique_count"] == row_count and ID_NAME_RE.search(column["name"]):
            keys.append(column["name"])
    return keys


def candidate_foreign_keys(column_profiles: list[dict[str, Any]], primary_keys: list[str]) -> list[str]:
    return [
        column["name"]
        for column in column_profiles
        if column["name"] not in primary_keys and FK_NAME_RE.search(column["name"])
    ]


def render_markdown(profile: dict[str, Any]) -> str:
    lines = [f"# {profile['file_name']}", ""]
    lines.append(f"- Source: `{profile['relative_path']}`")
    lines.append(f"- Type: `{profile['file_type']}`")
    lines.append(f"- Status: `{profile.get('status', 'ok')}`")
    lines.append(f"- Purpose guess: {profile.get('purpose_guess', 'Unknown')}")
    if profile.get("error"):
        lines.append(f"- Error: {profile['error']}")
    lines.append("")

    file_type = profile["file_type"]
    if file_type == "csv":
        render_csv_markdown(profile, lines)
    elif file_type in {"json", "jsonl"}:
        render_json_markdown(profile, lines)
    elif file_type in {"text", "markdown"}:
        render_text_markdown(profile, lines)
    elif file_type == "pdf":
        render_pdf_markdown(profile, lines)
    return "\n".join(lines).rstrip() + "\n"


def render_csv_markdown(profile: dict[str, Any], lines: list[str]) -> None:
    lines.extend(
        [
            "## Shape",
            f"- Rows: {profile['row_count']}",
            f"- Columns: {profile['column_count']}",
            f"- Candidate primary keys: {', '.join(profile['candidate_primary_keys']) or 'None detected'}",
            f"- Candidate relationships by name: {', '.join(profile['candidate_foreign_keys_by_name']) or 'None detected'}",
            "",
            "## Fields",
            "| Field | Type | Missing | Unique | Samples |",
            "| --- | --- | ---: | ---: | --- |",
        ]
    )
    for column in profile["column_profiles"]:
        samples = ", ".join(f"`{escape_md(str(value))}`" for value in column["sample_values"][:3])
        lines.append(
            f"| `{column['name']}` | {column['inferred_type']} | "
            f"{column['missing_count']} ({column['missing_pct']}%) | "
            f"{column['unique_count']} ({column['unique_pct']}%) | {samples} |"
        )
    lines.extend(["", "## Caveats", "- Relationship candidates are name-based only; validate values across files before ontology extraction."])


def render_json_markdown(profile: dict[str, Any], lines: list[str]) -> None:
    lines.extend(
        [
            "## Shape",
            f"- Root kind: `{profile['root_kind']}`",
            f"- Records: {profile['record_count']}",
            f"- Object records: {profile['object_record_count']}",
            f"- Top-level keys: {', '.join(f'`{key}`' for key in profile['top_level_keys']) or 'None'}",
            "",
            "## Field Types",
        ]
    )
    for key, summary in profile["field_type_summary"].items():
        lines.append(f"- `{key}`: {summary['types']} examples={shorten(json.dumps(summary['examples'], ensure_ascii=False), 200)}")


def render_text_markdown(profile: dict[str, Any], lines: list[str]) -> None:
    lines.extend(
        [
            "## Shape",
            f"- Characters: {profile['char_count']}",
            f"- Lines: {profile['line_count']}",
            "",
            "## Headings",
        ]
    )
    lines.extend(f"- {heading}" for heading in profile["headings"][:20])
    if not profile["headings"]:
        lines.append("- None detected")
    lines.extend(["", "## Chunks"])
    for chunk in profile["chunks"]:
        lines.append(f"- Lines {chunk['start_line']}-{chunk['end_line']}: {chunk['preview']}")


def render_pdf_markdown(profile: dict[str, Any], lines: list[str]) -> None:
    if profile.get("status") != "ok":
        lines.append("## PDF Text")
        lines.append("- Text extraction unavailable without PyMuPDF (`fitz`) or pdfplumber.")
        return
    lines.extend(
        [
            "## PDF Text",
            f"- Backend: `{profile['pdf_backend']}`",
            f"- Pages: {profile['page_count']}",
            f"- Characters: {profile['char_count']}",
        ]
    )
    for index, page in enumerate(profile["pages"], start=1):
        lines.append(f"- Page {index}: {page}")


def extract_headings(lines: list[str]) -> list[str]:
    headings = []
    for line in lines:
        stripped = line.strip()
        if re.match(r"^#{1,6}\s+", stripped):
            headings.append(stripped)
        elif stripped and len(stripped) <= 100 and stripped.isupper() and any(char.isalpha() for char in stripped):
            headings.append(stripped)
    return headings


def summarize_text_chunks(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    chunks = []
    start = 0
    while start < len(lines) and len(chunks) < 20:
        end = min(start + 40, len(lines))
        block = "\n".join(lines[start:end]).strip()
        if block:
            chunks.append({"start_line": start + 1, "end_line": end, "preview": shorten(" ".join(block.split()), MAX_TEXT_PREVIEW)})
        start = end
    return chunks


def guess_purpose(path: Path) -> str:
    name = path.stem.lower()
    tokens = [token for token in re.split(r"[^a-z0-9]+", name) if token and token not in {"dataset", "data", "olist"}]
    if not tokens:
        return "General source file."
    return "Likely describes " + ", ".join(tokens[:8]).replace("_", " ") + "."


def json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def normalize_number(value: float, inferred_type: str) -> int | float:
    if inferred_type == "integer":
        return int(value)
    return value


def pct(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round((numerator / denominator) * 100, 4)


def relative_path(path: Path, input_root: Path) -> str:
    try:
        root = input_root if input_root.is_dir() else input_root.parent
        return str(path.relative_to(root))
    except ValueError:
        return path.name


def safe_output_stem(path: Path, input_root: Path) -> str:
    rel = relative_path(path, input_root)
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", rel).strip("_")


def shorten(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def escape_md(text: str) -> str:
    return text.replace("|", "\\|").replace("`", "'")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
