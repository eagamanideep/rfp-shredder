import io
from datetime import date
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

from doc_to_excel import extract_document, shred_ai
from doc_to_excel.shred import build_rule_shred
from doc_to_excel.shred_ai import AIConfig, build_ai_shred
from doc_to_excel.shred_excel import build_shred_workbook
from doc_to_excel.shred_schema import SHOW_STOPPER_AREAS, TAB_BY_NAME, TABS, column_key

RFP = """REQUEST FOR PROPOSALS
Network Switch Upgrades
RFP 10-2026-014
SECTION 1 – Instructions
Sealed bids will be received until January 5, 2026 at 2:00 p.m.
Questions may be submitted until December 3, 2025 at 2:00 p.m.
Bids must be signed in blue ink. The Non-Collusion Affidavit must be notarized.
The vendor must provide a W-9.
SECTION 2 – Terms
No right or interest in the contract shall be assigned without written permission.
The contractor shall maintain general liability insurance of $1,000,000.
"""
RUN = date(2026, 9, 20)


def rule_shred(**kwargs):
    result = extract_document(RFP.encode(), "rfp.txt")
    return result, build_rule_shred(result, "rfp.txt", RUN, **kwargs)


def test_rule_shred_fills_every_tab_with_ids():
    _, shred = rule_shred()
    for spec in TABS[1:]:
        rows = shred.tabs[spec.name]
        assert rows, spec.name
        assert rows[0]["id"] == f"{spec.prefix}-001"
    assert shred.rfp_number == "10-2026-014"
    assert [r["screening_area"] for r in shred.tabs["Show Stoppers"]] == [a for a, _ in SHOW_STOPPER_AREAS]


def test_rule_shred_dates_and_quick_turn():
    _, shred = rule_shred()
    dates = {r["date_type"]: r for r in shred.tabs["Key Dates"]}
    assert dates["Shred Run Date"]["id"] == "KD-001"
    due = dates["Proposal Due Date"]
    assert (due["date"], due["calendar_days_from_shred_run"], due["quick_turn"]) == ("Jan 05, 2026", "-258", "Past Due")
    assert dates["Questions Due"]["time_zone"] == "Not stated"
    quick = shred.tabs["Show Stoppers"][0]
    assert (quick["finding_status"], quick["risk_level"]) == ("Found", "Critical")
    assert "Proposal deadline: Past Due" in shred.opportunity_line()


def test_rule_shred_screening_and_forms():
    _, shred = rule_shred()
    stoppers = {r["screening_area"]: r for r in shred.tabs["Show Stoppers"]}
    assert stoppers["Wet signature / original / notarization"]["finding_status"] == "Found"
    assert stoppers["Bid bond / bid security"]["finding_status"] == "Not Found"
    forms = {r["form_attachment"] for r in shred.tabs["Forms + Attachments"]}
    assert {"Non-Collusion Affidavit", "W-9"} <= forms
    assert any(r["trigger_category"] == "Assignment" for r in shred.tabs["Contract Objections"])
    assert shred.tabs["Accessibility 508 ADA"][0]["standard"] == "Not stated"


def test_user_overrides_win():
    _, shred = rule_shred(client="Bartow County School System", title="Switch Upgrades")
    assert shred.title_suffix == "Bartow County School System | RFP 10-2026-014 Switch Upgrades"
    assert shred.client_overridden and not shred.rfp_overridden


def test_workbook_layout_matches_template():
    _, shred = rule_shred(client="Bartow County School System", title="Switch Upgrades")
    wb = load_workbook(io.BytesIO(build_shred_workbook(shred)))
    assert wb.sheetnames == [t.name for t in TABS]
    ws = wb["Show Stoppers"]
    assert ws["A1"].value == "Show Stoppers | Bartow County School System | RFP 10-2026-014 Switch Upgrades"
    assert ws["A2"].value == TAB_BY_NAME["Show Stoppers"].purpose
    assert [c.value for c in ws[5]][:5] == ["ID", "Screening Area", "Screening Type", "Finding Status", "Risk Level"]
    assert ws["A6"].value == "SS-001" and ws["E6"].fill.fgColor.rgb.endswith("B5479B")
    guide = wb["Workbook Guide"]
    assert guide["A3"].value.startswith("Opportunity: Bartow County School System")
    assert [guide.cell(row=r, column=4).value for r in (6, 17)] == ["WG-", "R-"]
    assert wb["Compliance Matrix"]["A5"].value == "Requirement ID"


def test_ai_shred_replaces_tabs_and_falls_back_per_group(monkeypatch):
    result, base = rule_shred()
    rule_issue_log = list(base.tabs["Q&A + Issue Log"])

    def fake_call(client, config, system, names, context, run_date, extra=None, usage=None):
        assert "PROCUREMENT DOCUMENT" in system and "Sealed bids" in system
        if "Show Stoppers" in names:
            raise RuntimeError("boom")
        out = {}
        for name in names:
            row = {k: f"{name} value" for k in TAB_BY_NAME[name].keys}
            out[column_key(name)] = [row, dict(row)]
        if extra:
            out["opportunity"] = {
                "client": "Bartow County School System",
                "rfp_number": "X",
                "title": "Switch Upgrades",
            }
            assert context == ""
        else:
            assert "R-001" in context  # later stages see the compliance matrix IDs
        return out

    monkeypatch.setattr(shred_ai, "_call", fake_call)
    progress = []
    shred = build_ai_shred(base, result, AIConfig("sk-test"), progress=lambda f, m: progress.append(f))

    assert shred.mode == "AI (gpt-5.6-sol)"
    assert shred.client == "Bartow County School System" and shred.rfp_number == "X"
    assert [r["id"] for r in shred.tabs["Compliance Matrix"]] == ["R-001", "R-002"]
    assert [r["id"] for r in shred.tabs["Key Dates"]] == ["KD-001", "KD-002", "KD-003"]
    assert shred.tabs["Key Dates"][0]["date_type"] == "Shred Run Date"
    assert shred.tabs["Scope Summary"][0]["scope_area"] == "Scope Summary value"
    # The failed stage-3 group keeps its rule-based rows and is reported.
    assert shred.tabs["Q&A + Issue Log"] == rule_issue_log
    assert any("Show Stoppers" in w for w in shred.warnings)
    assert len(shred.tabs["Show Stoppers"]) == len(SHOW_STOPPER_AREAS)
    assert progress[-1] == 1.0

    # Every step is timed, failed groups included.
    steps = [step for step, _ in shred.timings]
    assert steps[0] == "Rule-based shred (all tabs)"
    assert "AI stage 1 · Compliance Matrix, Key Dates" in steps
    assert "AI stage 2 · Scope Summary, Cost Breakdown, Client Objectives" in steps
    assert "AI stage 2 total (groups run in parallel)" in steps
    assert "AI stage 3 · Show Stoppers, Shred Readiness, Q&A + Issue Log" in steps
    assert all(seconds >= 0 for _, seconds in shred.timings)


def test_run_log_records_stage_times(tmp_path):
    from doc_to_excel.pipeline import generate_full_shred
    from doc_to_excel.run_log import load_runs, record_run

    result = extract_document(RFP.encode(), "rfp.txt")
    shred, workbook, total = generate_full_shred(result, ("Read & extract document (TXT)", 1.5), "rfp.txt", RUN)
    assert workbook[:2] == b"PK"  # a real .xlsx zip
    assert total >= 1.5
    assert [s for s, _ in shred.timings] == [
        "Read & extract document (TXT)",
        "Rule-based shred (all tabs)",
        "Build Excel workbook",
    ]

    log = tmp_path / "times.csv"
    record_run(shred, total, 1, path=log)
    shred.timings.append(("AI stage 2 · Scope Summary", 9.0))
    shred.timings.append(("AI stage 2 total (groups run in parallel)", 12.0))
    record_run(shred, total + 20, 1, model="gpt-6.1-sol", reasoning_effort="high", path=log)

    rule, ai = load_runs(log)
    assert (rule["mode"], rule["model"], rule["read_extract_s"], rule["ai_stage1_s"]) == ("Rule-based", "", "1.50", "")
    assert (ai["mode"], ai["model"], ai["reasoning_effort"]) == ("AI", "gpt-6.1-sol", "high")
    assert ai["ai_stage2_s"] == "12.00"  # the parallel stage's wall-clock total, not one group


def test_cost_uses_cached_and_long_context_rates():
    # gpt-5.6-sol: $4 input, $0.40 cached, $20 output per 1M; over 272K input: $8 / $0.80 / $30.
    assert shred_ai.cost_usd("gpt-5.6-sol", 100_000, 40_000, 10_000) == pytest.approx(0.24 + 0.016 + 0.2)
    assert shred_ai.cost_usd("gpt-5.6-sol", 300_000, 0, 10_000) == pytest.approx(2.4 + 0.3)
    assert shred_ai.cost_usd("gpt-5", 300_000, 0, 0) == pytest.approx(0.375)  # no long-context tier
    assert shred_ai.cost_usd("unknown-model", 1, 0, 1) is None
    assert set(shred_ai.PRICES) == set(shred_ai.MODELS)


def test_call_records_usage_even_when_response_is_cut_off():
    usage = SimpleNamespace(
        prompt_tokens=1_000,
        completion_tokens=500,
        prompt_tokens_details=SimpleNamespace(cached_tokens=200),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=300),
    )
    message = SimpleNamespace(content="{}", refusal=None)
    response = SimpleNamespace(usage=usage, choices=[SimpleNamespace(finish_reason="length", message=message)])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response)))

    recorded = []
    with pytest.raises(shred_ai.ShredAIError):
        shred_ai._call(client, AIConfig("sk-test"), "sys", ("Key Dates",), "", "2026-09-20", usage=recorded)
    assert recorded == [
        {
            "step": "Key Dates",
            "input_tokens": 1_000,
            "cached_input_tokens": 200,
            "output_tokens": 500,
            "reasoning_tokens": 300,
            "cost_usd": pytest.approx((800 * 4 + 200 * 0.4 + 500 * 20) / 1e6),
        }
    ]


def test_run_log_records_usage_cost_and_citations(tmp_path):
    from doc_to_excel.run_log import citation_stats, load_runs, record_run

    _, shred = rule_shred()
    for name in shred.tabs:
        shred.tabs[name] = []
    shred.tabs["Compliance Matrix"] = [
        {"source_location": "p. 2, Section 1"},
        {"source_location": "p. 9, Section 4"},  # page past the end of a 3-page document
        {"source_location": "Entire document reviewed"},
        {"source_location": ""},
    ]
    assert citation_stats(shred, 3) == {"rows": 4, "cited_rows": 3, "cited_pct": 75.0, "page_cited_pct": 25.0}
    assert citation_stats(shred, None)["page_cited_pct"] is None

    shred.usage = [
        {
            "step": "a",
            "input_tokens": 10,
            "cached_input_tokens": 4,
            "output_tokens": 5,
            "reasoning_tokens": 2,
            "cost_usd": 0.5,
        },
        {
            "step": "b",
            "input_tokens": 20,
            "cached_input_tokens": 0,
            "output_tokens": 7,
            "reasoning_tokens": 1,
            "cost_usd": 0.25,
        },
    ]
    log = tmp_path / "times.csv"
    log.write_text("timestamp,file\n2026-01-01T00:00:00,old.pdf\n")  # a log from before the new columns
    record_run(shred, 12.0, 3, model="gpt-5.6-sol", path=log)

    old, new = load_runs(log)
    assert old["file"] == "old.pdf" and old["cost_usd"] == ""
    assert (new["input_tokens"], new["cached_input_tokens"], new["output_tokens"]) == ("30", "4", "12")
    assert (new["cost_usd"], new["cited_rows"], new["cited_pct"], new["page_cited_pct"]) == (
        "0.7500",
        "3/4",
        "75.0",
        "25.0",
    )
