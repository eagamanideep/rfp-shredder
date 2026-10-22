"""Streamlit app: upload an RFP / requirements document and download it as a Full Shred Excel workbook.

Run with:  python -m streamlit run app.py
"""

from __future__ import annotations

import hashlib
import os
import time
from datetime import date
from pathlib import PurePath

import pandas as pd
import streamlit as st

from doc_to_excel import (
    DEFAULT_KEYWORDS,
    KEYWORD_TYPES,
    SHEETS,
    SUPPORTED_TYPES,
    ExtractionResult,
    build_frames,
    build_workbook,
    extract_document,
)
from doc_to_excel.pipeline import generate_full_shred
from doc_to_excel.run_log import LOG_PATH, citation_stats, load_runs, record_run, usage_totals
from doc_to_excel.shred import read_objection_matrix
from doc_to_excel.shred_ai import DEFAULT_MODEL, MODELS, AIConfig, default_effort
from doc_to_excel.shred_schema import TABS

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FULL_SHRED = "Full Shred (21 tabs)"
SIMPLE = "Simple extract (5 sheets)"

st.set_page_config(page_title="RFP Shred", page_icon="📊", layout="wide")


@st.cache_data(show_spinner=False, max_entries=8)
def convert(
    data: bytes,
    filename: str,
    keywords: tuple[str, ...],
    inherit_list_items: bool,
    strip_headers: bool,
    exclude_table_text: bool,
) -> tuple[ExtractionResult, float]:
    """Extract the document; also return how long it took, so cached reruns still report the real time."""
    start = time.perf_counter()
    result = extract_document(data, filename, keywords, inherit_list_items, strip_headers, exclude_table_text)
    return result, time.perf_counter() - start


def show_timings(timings: list[tuple[str, float]], total: float) -> None:
    with st.expander(f"⏱ Excel file generated in {format_seconds(total)} — step breakdown", expanded=False):
        frame = pd.DataFrame(timings, columns=["Step", "Seconds"])
        frame["Time"] = frame["Seconds"].map(format_seconds)
        frame["Seconds"] = frame["Seconds"].round(2)
        st.dataframe(frame, hide_index=True, width="stretch")
        st.caption("Steps marked · are tab groups that run in parallel inside their stage, so they overlap.")


def show_usage(shred, page_count: int | None) -> None:
    """Cost, token counts and citation rate of the run, with a per-call breakdown."""
    citations = citation_stats(shred, page_count)
    cols = st.columns(4)
    if shred.usage:
        totals = usage_totals(shred)
        cost = totals["cost_usd"]
        cols[0].metric("Cost", "unknown" if cost is None else f"${cost:.4f}", help="Standard-tier OpenAI prices.")
        cols[1].metric(
            "Input tokens", f"{totals['input_tokens']:,}", help=f"{totals['cached_input_tokens']:,} served from cache"
        )
        cols[2].metric(
            "Output tokens", f"{totals['output_tokens']:,}", help=f"{totals['reasoning_tokens']:,} of them reasoning"
        )
    else:
        cols[0].metric("Cost", "$0 (rule-based)")
    if citations["cited_pct"] is not None:
        page_note = (
            f"{citations['page_cited_pct']:.1f}% cite a page that exists in the document."
            if citations["page_cited_pct"] is not None
            else "Not a PDF, so page numbers are not checked."
        )
        cols[3].metric(
            "Rows with a citation",
            f"{citations['cited_pct']:.1f}%",
            help=f"{citations['cited_rows']} of {citations['rows']} rows have a Source Location. {page_note}",
        )
    if shred.usage:
        with st.expander(f"🪙 Token usage by model call ({len(shred.usage)} calls)"):
            frame = pd.DataFrame(shred.usage)
            frame["cost_usd"] = frame["cost_usd"].round(4)
            st.dataframe(frame, hide_index=True, width="stretch")


def format_seconds(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)} min {rest:.0f} s"


def secret(name: str) -> str:
    try:
        value = st.secrets.get(name, "")
    except Exception:  # noqa: BLE001 - no secrets.toml is a normal setup
        value = ""
    return value or os.environ.get(name, "")


with st.sidebar:
    output = st.radio("Output format", (FULL_SHRED, SIMPLE))

    if output == FULL_SHRED:
        st.header("Analysis")
        stored_key = secret("OPENAI_API_KEY")
        typed_key = st.text_input(
            "OpenAI API key",
            type="password",
            placeholder="Using key from secrets.toml" if stored_key else "sk-…",
            help="Leave blank to use OPENAI_API_KEY from .streamlit/secrets.toml or the environment.",
        )
        api_key = typed_key or stored_key
        use_ai = st.toggle("AI analysis", value=bool(api_key), disabled=not api_key)
        if not api_key:
            st.caption("No API key: the shred is rule-based (risk and contradictions left for review).")
        model = st.selectbox("Model", MODELS, index=MODELS.index(DEFAULT_MODEL), disabled=not use_ai)
        effort = st.select_slider(
            "Reasoning effort",
            ("low", "medium", "high"),
            value=default_effort(model),
            disabled=not use_ai or model.startswith("gpt-4"),
            help="Starts at the recommended setting for the model.",
        )

        st.header("Opportunity")
        st.caption("Leave blank to detect from the document.")
        client = st.text_input("Client / issuing organisation")
        rfp_number = st.text_input("RFP number")
        title = st.text_input("Title")
        run_date = st.date_input("Shred run date", value=date.today())
        matrix_file = st.file_uploader(
            "Contracts Proposal Objection Matrix (optional)",
            type=["xlsx"],
            help="Contract Objections are screened only against this internal matrix.",
        )
    else:
        include = st.multiselect("Sheets to include", SHEETS, default=list(SHEETS))

    with st.expander("Extraction options", expanded=output == SIMPLE):
        keywords = st.multiselect(
            "Requirement keywords",
            options=list(KEYWORD_TYPES),
            default=list(DEFAULT_KEYWORDS),
            help="Sentences containing any of these words are listed as requirements.",
        )
        inherit_list_items = st.checkbox(
            "Count list items under a requirement",
            value=True,
            help='Bullets that follow a sentence like "The vendor must:" are listed as requirements too.',
        )
        strip_headers = st.checkbox("Remove page headers, footers and page numbers", value=True)
        exclude_table_text = st.checkbox("Keep table text out of Sections", value=True)

st.title("📊 RFP Shred")
st.caption(
    "Upload an RFP, requirements spec or contract. The Full Shred workbook covers show stoppers, key dates, the "
    "compliance matrix, scope, pricing, evaluation, submission mechanics, forms, contract objections and an issue "
    "log, all traced back to page and section."
)

uploaded = st.file_uploader("Document", type=list(SUPPORTED_TYPES), help="PDF, Word (.docx), .txt or .md")
if uploaded is None:
    st.info("Upload a document to get started.")
    st.stop()

data = uploaded.getvalue()
try:
    with st.spinner(f"Reading {uploaded.name}…"):
        wall = time.perf_counter()
        result, extract_seconds = convert(
            data, uploaded.name, tuple(keywords), inherit_list_items, strip_headers, exclude_table_text
        )
        cached = time.perf_counter() - wall < extract_seconds / 2
except Exception as exc:  # noqa: BLE001 - surface any parser failure to the user instead of a stack trace
    st.error(f"Could not read **{uploaded.name}**: {exc}")
    st.stop()

for warning in result.warnings:
    st.warning(warning)

stem = PurePath(uploaded.name).stem
page_count = len(result.page_texts) if result.file_type == "pdf" else None
pages = f"{page_count} pages" if page_count else result.file_type.upper()
extract_step = (f"Read & extract document ({pages})" + (" (cached)" if cached else ""), extract_seconds)

# ---------------------------------------------------------------- simple extract

if output == SIMPLE:
    frames = build_frames(result)
    cols = st.columns(4)
    cols[0].metric("Pages", len(result.page_texts) if result.file_type == "pdf" else "–")
    cols[1].metric("Sections", len(result.sections))
    cols[2].metric("Requirements", len(result.requirements))
    cols[3].metric("Tables", len(result.tables))
    if include:
        start = time.perf_counter()
        workbook = build_workbook(result, tuple(include))
        build_step = ("Build Excel workbook", time.perf_counter() - start)
        st.download_button(
            "⬇️ Download Excel workbook", data=workbook, file_name=f"{stem}.xlsx", mime=XLSX_MIME, type="primary"
        )
        show_timings([extract_step, build_step], extract_seconds + build_step[1])
    for name, tab in zip(
        ("Requirements", "Sections", "Pages"), st.tabs(["Requirements", "Sections", "Pages"]), strict=True
    ):
        with tab:
            st.dataframe(frames[name], hide_index=True, width="stretch")
    st.stop()

# ---------------------------------------------------------------- full shred

matrix_bytes = matrix_file.getvalue() if matrix_file else b""
run_key = hashlib.sha256(
    b"|".join(
        [
            data,
            matrix_bytes,
            repr((keywords, inherit_list_items, strip_headers, exclude_table_text)).encode(),
            repr((use_ai, model, effort, client, rfp_number, title, run_date)).encode(),
        ]
    )
).hexdigest()

label = f"Generate Full Shred with {model}" if use_ai else "Generate Full Shred (rule-based)"
if st.button(label, type="primary"):
    matrix_text = read_objection_matrix(matrix_bytes) if matrix_bytes else None
    bar = st.progress(0.0, text="Starting AI analysis…") if use_ai else None
    shred, workbook, total = generate_full_shred(
        result,
        extract_step,
        uploaded.name,
        run_date,
        client,
        rfp_number,
        title,
        matrix_text,
        AIConfig(api_key, model, secret("OPENAI_ORG_ID"), secret("OPENAI_PROJECT_ID"), effort) if use_ai else None,
        progress=(lambda fraction, message: bar.progress(fraction, text=message)) if bar else None,
    )
    if bar:
        bar.empty()
    try:
        record_run(shred, total, page_count or result.file_type.upper(), model if use_ai else "", effort)
    except OSError as exc:
        st.warning(f"Could not record the run time in {LOG_PATH}: {exc}")
    st.session_state["shred"] = (run_key, shred, workbook, total)

saved = st.session_state.get("shred")
if not saved or len(saved) != 4 or saved[0] != run_key:
    if saved:
        st.info("Settings or file changed. Generate again to refresh the shred.")
    elif use_ai:
        st.info("AI analysis reads the whole document with several model calls; expect about 10-15 minutes.")
    st.stop()

_, shred, workbook, total = saved
for warning in shred.warnings:
    st.warning(warning)

st.subheader(shred.title_suffix or stem)
st.caption(f"{shred.mode} · {shred.opportunity_line()}")
show_timings(shred.timings, total)
show_usage(shred, page_count)

stoppers = shred.tabs.get("Show Stoppers", [])
cols = st.columns(5)
cols[0].metric("Requirements", len(shred.tabs.get("Compliance Matrix", [])))
cols[1].metric("Critical show stoppers", sum(r.get("risk_level") == "Critical" for r in stoppers))
cols[2].metric("High show stoppers", sum(r.get("risk_level") == "High" for r in stoppers))
cols[3].metric("Open issues", sum(r.get("status", "Open") == "Open" for r in shred.tabs.get("Q&A + Issue Log", [])))
cols[4].metric("Key dates", len(shred.tabs.get("Key Dates", [])))

st.download_button(
    "⬇️ Download Full Shred workbook",
    data=workbook,
    file_name=f"{stem} - Full Shred.xlsx",
    mime=XLSX_MIME,
    type="primary",
)

names = [t.name for t in TABS if t.name != "Workbook Guide"]
choice = st.selectbox("Preview tab", names)
spec = next(t for t in TABS if t.name == choice)
rows = shred.tabs.get(choice, [])
st.caption(shred.banner_for(spec))
frame = pd.DataFrame(
    [[r.get(k, "") for k in ("id", *spec.keys)] for r in rows],
    columns=[spec.id_header, *spec.headers],
)
st.dataframe(frame, hide_index=True, width="stretch")

if runs := load_runs():
    with st.expander(f"📈 Recorded runs ({len(runs)})"):
        st.caption(f"Every Full Shred run is appended to {LOG_PATH}. Times are in seconds, cost in USD.")
        st.dataframe(pd.DataFrame(runs[::-1]), hide_index=True, width="stretch")
