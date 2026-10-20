"""The Full Shred pipeline shared by the Streamlit app and benchmark.py, so both time the same steps."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date

from .extract import ExtractionResult
from .shred import Shred, build_rule_shred
from .shred_ai import AIConfig, build_ai_shred
from .shred_excel import build_shred_workbook


def generate_full_shred(
    result: ExtractionResult,
    extract_step: tuple[str, float],
    source_file: str,
    run_date: date,
    client: str = "",
    rfp_number: str = "",
    title: str = "",
    matrix_text: str | None = None,
    ai: AIConfig | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> tuple[Shred, bytes, float]:
    """Build the shred (rule-based, then AI if configured) and the workbook.

    Returns the shred, the .xlsx bytes, and the total seconds including the already-timed extraction step.
    """
    started = time.perf_counter()
    shred = build_rule_shred(result, source_file, run_date, client, rfp_number, title, matrix_text)
    if ai is not None:
        shred = build_ai_shred(shred, result, ai, matrix_text, progress)
    with shred.timed("Build Excel workbook"):
        workbook = build_shred_workbook(shred)
    shred.timings.insert(0, extract_step)
    return shred, workbook, extract_step[1] + time.perf_counter() - started
