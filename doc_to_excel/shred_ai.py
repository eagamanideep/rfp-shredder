"""Fill the Full Shred with an OpenAI model, on top of the rule-based shred.

Three stages, each a set of structured-output calls that share the same system prompt (instructions + the full
document) so OpenAI's prompt cache serves the document after the first call:

1. Opportunity details, Compliance Matrix and Key Dates (later tabs cite their R-/KD- IDs).
2. The topical tabs, in parallel.
3. Show Stoppers, Shred Readiness and the Q&A + Issue Log, which summarise everything before them.

Any group that fails keeps its rule-based rows and adds a warning, so a run always produces a full workbook.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

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
- Source Location: "p. N, <section / heading>" (use the page markers). Source File: the file name given below.
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
    "weight it supports, if any. Amendment / Version: 'Baseline <RFP number>'. Include evaluation weights and "
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

STAGE2_GROUPS = (
    ("Scope Summary", "Cost Breakdown", "Client Objectives"),
    ("Proposal Outline", "Evaluation Summary", "Submission Mechanics", "Formatting + Limits"),
    (
        "Accessibility 508 ADA",
        "Contract Vehicle Eligibility",
        "Supplier Diversity Set-Aside",
        "Work Samples + References",
        "Forms + Attachments",
    ),
    ("Contract Objections", "Source Inventory", "Amendment + Delta Log"),
)
STAGE3_GROUP = ("Show Stoppers", "Shred Readiness", "Q&A + Issue Log")


class ShredAIError(RuntimeError):
    pass


@dataclass
class AIConfig:
    api_key: str
    model: str = DEFAULT_MODEL
    organization: str | None = None
    project: str | None = None
    reasoning_effort: str = "medium"


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
        timeout=900,
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

    def run(names: tuple[str, ...], context: str, extra: dict | None = None) -> dict:
        return _call(client, config, system, names, context, shred.run_date.isoformat(), extra, usage=shred.usage)

    report(0.05, "Stage 1/3: requirements, key dates and opportunity details…")
    opportunity = {
        "type": "object",
        "properties": {k: {"type": "string"} for k in ("client", "rfp_number", "title")},
        "required": ["client", "rfp_number", "title"],
        "additionalProperties": False,
    }
    try:
        with shred.timed("AI stage 1 · Compliance Matrix, Key Dates"):
            data = run(("Compliance Matrix", "Key Dates"), "", {"opportunity": opportunity})
        opp = data["opportunity"]
        shred.client = shred.client if base.client_overridden else (opp["client"] or shred.client)
        shred.rfp_number = shred.rfp_number if base.rfp_overridden else (opp["rfp_number"] or shred.rfp_number)
        shred.title = shred.title if base.title_overridden else (opp["title"] or shred.title)
        shred.tabs["Compliance Matrix"] = data["compliance_matrix"]
        shred.tabs["Key Dates"] = shred.tabs["Key Dates"][:1] + data["key_dates"]
        assign_ids(shred.tabs, ["Compliance Matrix", "Key Dates"])
    except Exception as exc:  # noqa: BLE001 - keep the rule-based rows and report the failure
        shred.warnings.append(f"AI stage 1 failed ({exc}); requirements and dates are rule-based.")

    context = _summary(shred, ("Compliance Matrix", "Key Dates"))
    report(0.35, "Stage 2/3: scope, pricing, outline, forms, contract screening…")
    _run_groups(shred, "AI stage 2", STAGE2_GROUPS, lambda names: run(names, context))

    report(0.75, "Stage 3/3: show stoppers, readiness and issue log…")
    context = _summary(shred, [n for n in shred.tabs if n not in STAGE3_GROUP])
    _run_groups(shred, "AI stage 3", (STAGE3_GROUP,), lambda names: run(names, context))
    _pin_show_stoppers(shred)

    readiness = next(iter(shred.tabs.get("Shred Readiness", [])), {})
    if readiness:
        shred.banners["Shred Readiness"] = f"{readiness.get('status')}: {readiness.get('issue_limitation', '')}"
        shred.guide_notes["Shred Readiness"] = shred.banners["Shred Readiness"]
    report(1.0, "Done")
    return shred


def _run_groups(shred: Shred, stage: str, groups, call) -> None:
    def timed_call(names):
        start = time.perf_counter()
        try:
            return call(names), None, time.perf_counter() - start
        except Exception as exc:  # noqa: BLE001 - keep the rule-based rows and report the failure
            return None, exc, time.perf_counter() - start

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(groups)) as pool:
        futures = {names: pool.submit(timed_call, names) for names in groups}
    elapsed = time.perf_counter() - start
    for names, future in futures.items():
        data, exc, seconds = future.result()
        shred.timings.append((f"{stage} · {', '.join(names)}", seconds))
        if exc is not None:
            shred.warnings.append(f"AI failed for {', '.join(names)} ({exc}); those tabs are rule-based.")
            continue
        for name in names:
            shred.tabs[name] = data[column_key(name)]
        assign_ids(shred.tabs, list(names))
    if len(groups) > 1:
        shred.timings.append((f"{stage} total (groups run in parallel)", elapsed))


def _pin_show_stoppers(shred: Shred) -> None:
    """Show Stoppers has fixed rows: force the area names/order whatever the model returned."""
    rows = shred.tabs.get("Show Stoppers", [])
    by_area = {r.get("screening_area", "").lower(): r for r in rows}
    pinned = []
    for i, (area, kind) in enumerate(SHOW_STOPPER_AREAS):
        row = by_area.get(area.lower()) or (rows[i] if i < len(rows) else {"finding_status": "Unclear"})
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


def _tab_schema(name: str) -> dict:
    keys = list(TAB_BY_NAME[name].keys)
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


def _call(
    client, config: AIConfig, system: str, names, context: str, run_date: str, extra=None, usage: list | None = None
) -> dict:
    properties = {column_key(n): _tab_schema(n) for n in names}
    properties.update(extra or {})
    schema = {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}

    task = [f"Shred run date: {run_date}.", "Produce these tabs:"]
    for name in names:
        spec = TAB_BY_NAME[name]
        task.append(
            f"\n### {name} (IDs {spec.prefix}-NNN)\nPurpose: {spec.purpose}\nColumns: {', '.join(spec.headers)}"
            f"\n{TAB_GUIDANCE.get(name, '')}"
        )
        if name == "Show Stoppers":
            task.append(
                "Fixed screening areas, in order:\n"
                + "\n".join(f"{i}. {a} ({t})" for i, (a, t) in enumerate(SHOW_STOPPER_AREAS, start=1))
            )
    if extra and "opportunity" in extra:
        task.append("\nAlso return `opportunity`: issuing organisation (client), solicitation number, and title.")
    if context:
        task.append(f"\n===== TABS ALREADY BUILT (cite these IDs) =====\n{context}")

    kwargs = {
        "model": config.model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(task)}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": column_key("_".join(names))[:60], "strict": True, "schema": schema},
        },
        "max_completion_tokens": 32_000 if config.model.startswith("gpt-4") else 100_000,
    }
    if config.model.startswith(("gpt-5", "gpt-6", "o")):
        kwargs["reasoning_effort"] = config.reasoning_effort
    response = client.chat.completions.create(**kwargs)
    # Record before the checks below: a cut-off or refused response is still billed.
    if usage is not None and response.usage is not None:
        usage.append(call_usage(config.model, ", ".join(names), response.usage))
    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise ShredAIError("response was cut off at the output-token limit")
    if getattr(choice.message, "refusal", None):
        raise ShredAIError(f"model refused: {choice.message.refusal}")
    return json.loads(choice.message.content)
