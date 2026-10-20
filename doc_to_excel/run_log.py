"""Record each Full Shred run's timings in a CSV so extraction times can be compared across models and files."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from .shred import Shred

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
    "warnings",
)


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
    pages: int | str,
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
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)
    return row


def load_runs(path: Path = LOG_PATH) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))
