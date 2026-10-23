"""Fill the Full Shred with an OpenAI model, on top of the rule-based shred.

Two phases of parallel structured-output calls:

A. Stages 1 and 2 run together.
   1. Compliance Matrix, Key Dates and the screening tabs (Accessibility, Contract Vehicle, Supplier Diversity),
      extracted from page chunks. The rows are merged in page order, each requirement's citation is checked against
      the page text (see chunking.py), and a screening tab no chunk found anything for keeps its rule-based Not Found
      row. A requirement the text check cannot place gets a second look from a small model, which may place it on
      a nearby page; a requirement whose Requirement Force disagrees with its wording gets a note. Every chunk
      sees the document outline, the evaluation factors and the pages they come from, so it can fill Evaluation Link.
   2. The topical tabs, one call per tab, over the whole document. They leave their requirement-ID columns empty,
      since the requirements do not exist yet.
B. Stage 3 and the ID links run together.
   3. Show Stoppers (split into parts of the fixed screening areas), Shred Readiness and the Q&A + Issue Log (split
      by issue kind), summarising everything before them.
   Link calls fill each topical tab's requirement-ID column from the finished Compliance Matrix. Requirements are
   linked in code to the key dates and forms they mention (Related IDs).

Columns that are the same on every row (Source File, Amendment / Version) are filled in code, not written by the
model.

Every call uses the same response schema (all tabs; a call fills only its own and returns [] for the rest). OpenAI's
prompt cache covers the schema too, so a schema per call would stop the cache from serving the document.

Any group that fails keeps its rule-based rows and adds a warning, so a run always produces a full workbook.

OpenAI's prompt cache reuses only the system message (and the schema), not the start of a user message, so
anything several calls share goes in the system message: the document, the chunk outline, and stage 3's summary of
the finished tabs. Each call's own task and excerpt go in the user message.

A call that runs far longer than the finished calls of its kind is sent again, and whichever copy finishes first is
used (_run_parallel), so a phase does not wait on one stalled request.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import date, datetime
from statistics import median

from .chunking import (
    UNVERIFIED,
    Chunk,
    add_note,
    check_citations,
    cite_page,
    cited_pages,
    merge_chunk_rows,
    page_texts_with_tables,
    plan_chunks,
    recheck_pages,
)
from .extract import ExtractionResult
from .shred import Shred, assign_ids
from .shred_schema import SHOW_STOPPER_AREAS, TAB_BY_NAME, column_key

DEFAULT_MODEL = "gpt-5.6-sol"
MODELS = (
    "gpt-6.1-sol",
    "gpt-6-sol",
    "gpt-6-luna",
    "gpt-6-astra",
    "gpt-5.6-sol",
    "gpt-5.6-luna",
    "gpt-5.6-terra",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.2",
    "gpt-5.1",
    "gpt-5",
    "gpt-5-mini",
    "gpt-5-nano",
)

# Standard-tier USD per 1M tokens: (input, cached input, output). Source: developers.openai.com/api/docs/pricing,
# checked 2026-10-21. Keep in step with the model table in README.md.
PRICES = {
    "gpt-6.1-sol": (2.00, 0.10, 10.00),
    "gpt-6-sol": (2.00, 0.20, 10.00),
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-6-astra": (10.00, 1.00, 50.00),
    "gpt-5.6-sol": (4.00, 0.40, 20.00),
    "gpt-5.6-luna": (0.20, 0.02, 1.20),
    "gpt-5.6-terra": (2.00, 0.20, 12.00),
    "gpt-5.5": (5.00, 0.50, 30.00),
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.4-nano": (0.20, 0.02, 1.25),
    "gpt-5.2": (1.75, 0.175, 14.00),
    "gpt-5.1": (1.25, 0.125, 10.00),
    "gpt-5": (1.25, 0.125, 10.00),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-5-nano": (0.05, 0.005, 0.40),
}
# Rates for a request whose input is over LONG_CONTEXT_TOKENS (only these models have a long-context tier).
LONG_CONTEXT_TOKENS = 272_000
LONG_CONTEXT_PRICES = {
    "gpt-6.1-sol": (4.00, 0.20, 15.00),
    "gpt-6-sol": (4.00, 0.40, 15.00),
    "gpt-6-luna": (0.20, 0.02, 0.75),
    "gpt-6-astra": (20.00, 2.00, 75.00),
    "gpt-5.6-sol": (8.00, 0.80, 30.00),
    "gpt-5.6-luna": (0.40, 0.04, 1.80),
    "gpt-5.6-terra": (4.00, 0.40, 18.00),
    "gpt-5.5": (10.00, 1.00, 45.00),
    "gpt-5.4": (5.00, 0.50, 22.50),
}

SYSTEM_PROMPT = """You are a proposal "shred" analyst. You turn a procurement document (RFP/RFQ/IFB) into a
source-grounded workbook for an internal proposal/pursuit team.

Rules:
- Ground every row in the document below. Never invent requirements, dates, quantities, names, or values.
- Quote or closely paraphrase source language. Keep requirement text near-verbatim.
- Source Location: "p. N, <section / heading>" (use the page markers).
- Make absences visible: where a tab screens for something the document does not contain, return an explicit
  "Not Found" row instead of an empty tab.
- Flag contradictions and ambiguities instead of silently resolving them (e.g. a clause that conflicts with the
  stated scope, a referenced form that is not supplied, a time without a time zone).
- No proposal strategy, win themes, or legal advice. Contract screening is internal, against the supplied matrix only.
- Dates as "Mon D, YYYY"; times as "2:00 PM". Use "" for a cell that does not apply.
- Controlled values (use exactly):
  Finding Status: Found / Not Found / Not Applicable / Unclear
  Risk Level and Severity: Critical / High / Moderate / Watch / Informational / None Identified / Not Applicable
  Readiness Status: Complete / Partial / Reviewed / Complete with Limits / Missing Referenced Source
  Requirement Force: Mandatory / Conditional / Advisory / Informational
  Yes/No columns: Yes / No / Conditional / Unclear / Not Applicable
  Required Stage: Pre-Bid / Proposal Submission / Pre-Award / Post-Award / Performance / Conditional / Not Applicable
  Confidence: High / Moderate / Low
- Leave the ID column out; rows are numbered with the tab's prefix (R-001, KD-002, ...) in the order you return them.
"""

TAB_GUIDANCE = {
    "Compliance Matrix": "One row per discrete obligation, instruction, or condition, in document order (atomic: "
    "split compound sentences). Requirement Type is a short label (Deadline, Q&A, Submission, Formatting, Signature, "
    "Notarization, Forms, Pricing, Insurance, Eligibility, Scope, Hardware, Performance, Contract Term, Invoicing, "
    "Evaluation, Award Rights...). Response / Artifact Needed is a short imperative. Workstream: Submission / "
    "Pricing / Forms / Legal / Eligibility / Narrative / Other. Evaluation Link names the evaluation factor and "
    "weight it supports, if any. Include evaluation weights and "
    "award/reservation-of-rights statements as Informational rows.",
    "Key Dates": "Every procurement date and deadline (questions, addenda, pre-bid, proposal due, withdrawal, "
    "award, contract term, post-award deliverables with relative timing). Do NOT add the shred run date row; it is "
    "added as KD-001 and your rows start at KD-002. Calendar Days from Shred Run is the integer (date minus shred run "
    "date). Quick Turn? only for the proposal due date: Past Due / Yes (0-7 days) / No. Time Zone 'Not stated' when "
    "absent.",
    "Scope Summary": "Overall project, every line item / quantity, services (configuration, installation, "
    "removal, training...), post-award artifacts, and any scope inconsistency (Delivery Mix: Hardware / Software / "
    "Services / Mixed / Unclear).",
    "Cost Breakdown": "Each priced element and quantity basis, required pricing format, separate-cost-volume rules, "
    "reimbursables/travel, and a row for budget/ceiling/NTE (Not Found when absent).",
    "Client Objectives": "Only objectives and desired outcomes the document states. Implication for Response is "
    "factual, not strategy.",
    "Proposal Outline": "Required response items in the order the document mandates (TOC Level: Package / Section / "
    "Subsection / Forms / Post-Award), separating narrative items from forms, pricing and artifacts. Note when a "
    "heading is an evaluation factor rather than a mandated section title.",
    "Evaluation Summary": "Each stated factor with its exact weight/points; thresholds (e.g. minimum scores) and "
    "steps as rows; never invent a formula.",
    "Submission Mechanics": "Delivery method, addresses/emails, subject lines, copies/originals, labeling, "
    "signatures, notarization, file format/size, package order, redacted copies, deadlines, post-award submissions.",
    "Formatting + Limits": "Explicit format rules (page limits, file type/size, ink, ordering, indexing) plus Not "
    "Found rows for page limit and font/margin when absent.",
    "Accessibility 508 ADA": "Any Section 508 / ADA / WCAG / accessible-document requirement, or one Not Found row.",
    "Contract Vehicle Eligibility": "E-Rate/SPIN or similar program eligibility, cooperative contracts, approved "
    "pools, or one Not Found row.",
    "Supplier Diversity Set-Aside": "Set-asides, MBE/WBE/DBE goals, certifications, participation %, or one Not "
    "Found row.",
    "Work Samples + References": "References, similar-project proof, examples of work, work samples (with quantity, "
    "recency, required fields). Not Found rows for types that are not required.",
    "Forms + Attachments": "Every form, signature page, affidavit, certificate, W-9, notarization, corporate seal, "
    "identifier (e.g. SPIN) and referenced attachment, including referenced-but-missing ones (File Provided? = No).",
    "Contract Objections": "Screen the contract terms ONLY against the internal objection matrix supplied below. "
    "Return a row only for a clear match to a matrix trigger (including a matrix trigger fired by the absence of a "
    "clause). Client Restriction notes when signing/submission equals acceptance. If no matrix is supplied, return a "
    "single row whose Notes say the terms were not screened because no matrix was supplied.",
    "Source Inventory": "The supplied file, every referenced-but-missing source (addenda, Q&A, forms, attachments), "
    "and the objection matrix if supplied (Procurement Authority? = No).",
    "Amendment + Delta Log": "Baseline row, plus a source-gap row for any referenced addendum/Q&A that was not "
    "supplied. Do not invent deltas.",
    "Show Stoppers": "Return exactly one row per fixed screening area, in the given order, with the given Screening "
    "Area and Screening Type. Finding Status and Risk Level are separate judgements. Decision Relevance explains why "
    "it matters for a human go/no-go discussion (no pursue decision). Cite related R-IDs.",
    "Shred Readiness": "First row is 'Overall shred readiness' with Status Partial / Complete with Limits / "
    "Complete. Then rows for source review, each missing referenced source, the objection-matrix review, internal "
    "source consistency (contradictions), and deadline status.",
    "Q&A + Issue Log": "Every contradiction, missing source, ambiguity and open question found across all tabs, "
    "with severity, the decision owner needed, impact, related IDs from the other tabs, and the recommended "
    "clarification.",
}

SCREENING_TABS = ("Accessibility 508 ADA", "Contract Vehicle Eligibility", "Supplier Diversity Set-Aside")
# Stage 1 calls per chunk: each tuple is one call.
CHUNK_CALLS = (("Compliance Matrix",), ("Key Dates",), SCREENING_TABS)
STAGE1_TABS = tuple(name for call in CHUNK_CALLS for name in call)
STAGE2_TABS = (
    "Scope Summary",
    "Cost Breakdown",
    "Client Objectives",
    "Proposal Outline",
    "Evaluation Summary",
    "Submission Mechanics",
    "Formatting + Limits",
    "Work Samples + References",
    "Forms + Attachments",
    "Contract Objections",
    "Source Inventory",
    "Amendment + Delta Log",
)
STAGE3_GROUP = ("Show Stoppers", "Shred Readiness", "Q&A + Issue Log")
AI_TABS = STAGE1_TABS + STAGE2_TABS + STAGE3_GROUP

# Per-model defaults, from benchmarks on a 31-page RFP: gpt-5-nano at medium effort spent ~80% of its output on
# reasoning and repeated rows, so it gets low effort and smaller chunks. Other models use the fallbacks; for
# gpt-6-luna, 6,000-character chunks cost ~2 points of grounding and ~4 of recall against 9,000.
MODEL_SETTINGS = {
    "gpt-5-nano": {"reasoning_effort": "low", "chunk_chars": 6000},
}
DEFAULT_EFFORT = "medium"
DEFAULT_CHUNK_CHARS = 9000


def default_effort(model: str) -> str:
    return MODEL_SETTINGS.get(model, {}).get("reasoning_effort", DEFAULT_EFFORT)


def default_chunk_chars(model: str) -> int:
    return MODEL_SETTINGS.get(model, {}).get("chunk_chars", DEFAULT_CHUNK_CHARS)


CHUNK_RULES = """
You are given ONE EXCERPT of the document (the pages named in the task), plus the document outline for orientation.
- Return rows only for content that begins on the excerpt's own pages, and cite pages inside the excerpt.
- Text marked CONTEXT ONLY belongs to the next excerpt: read it to complete a row, never return rows that begin there.
- Leave Related IDs empty; they are linked later.
- Evaluation Link: when a row clearly supports one of the EVALUATION FACTORS below, name that factor and its weight;
  otherwise leave it empty. The EVALUATION RUBRIC (the pages the factors come from) says what each factor scores;
  it is reference only, so never return rows from it unless those pages are also in your excerpt.
- Screening tabs (Accessibility 508 ADA, Contract Vehicle Eligibility, Supplier Diversity Set-Aside): return rows only
  for matching content in the excerpt, and an empty array when there is none. Never return Not Found rows for them;
  absence across the whole document is recorded later.
"""
SCHEMA_NAME = "full_shred"

# Columns filled in code (same value on every row), so the model does not spend output tokens on them.
CODE_FILLED = {"source_file"}
CODE_FILLED_BY_TAB = {"Compliance Matrix": {"amendment_version"}}
# Requirement-ID columns of the tabs built before the Compliance Matrix exists, filled by link calls.
LINKED_COLUMNS = {
    "Proposal Outline": "requirement_ids",
    "Evaluation Summary": "related_requirement_ids",
    "Submission Mechanics": "related_requirement_ids",
    "Formatting + Limits": "related_requirement_ids",
    "Work Samples + References": "related_requirement_ids",
    "Forms + Attachments": "related_requirement_ids",
    "Contract Objections": "related_requirement_ids",
    "Accessibility 508 ADA": "related_requirement_ids",
    "Contract Vehicle Eligibility": "related_requirement_ids",
    "Supplier Diversity Set-Aside": "related_requirement_ids",
}
STAGE2_RULE = (
    "The Compliance Matrix is built in parallel with this call: leave Requirement IDs / Related Requirement IDs "
    "empty; they are linked afterwards."
)
# Characters of the evaluation pages shown to every chunk call (in the system message, so it is cached). Pages
# between two cited evaluation pages up to RUBRIC_GAP_PAGES apart are included: the rule-based shred finds the factor
# table and the scoring steps, and the rubric often sits between them.
EVALUATION_RUBRIC_CHARS = 15_000
RUBRIC_GAP_PAGES = 5
# Requirements the text check could not place get a second look from this model, whichever model runs the shred.
RECHECK_MODEL = "gpt-5-nano"
RECHECK_PROMPT = """You check where a requirement extracted from a procurement document appears in that document.
You are given the requirement, the location another model cited for it, and the text of some pages. Return the number
of the page that states the same requirement (the wording may differ: paraphrased, split across lines, or in a table),
or 0 when none of the pages states it. Return 0 rather than guess.
"""
RECHECK_SCHEMA = {
    "type": "object",
    "properties": {"page": {"type": "integer"}},
    "required": ["page"],
    "additionalProperties": False,
}
# Wording that suggests a Requirement Force. "May not" is a prohibition; "may" after "that" / "which" / "who" is in a
# subordinate clause ("barriers that may affect..."), and before a number it is the month. "When" is left out of
# conditions, since "When submitting, vendors must..." does not make the obligation conditional.
MANDATORY_WORDING = re.compile(r"\b(shall|must|required|may not)\b", re.I)
ADVISORY_WORDING = re.compile(
    r"\b((?<!\bthat )(?<!\bwhich )(?<!\bwho )may(?!\s+(\d|not\b))|should|encouraged|recommended|optional|preferred)\b",
    re.I,
)
CONDITIONAL_WORDING = re.compile(r"\b(if|unless|in the event|provided that|where applicable)\b", re.I)
SHOW_STOPPER_PARTS = 3
# A call still running after STRAGGLER_FACTOR x the median time of its finished peers (and at least
# STRAGGLER_MIN_SECONDS) is sent again. Peers must be at least half of their kind, and two or more.
STRAGGLER_FACTOR = 2.0
STRAGGLER_MIN_SECONDS = 30.0
ISSUE_LOG_PARTS = (
    "Cover ONLY contradictions (between pages, sections or tabs) and missing or referenced-but-unsupplied sources.",
    "Cover ONLY ambiguities and open questions that need a clarification; contradictions and missing sources are "
    "logged by a separate call.",
)
LINK_PROMPT = """You link rows of a proposal shred to the requirements they relate to.
For each row given, return the IDs of the Compliance Matrix requirements (R-NNN) that the row is about, supports,
or depends on: most relevant first, at most 6, comma-separated. Use only IDs from the Compliance Matrix below. Return
an empty string when no requirement relates. Return one entry per row, using the row's ID.
"""
LINK_SCHEMA = {
    "type": "object",
    "properties": {
        "links": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "requirement_ids": {"type": "string"}},
                "required": ["id", "requirement_ids"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["links"],
    "additionalProperties": False,
}


class ShredAIError(RuntimeError):
    pass


@dataclass
class AIConfig:
    api_key: str
    model: str = DEFAULT_MODEL
    organization: str | None = None
    project: str | None = None
    reasoning_effort: str = DEFAULT_EFFORT
    # Stage 1 reads the document in chunks of about this many characters; 0 reads it whole, None uses the
    # model's default (MODEL_SETTINGS).
    chunk_chars: int | None = None
    max_parallel: int = 48


def document_text(result: ExtractionResult) -> str:
    parts = []
    if result.file_type == "pdf":
        parts += [f"=== Page {i} ===\n{text}" for i, text in enumerate(result.page_texts, start=1)]
    else:
        parts.append(result.page_texts[0] if result.page_texts else "")
    for i, table in enumerate(result.tables, start=1):
        rows = "\n".join(" | ".join(c.replace("\n", " ") for c in row) for row in table.rows)
        parts.append(f"=== Table {i}" + (f" (page {table.page})" if table.page else "") + f" ===\n{rows}")
    return "\n\n".join(parts)


def build_ai_shred(
    base: Shred,
    result: ExtractionResult,
    config: AIConfig,
    matrix_text: str | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> Shred:
    """Replace the tabs of a rule-based shred with model output; failed groups keep their rule-based rows."""
    from openai import OpenAI

    client = OpenAI(
        api_key=config.api_key,
        organization=config.organization or None,
        project=config.project or None,
        # Calls are short now (the slowest ~90 s); a stalled request is cut off and retried instead of waiting 15 min.
        timeout=300,
        max_retries=2,
    )
    system = (
        f"{SYSTEM_PROMPT}\nSource file name: {base.source_file}\n\n"
        f"===== INTERNAL CONTRACTS PROPOSAL OBJECTION MATRIX =====\n{matrix_text or '(none supplied)'}\n\n"
        f"===== PROCUREMENT DOCUMENT =====\n{document_text(result)}"
    )
    shred = base
    shred.mode = f"AI ({config.model})"
    shred.warnings = [w for w in shred.warnings if not w.startswith("Rule-based shred")]
    report = progress or (lambda _f, _m: None)

    def run(names: tuple[str, ...], context: str, note: str = "", areas: range | None = None) -> dict:
        date = shred.run_date.isoformat()
        return _call(client, config, system, names, context, date, usage=shred.usage, note=note, areas=areas)

    chunks = plan_chunks(
        result, default_chunk_chars(config.model) if config.chunk_chars is None else config.chunk_chars
    )
    chunk_system = (
        f"{SYSTEM_PROMPT}{CHUNK_RULES}\nSource file name: {base.source_file}\n\n"
        f"===== DOCUMENT OUTLINE =====\n{_outline(result)}\n\n"
        f"===== EVALUATION FACTORS =====\n{_evaluation_factors(base)}\n\n"
        f"===== EVALUATION RUBRIC =====\n{_evaluation_rubric(base, result)}"
    )

    def run_chunk(names: tuple[str, ...], i: int) -> dict:
        chunk = chunks[i]
        excerpt = f"\n===== EXCERPT: {chunk.label} =====\n{chunk.text}"
        date = shred.run_date.isoformat()
        # The first chunk holds the cover page, so its Key Dates call also reads the opportunity details.
        opportunity = names == ("Key Dates",) and i == 0
        return _call(client, config, chunk_system, names, "", date, opportunity, usage=shred.usage, excerpt=excerpt)

    page_texts = page_texts_with_tables(result)

    def recheck(row: dict, pages: list[int]) -> int:
        return _recheck_call(client, config, row, pages, page_texts, shred.usage)

    rule_show_stoppers = list(shred.tabs.get("Show Stoppers", []))
    report(0.05, f"Stages 1-2: requirements from {len(chunks)} chunks, and the topical tabs…")
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as pool:
        stage1 = pool.submit(_run_stage1, shred, result, chunks, run_chunk, recheck, config.max_parallel)
        stage2 = pool.submit(
            _run_groups,
            shred,
            "AI stage 2",
            tuple((n,) for n in STAGE2_TABS),
            lambda names: run(names, "", STAGE2_RULE),
        )
        stage1.result(), stage2.result()
    shred.timings.append(("AI stages 1+2 total (run together)", time.perf_counter() - start))
    _fill_code_columns(shred)
    _link_related_ids(shred)

    report(0.6, "Stage 3: show stoppers, readiness, issue log, and requirement links…")
    context = _summary(shred, [n for n in shred.tabs if n not in STAGE3_GROUP])
    _run_stage3(shred, run, context, lambda tab: _link_call(client, config, shred, tab))
    _pin_show_stoppers(shred, rule_show_stoppers)
    _fill_code_columns(shred)

    readiness = next(iter(shred.tabs.get("Shred Readiness", [])), {})
    if readiness:
        shred.banners["Shred Readiness"] = f"{readiness.get('status')}: {readiness.get('issue_limitation', '')}"
        shred.guide_notes["Shred Readiness"] = shred.banners["Shred Readiness"]
    report(1.0, "Done")
    return shred


def _run_stage1(
    shred: Shred, result: ExtractionResult, chunks: list[Chunk], run_chunk, recheck, max_parallel: int
) -> None:
    """Extract the stage-1 tabs chunk by chunk in parallel, then merge and check them.

    A failed chunk keeps the rule-based rows that cite its pages, so the tab still covers the whole document.
    """
    rule_rows = {name: list(shred.tabs.get(name, [])) for name in STAGE1_TABS}
    jobs = {(names, i): lambda j=(names, i): run_chunk(*j) for i in range(len(chunks)) for names in CHUNK_CALLS}

    start = time.perf_counter()
    # Peers are the calls for the same tabs; a table-only chunk is shorter than the others, which is harmless.
    results = _run_parallel(jobs, lambda job: job[0], max_parallel)

    rows = {name: [[] for _ in chunks] for name in STAGE1_TABS}
    for (names, i), (data, exc, seconds, resent) in results.items():
        chunk = chunks[i]
        label = "Screening" if names == SCREENING_TABS else names[0]
        shred.timings.append((f"AI stage 1 · {label} · {chunk.label}{' (resent)' if resent else ''}", seconds))
        if exc is None:
            for name in names:
                rows[name][i] = data[column_key(name)]
            opportunity = data.get("opportunity") or {}
            if opportunity.get("client") or opportunity.get("title"):
                _apply_opportunity(shred, opportunity)
            continue
        for name in names:
            rows[name][i] = [
                r for r in rule_rows[name][1 if name == "Key Dates" else 0 :] if chunk.owns(r.get("source_location"))
            ]
        shred.warnings.append(f"AI failed for {label}, {chunk.label} ({exc}); that part uses rule-based rows.")

    for name in SCREENING_TABS:
        keys = TAB_BY_NAME[name].keys
        # Models return "Not Found" rows per chunk despite the instructions; absence is decided here instead.
        matches = [[r for r in chunk_rows if _is_finding(r, keys)] for chunk_rows in rows[name]]
        found = merge_chunk_rows(matches, lambda r, k=keys: " ".join(str(r.get(x, "")) for x in k[:2]), chunks)
        # Nothing found in any chunk: the rule-based rows stand (a Not Found row, or keyword hits marked for review).
        shred.tabs[name] = found or rule_rows[name]

    requirements = merge_chunk_rows(rows["Compliance Matrix"], lambda r: str(r.get("requirement_text", "")), chunks)
    checks = check_citations(requirements, result, "requirement_text")
    if checks["unverified"]:
        recheck_start = time.perf_counter()
        placed = _second_look(requirements, result, recheck, max_parallel)
        shred.timings.append(("AI stage 1 · Citation second look", time.perf_counter() - recheck_start))
        left = checks["unverified"] - placed
        shred.warnings.append(
            f"{checks['unverified']} of {checks['checked']} requirements could not be found on their cited page by "
            f"the text check; a second check ({RECHECK_MODEL}) placed {placed} on a page."
            + (f" {left} stay marked Unverified in Notes." if left else "")
        )
    flagged = _check_force(requirements)
    if flagged:
        shred.warnings.append(
            f'{flagged} requirements have a Requirement Force their wording does not match; see "Force check" in Notes.'
        )
    shred.timings.append(("AI stage 1 total (chunks run in parallel)", time.perf_counter() - start))
    shred.tabs["Compliance Matrix"] = requirements
    dates = merge_chunk_rows(
        rows["Key Dates"], lambda r: " ".join(str(r.get(k, "")) for k in ("date_type", "date", "time")), chunks
    )
    for row in dates:
        _normalize_date(row, shred.run_date)
    # KD-001 is the rule-based shred run date row; the model's rows follow it.
    shred.tabs["Key Dates"] = rule_rows["Key Dates"][:1] + dates
    assign_ids(shred.tabs, list(STAGE1_TABS))


def _is_finding(row: dict, keys: tuple[str, ...]) -> bool:
    """A screening row that reports something found, not an absence ("Not Found") or an empty row."""
    lead = " ".join(str(row.get(k) or "") for k in keys[:2]).strip()
    return bool(lead) and "not found" not in f"{lead} {row.get('notes') or ''}".lower()


def _second_look(rows: list[dict], result: ExtractionResult, recheck, max_parallel: int) -> int:
    """Ask a small model where each Unverified requirement is, among its cited and nearby pages; returns how many it
    placed.

    A page it names (one of those shown) replaces the citation. Any other answer leaves the row marked Unverified:
    rows are never dropped, because on rfp-1.pdf both rows gpt-5-nano found on no page were in a table on a page it
    was shown.
    """
    shown = {
        i: recheck_pages(r, result, "requirement_text") for i, r in enumerate(rows) if UNVERIFIED in r.get("notes", "")
    }
    results = _run_parallel(
        {i: lambda i=i: recheck(rows[i], shown[i]) for i in shown}, lambda _i: "recheck", max_parallel
    )
    placed = 0
    for i, (page, exc, _seconds, _resent) in results.items():
        if exc is not None or page not in shown[i]:
            continue
        row = rows[i]
        row["notes"] = "; ".join(n for n in row["notes"].split("; ") if n != UNVERIFIED)
        cite_page(row, page)
        add_note(row, f"Placed on p. {page} by a second check; the wording differs from the page.")
        placed += 1
    return placed


def _wording_force(text: str) -> tuple[str, str] | None:
    """The Requirement Force a requirement's wording suggests, and the word that suggests it; None if it has none."""
    force = MANDATORY_WORDING.search(text) or ADVISORY_WORDING.search(text)
    if not force:
        return None
    if condition := CONDITIONAL_WORDING.search(text):
        return "Conditional", condition.group(0)
    return ("Mandatory" if MANDATORY_WORDING.search(text) else "Advisory"), force.group(0)


def _check_force(rows: list[dict]) -> int:
    """Note each requirement whose Requirement Force disagrees with its wording; returns how many.

    The label is not changed: wording alone misreads cases such as "The Department may reject...". Only Mandatory
    and Advisory rows are checked. Informational rows describe the issuer's process, and a Conditional row's
    condition is often in the text around it ("The vendor must support the LEA..." when the LEA opts in): on
    rfp-1.pdf, 30 of 41 notes were on Conditional rows that were labelled correctly.
    """
    flagged = 0
    for row in rows:
        label = str(row.get("requirement_force", "")).strip()
        wording = _wording_force(str(row.get("requirement_text", "")))
        if label in ("Mandatory", "Advisory") and wording and wording[0] != label:
            add_note(row, f'Force check: the wording ("{wording[1]}") reads as {wording[0]}, not {label}.')
            flagged += 1
    return flagged


DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%b. %d, %Y", "%Y-%m-%d", "%m/%d/%Y")
DATE_IN_TEXT = re.compile(r"\b[A-Z][a-z]{2,8}\.? \d{1,2}, \d{4}\b")


def _parse_date(text: str) -> date | None:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _normalize_date(row: dict, run_date: date) -> None:
    """Write a parseable date as "Mon D, YYYY" and compute its days from the run date (models slip on both)."""
    parsed = _parse_date(str(row.get("date", "")))
    if parsed:
        row["date"] = f"{parsed:%b} {parsed.day}, {parsed.year}"
        row["calendar_days_from_shred_run"] = str((parsed - run_date).days)


def _normalized(text) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split())


def _link_related_ids(shred: Shred, limit: int = 6) -> None:
    """Fill an empty Related IDs cell with the key dates a requirement states and the forms it names.

    Chunk calls cannot see each other's IDs, so this is done in code: a key date matches when the requirement text
    contains that date; a form matches when the requirement text contains the form's name (before any parenthesis).
    """
    dates = [(r["id"], _parse_date(str(r.get("date", "")))) for r in shred.tabs.get("Key Dates", [])[1:]]
    forms = [
        (r["id"], name)
        for r in shred.tabs.get("Forms + Attachments", [])
        if len(name := _normalized(str(r.get("form_attachment", "")).split("(")[0])) >= 6
    ]
    for row in shred.tabs.get("Compliance Matrix", []):
        if row.get("related_ids"):
            continue
        text = str(row.get("requirement_text", ""))
        stated = {_parse_date(d) for d in DATE_IN_TEXT.findall(text)} - {None}
        words = f" {_normalized(text)} "
        ids = [i for i, d in dates if d and d in stated] + [i for i, name in forms if f" {name} " in words]
        row["related_ids"] = ", ".join(dict.fromkeys(ids[:limit]))


def _evaluation_factors(shred: Shred) -> str:
    """The evaluation factors and weights the rule-based shred found, for chunk calls to fill Evaluation Link."""
    lines = [
        f"- {r.get('criterion_factor')}" + (f" ({r['weight_points']})" if r.get("weight_points") else "")
        for r in shred.tabs.get("Evaluation Summary", [])
        if r.get("criterion_factor")
    ]
    return "\n".join(lines) or "(none detected; leave Evaluation Link empty unless the excerpt names a factor)"


def _evaluation_rubric(shred: Shred, result: ExtractionResult, limit: int = EVALUATION_RUBRIC_CHARS) -> str:
    """The text the rule-based evaluation factors were found in (their pages, or sections without pages), capped."""
    locations = [str(r.get("source_location", "")) for r in shred.tabs.get("Evaluation Summary", [])]
    if result.file_type == "pdf":
        texts = page_texts_with_tables(result)
        cited = sorted({p for loc in locations for p in cited_pages(loc, len(texts))})
        pages = [
            p
            for a, b in zip(cited, cited[1:] + cited[-1:])
            for p in (range(a, b) if b - a <= RUBRIC_GAP_PAGES else [a])
        ]
        pages = sorted(set(pages + cited[-1:]))
        parts = [f"=== Page {p} ===\n{texts[p - 1]}" for p in pages]
    else:
        parts = [
            f"{s.heading}\n{s.content}"
            for s in result.sections
            if s.heading and any(s.heading[:67] in loc for loc in locations)
        ]
    text = "\n\n".join(parts)
    if not text:
        return "(not found)"
    return text if len(text) <= limit else text[:limit] + "\n[…rest of the evaluation pages omitted]"


def _apply_opportunity(shred: Shred, opp: dict) -> None:
    shred.client = shred.client if shred.client_overridden else (opp.get("client") or shred.client)
    shred.rfp_number = shred.rfp_number if shred.rfp_overridden else (opp.get("rfp_number") or shred.rfp_number)
    shred.title = shred.title if shred.title_overridden else (opp.get("title") or shred.title)


def _outline(result: ExtractionResult) -> str:
    """Top two heading levels with their pages, so a chunk call knows where its excerpt sits."""
    lines = [
        f"{'  ' * (s.level - 1)}{s.heading[:100]}" + (f" (p. {s.page})" if s.page else "")
        for s in result.sections
        if s.level <= 2
    ]
    return "\n".join(lines) or "(no headings detected)"


def _run_groups(shred: Shred, stage: str, groups, call) -> None:
    """Run each group's call in parallel and store one list of rows per tab."""

    start = time.perf_counter()
    # Every group reads the whole document, so they are all peers.
    results = _run_parallel({names: lambda n=names: call(n) for names in groups}, lambda _names: stage)
    elapsed = time.perf_counter() - start
    for names, (data, exc, seconds, resent) in results.items():
        shred.timings.append((f"{stage} · {', '.join(names)}{' (resent)' if resent else ''}", seconds))
        if exc is not None:
            shred.warnings.append(f"AI failed for {', '.join(names)} ({exc}); those tabs are rule-based.")
            continue
        for name in names:
            if data[column_key(name)]:
                shred.tabs[name] = data[column_key(name)]
            else:
                # The prompt asks for explicit Not Found rows, so an empty tab means the call did not do the work.
                shred.warnings.append(f"AI returned no rows for {name}; that tab is rule-based.")
        assign_ids(shred.tabs, list(names))
    if len(groups) > 1:
        shred.timings.append((f"{stage} total (groups run in parallel)", elapsed))


def _run_stage3(shred: Shred, run, context: str, link) -> None:
    """Show Stoppers in parts, Shred Readiness, the issue log by issue kind, and the link calls, all in parallel.

    A split tab is assembled from its parts in order (the issue log without repeats). A failed or empty part is
    reported; the tab keeps its rule-based rows only when no part returned rows.
    """
    size = -(-len(SHOW_STOPPER_AREAS) // SHOW_STOPPER_PARTS)
    jobs = {
        f"Show Stoppers {i + 1}/{SHOW_STOPPER_PARTS}": (
            "Show Stoppers",
            lambda a=range(i * size, min((i + 1) * size, len(SHOW_STOPPER_AREAS))): run(
                ("Show Stoppers",), context, areas=a
            ),
        )
        for i in range(SHOW_STOPPER_PARTS)
    }
    jobs["Shred Readiness"] = ("Shred Readiness", lambda: run(("Shred Readiness",), context))
    for i, note in enumerate(ISSUE_LOG_PARTS):
        jobs[f"Q&A + Issue Log {i + 1}/{len(ISSUE_LOG_PARTS)}"] = (
            "Q&A + Issue Log",
            lambda n=note: run(("Q&A + Issue Log",), context, n),
        )
    for tab in LINKED_COLUMNS:
        if shred.tabs.get(tab):
            jobs[f"Links · {tab}"] = (None, lambda t=tab: link(t))

    start = time.perf_counter()
    # Peers are calls of the same kind (Show Stoppers parts, issue-log parts, link calls).
    results = _run_parallel(
        {label: fn for label, (_, fn) in jobs.items()}, lambda label: jobs[label][0] or "Links", len(jobs)
    )
    elapsed = time.perf_counter() - start

    parts: dict[str, list[list[dict]]] = {name: [] for name in STAGE3_GROUP}
    for label, (data, exc, seconds, resent) in results.items():
        tab = jobs[label][0]
        shred.timings.append((f"AI stage 3 · {label}{' (resent)' if resent else ''}", seconds))
        if exc is not None:
            shred.warnings.append(
                f"AI failed for {label} ({exc}); " + ("rule-based rows fill in." if tab else "links left empty.")
            )
        elif tab is None:
            _apply_links(shred, label.removeprefix("Links · "), data)
        elif tab:
            parts[tab].append(data[column_key(tab)])
            if not data[column_key(tab)]:
                shred.warnings.append(f"AI returned no rows for {label}.")
    shred.timings.append(("AI stage 3 total (groups run in parallel)", elapsed))

    for tab, chunks in parts.items():
        rows = (
            merge_chunk_rows(chunks, lambda r: str(r.get("question_conflict", "")))
            if tab == "Q&A + Issue Log"
            else [r for part in chunks for r in part]
        )
        if rows:
            shred.tabs[tab] = rows
        elif chunks:
            shred.warnings.append(f"AI returned no rows for {tab}; that tab is rule-based.")
        assign_ids(shred.tabs, [tab])


def _link_call(client, config: AIConfig, shred: Shred, tab: str) -> dict[str, str]:
    """One tab's requirement IDs per row ID, from the Compliance Matrix (only IDs that exist are kept).

    Returned rather than written to the rows, because a resent copy of the call may still be running.
    """
    column = LINKED_COLUMNS[tab]
    requirements = shred.tabs.get("Compliance Matrix", [])
    known = {r["id"] for r in requirements}
    matrix = "\n".join(
        f"{r['id']}: {str(r.get('requirement_text', ''))[:200]} [{r.get('source_location', '')}]" for r in requirements
    )
    spec = TAB_BY_NAME[tab]
    rows = "\n".join(
        f"{r['id']}: "
        + " | ".join(
            str(r.get(k, ""))[:160] for k in spec.keys if r.get(k) and k not in (column, "source_file", "notes")
        )
        for r in shred.tabs[tab]
    )
    system = f"{LINK_PROMPT}\n===== COMPLIANCE MATRIX =====\n{matrix}"
    user = f"Tab: {tab}\nRows:\n{rows}"
    links: dict[str, list[str]] = {}
    for _attempt in range(2):
        data = _request(client, config, system, user, "requirement_links", LINK_SCHEMA, f"Links · {tab}", shred.usage)
        by_id = {link["id"]: link["requirement_ids"] for link in data["links"]}
        links = {
            r["id"]: [i for i in re.findall(r"R-\d{3,}", by_id.get(r["id"], "")) if i in known] for r in shred.tabs[tab]
        }
        # No link at all for a tab with real rows is a silent failure (seen once for Forms + Attachments): retry once.
        if any(links.values()) or len(shred.tabs[tab]) < 3:
            break
    else:
        raise ShredAIError("the link call returned no requirement IDs for any row")
    return {row_id: ", ".join(dict.fromkeys(ids[:6])) for row_id, ids in links.items()}


def _recheck_call(client, config: AIConfig, row: dict, pages: list[int], page_texts: list[str], usage) -> int:
    """The page (one of `pages`) that states the requirement, or 0, from RECHECK_MODEL."""
    user = f"Requirement: {row.get('requirement_text', '')}\nCited: {row.get('source_location', '')}\n\n" + "\n\n".join(
        f"=== Page {p} ===\n{page_texts[p - 1]}" for p in pages
    )
    small = replace(config, model=RECHECK_MODEL, reasoning_effort="low")
    data = _request(client, small, RECHECK_PROMPT, user, "requirement_page", RECHECK_SCHEMA, "Citation recheck", usage)
    return int(data["page"])


def _apply_links(shred: Shred, tab: str, links: dict[str, str]) -> None:
    column = LINKED_COLUMNS[tab]
    for row in shred.tabs.get(tab, []):
        row[column] = links.get(row["id"], "")


def _run_parallel(jobs: dict, peer_group: Callable, max_parallel: int | None = None) -> dict:
    """Run every job at once; resend a straggler and keep whichever copy finishes first.

    Returns {key: (result, exception, seconds, resent)} in the order of `jobs`; seconds run from the first send.
    A copy that loses the race is left to finish in the background (its tokens are still billed and recorded).
    """
    results: dict = {}
    if not jobs:
        return results
    started: dict = {}
    pending: dict = {}  # future -> key
    resent: set = set()
    finished: dict[str, list[float]] = {}
    groups = {key: peer_group(key) for key in jobs}
    group_size = {g: list(groups.values()).count(g) for g in set(groups.values())}

    def attempt(key):
        started.setdefault(key, time.perf_counter())
        try:
            return jobs[key](), None
        except Exception as exc:  # noqa: BLE001 - the caller keeps the rule-based rows and reports the failure
            return None, exc

    # A resent copy waits for a free worker; workers free up as the peers it is measured against finish.
    pool = ThreadPoolExecutor(max_workers=max(1, min(max_parallel or len(jobs), len(jobs))))
    try:
        for key in jobs:
            pending[pool.submit(attempt, key)] = key
        while len(results) < len(jobs):
            done, _ = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
            now = time.perf_counter()
            for future in done:
                key = pending.pop(future)
                if key in results:
                    continue  # the other copy already won
                data, exc = future.result()
                if exc is not None and key in pending.values():
                    continue  # this copy failed; wait for the other one
                results[key] = (data, exc, now - started[key], key in resent)
                if exc is None:
                    finished.setdefault(groups[key], []).append(now - started[key])
            for key in jobs:
                peers = finished.get(groups[key], [])
                waiting = key in results or key in resent or key not in started
                if waiting or len(peers) < max(2, group_size[groups[key]] / 2):
                    continue
                if now - started[key] > max(STRAGGLER_MIN_SECONDS, STRAGGLER_FACTOR * median(peers)):
                    resent.add(key)
                    pending[pool.submit(attempt, key)] = key
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return {key: results[key] for key in jobs}


def _fill_code_columns(shred: Shred) -> None:
    """Fill the columns the model is not asked for (CODE_FILLED); rule-based rows already have them."""
    for name in AI_TABS:
        for row in shred.tabs.get(name, []):
            if "source_file" in TAB_BY_NAME[name].keys:
                row.setdefault("source_file", shred.source_file)
    for row in shred.tabs.get("Compliance Matrix", []):
        row.setdefault("amendment_version", f"Baseline {shred.rfp_number}".strip())


def _pin_show_stoppers(shred: Shred, rule_rows: list[dict] | None = None) -> None:
    """Show Stoppers has fixed rows: force the area names/order; an area no part returned keeps its rule-based row."""
    rows = shred.tabs.get("Show Stoppers", [])
    by_area = {r.get("screening_area", "").lower(): r for r in rows}
    by_rule = {r.get("screening_area", "").lower(): r for r in rule_rows or []}
    # By position only when every area came back, so a reworded area name still lands on its own row.
    complete = len(rows) == len(SHOW_STOPPER_AREAS)
    pinned = []
    for i, (area, kind) in enumerate(SHOW_STOPPER_AREAS):
        row = (
            by_area.get(area.lower())
            or (rows[i] if complete else None)
            or by_rule.get(area.lower())
            or {"finding_status": "Unclear"}
        )
        pinned.append({**row, "screening_area": area, "screening_type": kind})
    shred.tabs["Show Stoppers"] = pinned
    assign_ids(shred.tabs, ["Show Stoppers"])


def _summary(shred: Shred, names) -> str:
    """Compact listing of finished tabs so later stages can cite their IDs."""
    lines = []
    for name in names:
        spec = TAB_BY_NAME[name]
        lines.append(f"## {name}")
        for row in shred.tabs.get(name, []):
            fields = [str(row.get(k, ""))[:160] for k in spec.keys[:4] if row.get(k)]
            lines.append(f"{row.get('id', '')}: " + " | ".join(fields))
    return "\n".join(lines)


def _model_keys(name: str) -> list[str]:
    skip = CODE_FILLED | CODE_FILLED_BY_TAB.get(name, set())
    return [k for k in TAB_BY_NAME[name].keys if k not in skip]


def _tab_schema(name: str) -> dict:
    keys = _model_keys(name)
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {k: {"type": "string"} for k in keys},
            "required": keys,
            "additionalProperties": False,
        },
    }


def cost_usd(model: str, input_tokens: int, cached_tokens: int, output_tokens: int) -> float | None:
    """Price of one request; None for a model without a known price. Output includes reasoning tokens."""
    long_context = input_tokens > LONG_CONTEXT_TOKENS and model in LONG_CONTEXT_PRICES
    prices = LONG_CONTEXT_PRICES[model] if long_context else PRICES.get(model)
    if prices is None:
        return None
    price_in, price_cached, price_out = prices
    return ((input_tokens - cached_tokens) * price_in + cached_tokens * price_cached + output_tokens * price_out) / 1e6


def call_usage(model: str, step: str, usage) -> dict:
    """Token counts and cost of one chat completion, from its `usage` block."""
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    output_details = getattr(usage, "completion_tokens_details", None)
    cached = getattr(prompt_details, "cached_tokens", 0) or 0
    return {
        "step": step,
        "input_tokens": usage.prompt_tokens,
        "cached_input_tokens": cached,
        "output_tokens": usage.completion_tokens,
        "reasoning_tokens": getattr(output_details, "reasoning_tokens", 0) or 0,
        "cost_usd": cost_usd(model, usage.prompt_tokens, cached, usage.completion_tokens),
    }


def shared_schema() -> dict:
    """One schema for every call: all AI tabs plus the opportunity details."""
    properties = {column_key(n): _tab_schema(n) for n in AI_TABS}
    properties["opportunity"] = {
        "type": "object",
        "properties": {k: {"type": "string"} for k in ("client", "rfp_number", "title")},
        "required": ["client", "rfp_number", "title"],
        "additionalProperties": False,
    }
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _call(
    client,
    config: AIConfig,
    system: str,
    names,
    context: str,
    run_date: str,
    opportunity: bool = False,
    usage: list | None = None,
    excerpt: str = "",
    note: str = "",
    areas: range | None = None,
) -> dict:
    if context:
        # In the system message, so the prompt cache serves it to every stage-3 call after the first.
        system = f"{system}\n\n===== TABS ALREADY BUILT (cite these IDs) =====\n{context}"
    task = [
        f"Shred run date: {run_date}.",
        "Produce ONLY these tabs; return an empty array for every other tab in the schema:",
    ]
    for name in names:
        spec = TAB_BY_NAME[name]
        headers = [h for h, k in zip(spec.headers, spec.keys, strict=True) if k in _model_keys(name)]
        task.append(
            f"\n### {name} (IDs {spec.prefix}-NNN)\nPurpose: {spec.purpose}\nColumns: {', '.join(headers)}"
            f"\n{TAB_GUIDANCE.get(name, '')}"
        )
        if name == "Show Stoppers":
            picked = areas if areas is not None else range(len(SHOW_STOPPER_AREAS))
            task.append(
                (
                    "Fixed screening areas, in order:\n"
                    if areas is None
                    else "Return rows ONLY for these screening areas, in order:\n"
                )
                + "\n".join(f"{i + 1}. {SHOW_STOPPER_AREAS[i][0]} ({SHOW_STOPPER_AREAS[i][1]})" for i in picked)
            )
    if note:
        task.append(f"\n{note}")
    if opportunity:
        task.append("\nAlso return `opportunity`: issuing organisation (client), solicitation number, and title.")
    else:
        task.append("\nReturn empty strings in `opportunity`.")
    if excerpt:
        task.append(excerpt)

    return _request(client, config, system, "\n".join(task), SCHEMA_NAME, _SHARED_SCHEMA, ", ".join(names), usage)


def _request(
    client,
    config: AIConfig,
    system: str,
    user: str,
    schema_name: str,
    schema: dict,
    step: str,
    usage,
) -> dict:
    kwargs = {
        "model": config.model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        },
        "max_completion_tokens": 32_000 if config.model.startswith("gpt-4") else 100_000,
    }
    if config.model.startswith(("gpt-5", "gpt-6", "o")):
        kwargs["reasoning_effort"] = config.reasoning_effort
    response = client.chat.completions.create(**kwargs)
    # Record before the checks below: a cut-off or refused response is still billed.
    if usage is not None and response.usage is not None:
        usage.append(call_usage(config.model, step, response.usage))
    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise ShredAIError("response was cut off at the output-token limit")
    if getattr(choice.message, "refusal", None):
        raise ShredAIError(f"model refused: {choice.message.refusal}")
    return json.loads(choice.message.content)


_SHARED_SCHEMA = shared_schema()
