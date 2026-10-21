"""Record each Full Shred run's timings, token usage, cost and citation rate in a CSV, so runs can be compared
across models and files."""

from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path

from .shred import Shred
from .shred_schema import TABS

LOG_PATH = Path(__file__).resolve().parent.parent / "runs" / "extraction_times.csv"
FIELDS = (
    "timestamp",
    "file",
    "pages",
    "mode",
    "model",
    "reasoning_effort",
    "requirements",
    "read_extract_s",
    "rule_shred_s",
    "ai_stage1_s",
    "ai_stage2_s",
    "ai_stage3_s",
    "build_excel_s",
    "total_s",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cost_usd",
    "cited_rows",
    "cited_pct",
    "page_cited_pct",
    "warnings",
)
TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens")
PAGE_REF = re.compile(r"\bpp?\.\s*(\d+)")


def usage_totals(shred: Shred) -> dict[str, float | None]:
    """Token counts summed over every model call; cost is None if any call used a model without a known price."""
    totals: dict[str, float | None] = {k: sum(u[k] for u in shred.usage) for k in TOKEN_FIELDS}
    costs = [u["cost_usd"] for u in shred.usage]
    totals["cost_usd"] = None if None in costs else sum(costs)
    return totals


def citation_stats(shred: Shred, page_count: int | None) -> dict[str, float | int | None]:
    """How many rows in the tabs with a Source Location column cite where they came from.

    cited_pct: rows with a non-empty Source Location. page_cited_pct: rows citing a page ("p. N") that exists in
    the document; None when the document has no pages (not a PDF).
    """
    rows = [r for spec in TABS if "source_location" in spec.keys for r in shred.tabs.get(spec.name, [])]
    locations = [str(r.get("source_location", "")).strip() for r in rows]
    cited = [loc for loc in locations if loc]
    page_cited = [loc for loc in cited if page_count and any(1 <= int(n) <= page_count for n in PAGE_REF.findall(loc))]

    def pct(part: list) -> float | None:
        return round(100 * len(part) / len(rows), 1) if rows else None

    return {
        "rows": len(rows),
        "cited_rows": len(cited),
        "cited_pct": pct(cited),
        "page_cited_pct": pct(page_cited) if page_count else None,
    }


def _seconds(shred: Shred, *prefixes: str) -> str:
    """Seconds of the first timing step starting with any prefix, in prefix order; "" if none ran."""
    for prefix in prefixes:
        for step, seconds in shred.timings:
            if step.startswith(prefix):
                return f"{seconds:.2f}"
    return ""


def record_run(
    shred: Shred,
    total: float,
    pages: int | str,  # page count for a PDF, else the file type
    model: str = "",
    reasoning_effort: str = "",
    path: Path = LOG_PATH,
) -> dict[str, str]:
    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "file": shred.source_file,
        "pages": str(pages),
        "mode": "AI" if model else "Rule-based",
        "model": model,
        "reasoning_effort": reasoning_effort if model else "",
        "requirements": str(len(shred.tabs.get("Compliance Matrix", []))),
        "read_extract_s": _seconds(shred, "Read & extract"),
        "rule_shred_s": _seconds(shred, "Rule-based shred"),
        "ai_stage1_s": _seconds(shred, "AI stage 1"),
        # Stage 2 runs its groups in parallel: its wall-clock total is the stage time.
        "ai_stage2_s": _seconds(shred, "AI stage 2 total", "AI stage 2"),
        "ai_stage3_s": _seconds(shred, "AI stage 3"),
        "build_excel_s": _seconds(shred, "Build Excel workbook"),
        "total_s": f"{total:.2f}",
        "warnings": str(len(shred.warnings)),
    }
    if shred.usage:
        totals = usage_totals(shred)
        row.update({k: str(totals[k]) for k in TOKEN_FIELDS})
        row["cost_usd"] = "" if totals["cost_usd"] is None else f"{totals['cost_usd']:.4f}"
    citations = citation_stats(shred, pages if isinstance(pages, int) else None)
    row["cited_rows"] = f"{citations['cited_rows']}/{citations['rows']}"
    row.update({k: "" if citations[k] is None else f"{citations[k]:.1f}" for k in ("cited_pct", "page_cited_pct")})
    path.parent.mkdir(parents=True, exist_ok=True)
    _upgrade_header(path)
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)
    return row


def _upgrade_header(path: Path) -> None:
    """Rewrite a log written with an older column set so new rows line up; old rows get blank new columns."""
    if not path.exists():
        return
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if tuple(reader.fieldnames or ()) == FIELDS:
            return
        rows = list(reader)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_runs(path: Path = LOG_PATH) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))
