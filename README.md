# SSO integration

## RFP Shred — document to Excel

A Streamlit app that turns a PDF, Word (`.docx`), `.txt` or `.md` procurement document (RFP, RFQ, IFB, requirements
spec, contract) into a **Full Shred** Excel workbook for the proposal / pursuit team.

### Full Shred workbook (21 tabs)

Every tab has the same layout: title bar (`Tab | Client | RFP # Title`), purpose line, run note, then a table whose
rows carry a stable ID and trace back to source file, page and section.

| Tab | ID | Tab | ID |
|---|---|---|---|
| Workbook Guide | WG- | Evaluation Summary | EV- |
| Show Stoppers (26 fixed screening areas) | SS- | Compliance Matrix | R- |
| Shred Readiness | SR- | Contract Objections | CT- |
| Key Dates (days from run date, Quick Turn) | KD- | Submission Mechanics | SM- |
| Source Inventory | SI- | Formatting + Limits | FL- |
| Amendment + Delta Log | AD- | Accessibility 508 ADA | AC- |
| Scope Summary | SC- | Contract Vehicle Eligibility | CE- |
| Cost Breakdown | CB- | Supplier Diversity Set-Aside | SD- |
| Client Objectives | CO- | Work Samples + References | WR- |
| Proposal Outline | PO- | Forms + Attachments | FA- |
| | | Q&A + Issue Log | QI- |

Two analysis modes:

- **AI (OpenAI)** — fills every tab, including the judgement columns: risk levels, decision relevance,
  contradictions between pages, missing referenced sources, and cross-referenced IDs. Runs three stages of
  structured-output calls with `gpt-5.6-sol` (default) or `gpt-6.1-sol`, selectable in the sidebar. Lower the
  reasoning effort to trade depth for speed. If one group of tabs fails, those tabs fall back to the
  rule-based rows and the app shows a warning.
- **Rule-based** (no key, offline) — fills every tab by pattern matching: requirements, dates, Quick Turn, forms,
  submission rules, keyword-screened show stoppers and contract clauses. Judgement columns are marked for human review.

**Contract Objections** are screened only against your internal *Contracts Proposal Objection Matrix* — upload it
(`.xlsx`) in the sidebar. Without it, the tab says the terms were not screened.

A **Simple extract** option (Summary / Requirements / Sections / Tables / Pages) is still available in the sidebar.

### Setup

```bash
python -m pip install -r requirements.txt
```

For AI mode, put the OpenAI credentials in `.streamlit/secrets.toml` (git-ignored — never commit it):

```toml
OPENAI_API_KEY = "sk-..."
OPENAI_ORG_ID = "org-..."        # optional
OPENAI_PROJECT_ID = "proj_..."   # optional
```

or set them as environment variables, or paste a key into the sidebar for one session.

### Run

```bash
python -m streamlit run app.py
```

Open http://localhost:8501, upload the document, optionally fill in client / RFP number / title and the objection
matrix, then click **Generate Full Shred** and **Download Full Shred workbook**.

### Extraction times

Every Full Shred run (app or benchmark) is appended to `runs/extraction_times.csv` (git-ignored) with the time for
each step: read & extract, rule-based shred, AI stages 1–3, Excel build, and the total. The app shows the breakdown
for the current run and the history of recorded runs.

To compare models on one document:

```bash
python benchmark.py "RFP.pdf" --models gpt-5.6-sol gpt-6.1-sol --effort medium
```

Models run one after another so their timings don't compete; each workbook is saved to `runs/`. Use `rule` as a
model name to time the rule-based pipeline.

### Tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

The tests stub the OpenAI call, so they run offline and cost nothing.

### Limitations

- Scanned PDFs (no text layer) are not supported — there is no OCR.
- AI mode sends the document text (and the objection matrix, if uploaded) to OpenAI.
- Rule-based mode cannot judge risk or spot contradictions; treat its Show Stoppers and Contract Objections as
  candidates for review.
- Legacy `.doc` files must be saved as `.docx` first.
