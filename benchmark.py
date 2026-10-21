"""Time and cost Full Shred generation for one document across models; record it in runs/extraction_times.csv.

Usage:
    python benchmark.py RFP.pdf --models gpt-5.6-sol gpt-6.1-sol [--effort medium] [--out-dir runs]

Runs the same pipeline as the Streamlit app (read & extract -> rule-based shred -> AI stages -> Excel), one model at a
time so the timings don't compete, and saves each workbook next to the log. Credentials come from
.streamlit/secrets.toml or the OPENAI_* environment variables.
"""

from __future__ import annotations

import argparse
import os
import time
import tomllib
from datetime import date
from pathlib import Path

from doc_to_excel import extract_document
from doc_to_excel.pipeline import generate_full_shred
from doc_to_excel.run_log import LOG_PATH, record_run
from doc_to_excel.shred_ai import DEFAULT_MODEL, AIConfig

ROOT = Path(__file__).resolve().parent


def credentials() -> dict[str, str]:
    secrets_file = ROOT / ".streamlit" / "secrets.toml"
    secrets = tomllib.loads(secrets_file.read_text(encoding="utf-8")) if secrets_file.exists() else {}
    names = ("OPENAI_API_KEY", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID")
    return {name: secrets.get(name) or os.environ.get(name, "") for name in names}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("document", type=Path)
    parser.add_argument("--models", nargs="+", default=[DEFAULT_MODEL], help="'rule' runs without AI")
    parser.add_argument("--effort", default="medium", choices=("low", "medium", "high"))
    parser.add_argument("--out-dir", type=Path, default=ROOT / "runs")
    args = parser.parse_args()

    creds = credentials()
    data = args.document.read_bytes()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    for model in args.models:
        start = time.perf_counter()
        result = extract_document(data, args.document.name)
        pages = len(result.page_texts) if result.file_type == "pdf" else result.file_type.upper()
        extract_step = (f"Read & extract document ({pages} pages)", time.perf_counter() - start)

        ai = None
        if model != "rule":
            if not creds["OPENAI_API_KEY"]:
                raise SystemExit("No OPENAI_API_KEY in .streamlit/secrets.toml or the environment.")
            ai = AIConfig(
                creds["OPENAI_API_KEY"], model, creds["OPENAI_ORG_ID"], creds["OPENAI_PROJECT_ID"], args.effort
            )

        print(f"\n=== {model} ===", flush=True)
        shred, workbook, total = generate_full_shred(
            result,
            extract_step,
            args.document.name,
            date.today(),
            ai=ai,
            progress=lambda _f, message: print(f"  [{time.perf_counter() - start:6.0f}s] {message}", flush=True),
        )
        row = record_run(shred, total, pages, "" if ai is None else model, args.effort)
        out = args.out_dir / f"{args.document.stem} - {model}.xlsx"
        out.write_bytes(workbook)

        for step, seconds in shred.timings:
            print(f"  {seconds:8.1f}s  {step}")
        print(f"  {total:8.1f}s  TOTAL ({len(shred.tabs['Compliance Matrix'])} requirements) -> {out.name}")
        if shred.usage:
            print(
                f"  tokens: {row['input_tokens']} in ({row['cached_input_tokens']} cached), "
                f"{row['output_tokens']} out ({row['reasoning_tokens']} reasoning); cost ${row['cost_usd'] or '?'}"
            )
        print(
            f"  citations: {row['cited_rows']} rows ({row['cited_pct']}%), "
            f"valid page cited: {row['page_cited_pct'] + '%' if row['page_cited_pct'] else 'n/a (not a PDF)'}"
        )
        for warning in shred.warnings:
            print(f"  WARNING: {warning}")

    print(f"\nRecorded in {LOG_PATH}")


if __name__ == "__main__":
    main()
