# RFP Shredder

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
  contradictions between pages, missing referenced sources, and cross-referenced IDs. Uses `gpt-5.6-sol`
  (default) or another GPT-6 / GPT-5.x model selectable in the sidebar, in two phases of parallel
  structured-output calls. Stages 1 and 2 run at the same time, then stage 3 and the ID links:
  1. **Compliance Matrix, Key Dates and the screening tabs** (Accessibility, Contract Vehicle, Supplier
     Diversity), extracted from section-aligned page chunks (a Word or text document, which has no pages, is cut
     into runs of sections instead, and its tables form a chunk of their own). Every chunk sees the document outline, the
     evaluation factors and the pages they come from (the scoring rubric), so requirements carry their Evaluation
     Link. The rows are merged in page order without repeats, each requirement's text is checked against the page it
     cites (the page is corrected, or the row is marked *Unverified* in Notes), and dates are normalised. Each
     Unverified requirement gets a second look from `gpt-5-nano`, which places it on one of the nearby pages or finds
     it on none; the row is then dropped and quoted in the warnings. A requirement whose wording suggests a different
     Requirement Force ("may" on a Mandatory row, "shall" on an Advisory one) gets a *Force check* note; the label
     is left as the model set it. A screening tab with no match in any chunk keeps the rule-based Not Found row.
  2. **The topical tabs**, one call per tab over the whole document.
  3. **Show Stoppers** (in three parts of the fixed screening areas), **Shred Readiness** and the **Q&A + Issue
     Log** (one call for contradictions and missing sources, one for other open questions), summarising
     everything before them. Alongside them, a short link call per topical tab fills its requirement-ID column
     from the finished Compliance Matrix (only IDs that exist are kept), and requirements are linked in code to
     the key dates they state and the forms they name.

  A call that runs more than twice as long as the finished calls of its kind (and over 30 s) is sent again, and
  whichever copy answers first is used, so one stalled request does not hold up its phase.

  Source File and Amendment / Version are the same on every row, so they are filled in code rather than written
  by the model. Each model has a recommended reasoning effort and chunk size (`MODEL_SETTINGS` in
  [shred_ai.py](doc_to_excel/shred_ai.py); `gpt-5-nano` uses low effort and smaller chunks). If a call fails or
  returns an empty tab, that tab (or those pages) keeps the rule-based rows and the app shows a warning.
- **Rule-based** (no key, offline) — fills every tab by pattern matching: requirements, dates, Quick Turn, forms,
  submission rules, keyword-screened show stoppers and contract clauses. Judgement columns are marked for human review.

### Models

The sidebar offers these OpenAI models. All support Chat Completions with strict structured outputs and
`reasoning_effort` (the app uses low / medium / high). Prices are standard tier, USD per 1M tokens, from the
[OpenAI pricing page](https://developers.openai.com/api/docs/pricing) as of Oct 21, 2026. Cost figures in the app
use the same prices ([`PRICES` in shred_ai.py](doc_to_excel/shred_ai.py)), so update both together.

| Model | Input | Cached input | Output | Long context (>272K input): in / cached / out | Context window | Max output | Knowledge cutoff |
|---|---|---|---|---|---|---|---|
| `gpt-6.1-sol` | $2.00 | $0.10 | $10.00 | $4.00 / $0.20 / $15.00 | 1.05M | 128K | Apr 30, 2026 |
| `gpt-6-sol` | $2.00 | $0.20 | $10.00 | $4.00 / $0.40 / $15.00 | 1.05M | 128K | Apr 20, 2026 |
| `gpt-6-luna` | $0.10 | $0.01 | $0.50 | $0.20 / $0.02 / $0.75 | 1.05M | 128K | May 18, 2026 |
| `gpt-6-astra` | $10.00 | $1.00 | $50.00 | $20.00 / $2.00 / $75.00 | 1.05M | 128K | Apr 30, 2026 |
| `gpt-5.6-sol` (default) | $4.00 | $0.40 | $20.00 | $8.00 / $0.80 / $30.00 | 1.05M | 128K | Feb 16, 2026 |
| `gpt-5.6-luna` | $0.20 | $0.02 | $1.20 | $0.40 / $0.04 / $1.80 | 1.05M | 128K | Feb 16, 2026 |
| `gpt-5.6-terra` | $2.00 | $0.20 | $12.00 | $4.00 / $0.40 / $18.00 | 1.05M | 128K | Feb 16, 2026 |
| `gpt-5.5` | $5.00 | $0.50 | $30.00 | $10.00 / $1.00 / $45.00 | 1.05M | 128K | Dec 1, 2025 |
| `gpt-5.4` | $2.50 | $0.25 | $15.00 | $5.00 / $0.50 / $22.50 | 1.05M | 128K | Aug 31, 2025 |
| `gpt-5.4-mini` | $0.75 | $0.075 | $4.50 | — | 400K | 128K | Aug 31, 2025 |
| `gpt-5.4-nano` | $0.20 | $0.02 | $1.25 | — | 400K (272K max input) | 128K | Aug 31, 2025 |
| `gpt-5.2` | $1.75 | $0.175 | $14.00 | — | 400K | 128K | Aug 31, 2025 |
| `gpt-5.1` | $1.25 | $0.125 | $10.00 | — | 400K | 128K | Sep 30, 2024 |
| `gpt-5` | $1.25 | $0.125 | $10.00 | — | 400K (272K max input) | 128K | Sep 30, 2024 |
| `gpt-5-mini` | $0.25 | $0.025 | $2.00 | — | 400K | 128K | May 31, 2024 |
| `gpt-5-nano` | $0.05 | $0.005 | $0.40 | — | 400K | 128K | May 31, 2024 |

Reasoning tokens are billed as output. Every call uses the same response schema and, within a stage, the same
system prompt, so OpenAI's prompt cache serves the repeated part (a different schema per call would defeat it). The
cache reuses only the system message, not the start of the user message, so text that several calls share (the
document, stage 3's summary of the finished tabs) goes in the system message. In benchmarks 70–80% of all input
tokens were billed at the cached rate; most of the rest are the page excerpts, which every chunk call has its own of.

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

### Extraction times, cost and citations

Every Full Shred run (app or benchmark) is appended to `runs/extraction_times.csv` (git-ignored) with:

- the time for each step: read & extract, rule-based shred, AI stages 1–3, Excel build, and the total;
- AI token usage summed over all model calls: `input_tokens` (of which `cached_input_tokens`), `output_tokens`
  (of which `reasoning_tokens`), and `cost_usd` at the prices in the model table;
- citation rate over every row of the tabs that have a Source Location column: `cited_rows` (cited / total),
  `cited_pct` (rows with a non-empty Source Location) and `page_cited_pct` (rows citing a "p. N" page that exists
  in the PDF; blank for other file types). These count whether a row says where it came from, not whether the
  cited text says what the row claims. A tab that falls back to rule-based rows after an AI failure counts its
  rule-based rows.

The app shows these for the current run, a per-call token breakdown, and the history of recorded runs.

To compare models on one document:

```bash
python benchmark.py "RFP.pdf" --models gpt-6-luna gpt-5-nano
```

Models run one after another so their timings don't compete; each workbook is saved to `runs/`, and the tokens,
cost and citation rate are printed per model. `--effort` and `--chunk-chars` override the model's recommended
settings (`--chunk-chars 0` reads the document whole). Use `rule` as a model name to time the rule-based pipeline.

Further speed, cost and quality work is listed in [docs/optimisation-backlog.md](docs/optimisation-backlog.md).

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
