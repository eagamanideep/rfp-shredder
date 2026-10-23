# Full Shred optimisation backlog

Further optimisations for the AI Full Shred, after the five optimisation steps that brought `gpt-6-luna` from 487 s
to 117–128 s on `rfp-1.pdf` (31 pages). Each item lists its expected effect, its risk to quality, where it goes in the
code, and how to check it. The numbers come from benchmarks on that one RFP (Oct 21–22, 2026), so treat the
estimates as starting points.

**Status (Oct 23, 2026):** items 1, 3 and 11 are done; see [Done](#done) for what was measured. Item 1 turned out
not to pay, but measuring it found a larger cache fix in stage 3. Items 8 and 9 are done, and item 7 was tried and
removed; see [Quality checks](#quality-checks-7-8-9). The evaluation script is in the repo as `evaluate.py`.

## Where the time and money go now

A run lasts as long as its slowest call in each of two phases (see the module docstring in
[shred_ai.py](../doc_to_excel/shred_ai.py)): phase A (stages 1 and 2) took 65–74 s, phase B (stage 3 and the link
calls) 49–52 s. Most of the cost is output, not input:

| Part of the cost (last 9,000-character `gpt-6-luna` run, $0.094) | Tokens | Cost | Share |
| --- | --- | --- | --- |
| Output | 142K | $0.071 | 76% |
| Uncached input | 157K | $0.016 | 17% |
| Cached input | 683K | $0.007 | 7% |

So shorter output saves the most time and money. Sending less input saves little, because about 80% of input is
already cached.

## Recommended order

1. ~~**No risk to quality:** cache warm-up (1), straggler resend (3), chunking for Word documents (11).~~ Done.
2. ~~**Quality:** scoring rubric in chunk calls (7), second look at Unverified rows (8).~~ 8 and the Force check (9)
   done; 7 removed, since it lowered grounding.
3. **Needs careful measuring:** shorter cells (2).
4. **Needs a person:** the hand-checked answer key (10).

## Speed and cost

| # | Item | Expected effect | Risk to quality |
| --- | --- | --- | --- |
| 1 | Warm the prompt cache before fanning out | Up to ~$0.015 a run (~15%); slightly faster first tokens | None |
| 2 | Shorter cells; more columns computed in code | 20–30% less output, so about that much less time and cost | Some detail lost |
| 3 | Resend straggler calls | Trims the slowest 10–30 s per phase; guards against stalls | None; a few extra calls |
| 4 | Priority processing tier | Lower latency on every call | None; higher price per token |
| 5 | Batch API for bulk runs | 50% off benchmarks and overnight backlogs | None; up to 24 h turnaround, not for the app |
| 6 | Reuse model results on re-runs | Re-runs with changed options in seconds, at no cost | None |

**1. Cache warm-up.** All ~45 phase-A calls start at the same moment, before any cache entry exists, so many pay the
uncached rate for the same opening. Send one call per distinct prompt opening first (one for the chunk system
prompt, one for the whole-document system prompt), with `max_completion_tokens` of a few tokens. Fan out the rest
once those return, about 2 s later.
*Where:* `build_ai_shred` in `shred_ai.py`, before the phase-A `ThreadPoolExecutor`.
*Check:* `cached_input_tokens` / `input_tokens` in `runs/extraction_times.csv` should rise from 73–81% towards 95%.

**2. Shorter cells.** Set word limits in `TAB_GUIDANCE` for the long free-text columns (Decision Relevance, Notes,
Impact, Response / Artifact Needed, Implication for Response). Compute in code what follows from other columns:
Quick Turn from the proposal due date and the run date, and Mandatory? from Requirement Force. Add those columns to
`CODE_FILLED_BY_TAB` so they leave the schema.
*Check:* output tokens per run, plus the full evaluation. A reviewer should also compare a few rows before and after,
since the evaluation does not measure how useful a cell is.

**3. Straggler resend.** Stage-1 chunk calls ranged from 16 s to 80 s in one run. In the `gpt-5-nano` step-5 run,
every stage-3 call stalled until the old 900 s timeout. Track each phase's finished calls; when a call runs past
about 2× the median of its finished peers, start a second copy and use whichever finishes first.
*Where:* the `ThreadPoolExecutor` loops in `_run_stage1`, `_run_groups` and `_run_stage3`. A shared helper could
replace the three `timed_call` closures.
*Check:* the spread of per-call times in the benchmark output; extra calls per run (expect 1–3).

**4. Priority tier.** OpenAI's priority processing (`service_tier="priority"` on the request) trades a higher price
for lower latency. Check that the account and these models support it, then make it an `AIConfig` option.
*Check:* run time and cost in two runs with and two without it.

**5. Batch API.** For benchmarks and bulk processing, submit the calls through the Batch API at half price. This
means a separate path in `benchmark.py`, because the pipeline's phases depend on each other's results: submit
phase A, wait, then submit phase B.

**6. Reuse results.** The app re-runs every model call when the client name, RFP number, title or run date
changes, though only stage 3 and the date columns depend on them. Cache the phase-A results per document hash and
model, and offer "re-run failed tabs only" when a run ends with warnings.
*Where:* `app.py` (`run_key`) and `pipeline.generate_full_shred`.

## Quality

| # | Item | Expected effect |
| --- | --- | --- |
| 7 | Scoring rubric in chunk calls, not just factor names | Recover Evaluation Link on proposal-content pages (36% now, 100% before chunking) |
| 8 | Second look at Unverified rows | Grounding above 96%, for under a cent per run |
| 9 | Requirement Force checked in code | Catches misclassified requirements |
| 10 | Hand-checked answer key and evaluation in the repo | A true precision figure; a gate against silent regressions |

**7. Rubric in chunk calls.** `_evaluation_factors` passes only factor names and weights from the rule-based
Evaluation Summary. Add the text of the pages those rows cite (pp. 19–23 here), capped at a few thousand
characters. It belongs in the chunk system prompt, so it stays cached across chunk calls.
*Check:* Evaluation Link coverage by section (the evaluation script, split by page range as in the step-4 analysis).

**8. Second look at Unverified rows.** `check_citations` flags 14–20 `gpt-6-luna` requirements per run whose text is
not on the cited page. Send each one, with the text of its cited page and the pages next to it, to a cheap model
(`gpt-5-nano`). Ask it to return the page the text is on, or "not in document". Correct the citation, or drop the
row with a warning.
*Where:* after `check_citations` in `_run_stage1`; or in phase B, which has spare time.
*Check:* grounding and recall in the evaluation; drops should be rare and listed in the warnings.

**9. Requirement Force check.** `gpt-5-nano` labelled 340 of 350 requirements Mandatory. Compare each row's Force
with its wording ("shall", "must" = Mandatory; "may", "should" = Advisory; "if", "when" = Conditional), and add a
note to Notes where they disagree. Do not overwrite the model's label: wording alone misreads cases such as "The
Department may reject...".

**10. Answer key and evaluation in the repo.** Grounding and recall are measured against a `gpt-5.6-sol` workbook,
which is model output, by [evaluate.py](../evaluate.py) (moved into the repo on Oct 23). To fix the rest:
- Have a proposal reviewer hand-check about 30 Compliance Matrix requirements from `rfp-1.pdf` (correct text and
  page, plus any the shred missed).
- Store that answer key as a fixture in the repo.
- ~~Move the evaluation script into the repo (`evaluate.py`)~~ done; add a documented pass threshold once the
  answer key exists.
- Add more RFPs to `rfp_docs/`; every number above comes from one document.

## Gaps

**11. Word and text documents are not chunked.** `plan_chunks` in [chunking.py](../doc_to_excel/chunking.py) returns a
single chunk for anything that is not a PDF. A long `.docx` RFP therefore runs as one large Compliance Matrix call:
the slow, low-recall pattern the chunking removed. Fix: split non-PDF documents at heading boundaries from
`result.sections`, by character count. Cite a section instead of a page, and skip the page-based citation check for
them.
*Check:* a long `.docx` RFP before and after: run time, requirement count, recall against a reference run.

**12. Scanned PDFs.** There is no OCR, so a scanned RFP yields no text (see the README's Limitations). This is out of
scope for performance work, but worth tracking.

## Done

Measured on `rfp-1.pdf` with `gpt-6-luna`, each run on a fresh cache prefix (a run started minutes after another
reads that run's cache and looks better than it is).

| Run | Time | Cost | Cached input | Grounding | Recall | Requirements |
| --- | --- | --- | --- | --- | --- | --- |
| Before (two runs, Oct 22) | 117–128 s | $0.094–$0.103 | 73–81% | 95–96% | 78–79% | — |
| After (two runs) | 114 s, 128 s | $0.099, $0.093 | 78%, 82% | 95%, 95% | 74%, 77% | 392, 408 |
| Word copy of the RFP, chunked by section | 126 s | $0.095 | 82% | n/a (no pages) | 79% | 382 |
| Word copy of the RFP, read whole (before) | 270 s | $0.065 | 80% | n/a (no pages) | 79% | 224 |

The page chunks and chunk prompts are byte-for-byte the same as before, so the PDF recall difference is
run-to-run variance in the model, not the change. Stage 3 output (26 Show Stoppers rows, readiness, issue log) was
complete in every run.

**1. Cache warm-up: measured, not adopted.** Without it, the 12 stage-2 calls already read 91% of their input from
the cache (they reach the server a moment apart); with it, 99%. That is ~19K tokens, $0.002 a `gpt-6-luna` run,
for 2.5 s more on the critical path. Measuring it showed why stage 3 cached poorly: **OpenAI's prompt cache reuses
only the system message**, not the start of the user message (a probe with the same 10K-token text: 2.4K tokens
cached at the start of the user message, all 10K in the system message). Stage 3's summary of the finished tabs
was in the user message, so each of its six calls paid for ~21K uncached tokens. It is now in the system message:
the issue-log and readiness calls read 100% from the cache, and stage 3 went from ~49 s to 32–36 s.

**3. Straggler resend: done.** `_run_parallel` in `shred_ai.py` runs every phase. A call still running after 2× the
median time of its finished peers of the same kind (and at least 30 s, with at least half of them finished) is
sent again, and the first copy to answer is used. Link calls now return their links instead of writing the rows,
so a losing copy cannot change the shred; its tokens are still billed and recorded if it finishes before the run
is logged. One call was resent in each verification run (a stage-2 tab twice, a Compliance Matrix chunk once).

**11. Word documents: done.** `plan_chunks` cuts a document without pages into runs of whole sections of about the
chunk size, before a top-level heading when possible; a section longer than a chunk is split at paragraphs. The
tables (which have no position in a Word document's text) form a last chunk, and before this change they were
missing from the Compliance Matrix call altogether. Rows cite section headings; a failed chunk falls back to the
rule-based rows that cite its headings; the page check is skipped. On a Word copy of `rfp-1.pdf` the run took less
than half the time and found 70% more requirements, at the same recall.

## Quality checks (7, 8, 9)

`gpt-6-luna` on `rfp-1.pdf`, Oct 23, scored by `evaluate.py` against the Oct 21 `gpt-5.6-sol` workbook. One run each,
so a few points either way is run-to-run variance (the three runs before the change span 74–78% recall).

| Run | Time | Cost | Requirements | Grounded | Requirement recall | Evaluation Link |
| --- | --- | --- | --- | --- | --- | --- |
| Before (step 5d; two Oct 23 runs) | 114–128 s | $0.093–$0.099 | 384–408 | 95–96% | 74–78% | 41–45% |
| With 7, 8 and 9 | 146 s | $0.105 | 447 | 92% | 73% | 46% |
| With 8 and 9 (7 removed) | 120 s | $0.096 | 374 | 97% | 78% | 36% |

**7. Rubric in chunk calls: tried, removed.** The chunk system prompt got the pages the rule-based Evaluation Summary
cites, with gaps of up to 5 pages filled in (pp. 19–24 on `rfp-1.pdf`, ~3K cached tokens). Evaluation Link rose on
the evaluation pages (41% to 63%) but barely elsewhere (58% to 61% on pp. 1–18, 16% to 17% on pp. 25–31), and that was
the aim. The chunk that owns those pages also wrote a reworded row per rubric bullet ("Priority 1 scoring
considers..."): 36 rows on pp. 20–23 against 10 before. Ungrounded rows elsewhere held at 8–9, but on pp. 20–23 they
went from 6–12 to 26, so grounding fell to 92%. Evaluation Link swings between 36% and 46% from run to run anyway, so
a fix for it needs several runs per variant to measure.

**8. Second look at Unverified rows: done, without drops.** `_second_look` sends each Unverified requirement to
`gpt-5-nano` (low effort), whatever model runs the shred. It sees the cited pages, their neighbours, and the page
sharing the most words with the row; a page it names among those replaces the citation, and Notes say so. In the
first run it placed 34 of 36: 31 on the page with the most word overlap, the other 3 plausible table rows. It said
two rows were on no page, but both were in the rubric table on p. 22, which it had been shown, so rows are never
dropped: anything not placed stays Unverified. In the second run it placed all 12. Cost under $0.01; in the first run
it took 17 s at the end of stage 1. `evaluate.py` still counts a placed row as ungrounded, since its words do not match the page.

**9. Requirement Force check: done.** `_check_force` notes rows whose wording suggests another Force ("shall",
"must", "required", "may not" = Mandatory; "may", "should" = Advisory; "if", "unless", "in the event" = Conditional,
when the row also has one of the others). The label is not changed. Only Mandatory and Advisory rows are checked:
the first version also checked Conditional rows, and 30 of its 41 notes were on Conditional rows whose condition is in
the surrounding text. Now it flags 11–14 rows a run (3–4%). A "may" in a subordinate clause ("barriers that may
affect") or before a date is not counted.
