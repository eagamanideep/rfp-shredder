"""Layout of the "Full Shred" workbook: tabs, ID prefixes, columns and controlled values."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

AUDIENCE = "Internal proposal / pursuit team"


def column_key(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", header.lower()).strip("_")


@dataclass(frozen=True)
class TabSpec:
    name: str
    prefix: str
    purpose: str
    banner: str  # default third-row note; builders may override it per run
    headers: tuple[str, ...]  # excluding the ID column
    guide_notes: str = ""
    id_header: str = "ID"
    keys: tuple[str, ...] = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, "keys", tuple(column_key(h) for h in self.headers))


TABS: tuple[TabSpec, ...] = (
    TabSpec(
        "Workbook Guide",
        "WG",
        "Navigation, controlled-value guidance, and executive orientation for the Full Shred.",
        "",
        ("Tab", "Purpose", "Primary Use / Audience", "ID Prefix", "Key Controlled Values / Notes"),
        id_header="",
    ),
    TabSpec(
        "Show Stoppers",
        "SS",
        "Fixed pre-pursuit screening areas for human go/no-go discussion; no pursue/no-pursue decision is made here.",
        "Human decision-input screen only. Required screening rows are fixed; Finding Status and Risk Level are "
        "separate. Past-due status is based on the shred run date.",
        (
            "Screening Area",
            "Screening Type",
            "Finding Status",
            "Risk Level",
            "Decision Relevance",
            "Finding / Requirement",
            "Key Value / Threshold",
            "Required Stage",
            "Source File",
            "Source Location",
            "Related Requirement IDs",
            "Next Action",
        ),
        "Finding Status: Found / Not Found / Not Applicable / Unclear. Risk Level: Critical / High / Moderate / "
        "Watch / Informational / None Identified / Not Applicable.",
    ),
    TabSpec(
        "Shred Readiness",
        "SR",
        "Shows source completeness, review status, and limitations that affect confidence in the shred.",
        "",
        (
            "Readiness Area",
            "Status",
            "Source File / Dependency",
            "Issue / Limitation",
            "Impact on Findings",
            "Affected Tabs / IDs",
            "Mitigation / Next Step",
        ),
    ),
    TabSpec(
        "Key Dates",
        "KD",
        "Tracks procurement deadlines, shred run date, days remaining, and Quick Turn status.",
        "Calendar Days from Shred Run uses calendar dates. Quick Turn is Yes at 0-7 days, No above 7, and Past Due "
        "when the deadline is before the shred run date.",
        (
            "Date Type",
            "Date",
            "Time",
            "Time Zone",
            "Mandatory?",
            "Calendar Days from Shred Run",
            "Quick Turn?",
            "Source File",
            "Source Location",
            "Notes",
        ),
        "Quick Turn = 7 or fewer calendar days.",
    ),
    TabSpec(
        "Source Inventory",
        "SI",
        "Inventories procurement sources, referenced-but-missing sources, and the internal objection matrix used "
        "for legal-trigger screening.",
        "Procurement authority and internal trigger sources are separated. Missing referenced/expected sources are "
        "visible rather than silently omitted.",
        (
            "File / Source Name",
            "Source Type",
            "Procurement Authority?",
            "Reviewed?",
            "Readability / Access",
            "Version / Date",
            "Superseded?",
            "Use Scope",
            "Notes",
        ),
    ),
    TabSpec(
        "Amendment + Delta Log",
        "AD",
        "Tracks addenda, Q&A changes, user updates, and source-set change limitations while preserving stable IDs.",
        "Baseline IDs are stable. Deltas are recorded only from supplied amendment text.",
        (
            "Change Source",
            "Effective Date",
            "Change Type",
            "Previous Rule / Value",
            "New Rule / Value",
            "Affected IDs",
            "Affected Tabs / Workstreams",
            "Action Required",
            "Source Location",
            "Notes",
        ),
    ),
    TabSpec(
        "Scope Summary",
        "SC",
        "Summarizes product, configuration, installation, removal, quantities, location, and "
        "manufacturer/equivalency requirements.",
        "",
        (
            "Scope Area",
            "Requirement / Description",
            "Delivery Mix",
            "Quantity",
            "Unit",
            "Location / Site",
            "Manufacturer / Partner",
            "Cabling / Installation?",
            "Mandatory?",
            "Source File",
            "Source Location",
            "Notes",
        ),
    ),
    TabSpec(
        "Cost Breakdown",
        "CB",
        "Isolates pricing elements, quantity bases, pricing format, and budget/ceiling status.",
        "",
        (
            "Cost / Pricing Element",
            "Quantity",
            "Unit",
            "Pricing Basis",
            "Budget / Ceiling / NTE",
            "Separate Cost File?",
            "Required Format",
            "Reimbursables / Travel",
            "Source File",
            "Source Location",
            "Notes",
        ),
    ),
    TabSpec(
        "Client Objectives",
        "CO",
        "Captures source-stated objectives and desired outcomes without proposal strategy.",
        "Source-grounded goals only. This tab does not create win themes, differentiators, or proposal strategy.",
        (
            "Objective / Pressure",
            "Desired Future State",
            "Beneficiary / Stakeholder",
            "Evidence / Success Signal",
            "Implication for Response",
            "Source File",
            "Source Location",
            "Confidence",
            "Notes",
        ),
    ),
    TabSpec(
        "Proposal Outline",
        "PO",
        "Maps required response items and separate artifacts without inventing proposal narrative structure.",
        "Separates narrative response items from forms, pricing, and submission artifacts. Evaluation-factor headings "
        "are not treated as client-mandated section titles unless the RFP says so.",
        (
            "TOC Level",
            "Structural Role",
            "RFP Heading / Item",
            "Clean Proposal Heading",
            "Parent ID",
            "Requirement IDs",
            "Required?",
            "Narrative / Artifact",
            "Source File",
            "Source Location",
            "Notes",
        ),
    ),
    TabSpec(
        "Evaluation Summary",
        "EV",
        "Preserves stated evaluation factors and exact weights/points.",
        "Exact stated weights/points only. No unstated scoring formula is added.",
        (
            "Criterion / Factor",
            "Weight / Points",
            "Subfactor",
            "What Evaluator Will Score",
            "Required Evidence / Proof",
            "Related Requirement IDs",
            "Source File",
            "Source Location",
            "Notes",
        ),
    ),
    TabSpec(
        "Compliance Matrix",
        "R",
        "Standalone requirement baseline with stable Requirement IDs and source traceability.",
        "Standalone export-ready requirement baseline. Source locations are specific and Requirement IDs remain "
        "stable through future updates.",
        (
            "Requirement Force",
            "Requirement Type",
            "Requirement Text",
            "Response / Artifact Needed",
            "Proposal / Package Home",
            "Workstream",
            "Evaluation Link",
            "Source File",
            "Source Location",
            "Amendment / Version",
            "Related IDs",
            "Notes",
        ),
        "Standalone export-ready requirement baseline; Requirement Force = Mandatory / Conditional / Advisory / "
        "Informational.",
        id_header="Requirement ID",
    ),
    TabSpec(
        "Contract Objections",
        "CT",
        "Records only clear matches to the current Contracts Proposal Objection Matrix; this is internal "
        "screening, not legal advice.",
        "Internal screening against the current Contracts Proposal Objection Matrix only. Clear matches are shown; "
        "no legal advice or redline language is provided.",
        (
            "Section / Clause",
            "Clause Summary",
            "Triggering Language (Short Quote)",
            "Trigger Category",
            "Matrix Basis / Threshold",
            "Client Restriction",
            "Matrix Escalation",
            "Proposal Risk Level",
            "Required Internal Action",
            "Proposal Impact",
            "Source File",
            "Source Location",
            "Related Requirement IDs",
            "Confidence",
            "Notes",
        ),
        "Only clear matches to the current internal matrix are included; client restriction is tracked separately.",
    ),
    TabSpec(
        "Submission Mechanics",
        "SM",
        "Controls physical delivery, package composition, signatures, copies, deadlines, and post-award "
        "submission steps.",
        "Submission controls are separated from proposal content.",
        (
            "Control",
            "Requirement",
            "Required Stage",
            "Submission Method",
            "File / Package",
            "Copies / Originals",
            "Signature Type",
            "Portal / Delivery Details",
            "Source File",
            "Source Location",
            "Related Requirement IDs",
            "Notes",
        ),
    ),
    TabSpec(
        "Formatting + Limits",
        "FL",
        "Captures explicit response-format rules and visible absences such as no stated page limit.",
        "Explicit response-format controls only.",
        (
            "Applies To",
            "Rule Type",
            "Requirement",
            "Limit / Value",
            "Consequence",
            "Source File",
            "Source Location",
            "Related Requirement IDs",
            "Notes",
        ),
    ),
    TabSpec(
        "Accessibility 508 ADA",
        "AC",
        "Captures any Section 508, ADA, or accessibility requirements, including explicit Not Found results.",
        "",
        (
            "Requirement",
            "Standard",
            "Applies To",
            "Proposal Stage?",
            "Deliverable Stage?",
            "Evidence / Artifact",
            "Source File",
            "Source Location",
            "Related Requirement IDs",
            "Notes",
        ),
    ),
    TabSpec(
        "Contract Vehicle Eligibility",
        "CE",
        "Captures E-Rate/SPIN program eligibility and any contract-vehicle or approved-pool requirements.",
        "",
        (
            "Eligibility Type",
            "Requirement",
            "Applies To",
            "Mandatory?",
            "Evidence Required",
            "Required Stage",
            "Source File",
            "Source Location",
            "Related Requirement IDs",
            "Notes",
        ),
    ),
    TabSpec(
        "Supplier Diversity Set-Aside",
        "SD",
        "Captures supplier-diversity set-asides, goals, certifications, or participation rules.",
        "",
        (
            "Program / Requirement",
            "Type",
            "Mandatory / Goal",
            "Percentage / Threshold",
            "Certification / Evidence",
            "Subcontracting Requirement",
            "Source File",
            "Source Location",
            "Related Requirement IDs",
            "Notes",
        ),
    ),
    TabSpec(
        "Work Samples + References",
        "WR",
        "Captures references, similar-scope proof, and work-sample requirements tied to evaluation.",
        "",
        (
            "Item Type",
            "Requirement",
            "Quantity",
            "Recency / Similarity",
            "Required Fields / Format",
            "Approval / Permission Need",
            "Proposal Home",
            "Source File",
            "Source Location",
            "Related Requirement IDs",
            "Notes",
        ),
    ),
    TabSpec(
        "Forms + Attachments",
        "FA",
        "Lists forms, signatures, notarizations, corporate seal, W-9, affidavits, and separate artifacts.",
        "",
        (
            "Form / Attachment",
            "Required?",
            "Required Stage",
            "Signature / Notary",
            "Who Completes",
            "Separate Artifact?",
            "File Provided?",
            "Source File",
            "Source Location",
            "Related Requirement IDs",
            "Notes",
        ),
    ),
    TabSpec(
        "Q&A + Issue Log",
        "QI",
        "Centralizes contradictions, missing sources, ambiguities, and decision-owner questions.",
        "Open contradictions and source gaps are centralized here. Resolving an issue may require affected-tab "
        "refresh while preserving stable IDs.",
        (
            "Issue Type",
            "Question / Conflict",
            "Severity",
            "Status",
            "Decision / Owner Needed",
            "Impact",
            "Source File",
            "Source Location",
            "Related IDs",
            "Resolution / Notes",
        ),
    ),
)

TAB_BY_NAME = {t.name: t for t in TABS}

# Fixed Show Stoppers screening rows: (Screening Area, Screening Type).
SHOW_STOPPER_AREAS: tuple[tuple[str, str], ...] = (
    ("Quick Turn", "Timing Risk"),
    ("Mandatory pre-bid / site visit / LOI", "Eligibility Gate"),
    ("Wet signature / original / notarization", "Submission Risk"),
    ("Hard-copy / physical delivery", "Submission Risk"),
    ("Set-aside / prime eligibility", "Eligibility Gate"),
    ("Contract vehicle / approved pool", "Eligibility Gate"),
    ("License / certification", "Eligibility Gate"),
    ("Business registration / authority to do business", "Eligibility Gate"),
    ("Manufacturer / OEM / partner authorization", "Eligibility Gate"),
    ("Local presence / geography", "Delivery Risk"),
    ("Supplier diversity", "Eligibility Gate"),
    ("Security / background / hosting / data residency", "Eligibility Gate"),
    ("Conflict / OCI / independence", "Legal/Contract Risk"),
    ("Bid bond / bid security", "Financial Gate"),
    ("Performance / payment bond / bonding capacity", "Financial Gate"),
    ("Financial statements / qualification", "Financial Gate"),
    ("Insurance", "Financial Gate"),
    ("Budget / ceiling / NTE", "Financial Gate"),
    ("Contract exceptions / redlines / term acceptance", "Legal/Contract Risk"),
    ("Contracts objection triggers", "Legal/Contract Risk"),
    ("Strict staffing / qualifications", "Delivery Risk"),
    ("Delivery mix", "Scope/Profile"),
    ("Cabling / field installation / site work", "Delivery Risk"),
    ("Hardware requirements / quantities", "Scope/Profile"),
    ("Customer location", "Informational"),
    ("Customer profile / scale", "Informational"),
)

FINDING_STATUS = ("Found", "Not Found", "Not Applicable", "Unclear")
RISK_LEVELS = ("Critical", "High", "Moderate", "Watch", "Informational", "None Identified", "Not Applicable")
READINESS_STATUS = ("Complete", "Partial", "Reviewed", "Complete with Limits", "Missing Referenced Source")
REQUIREMENT_FORCE = ("Mandatory", "Conditional", "Advisory", "Informational")
