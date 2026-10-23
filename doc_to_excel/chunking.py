"""Split a document into section-aligned page chunks for parallel extraction, then merge and check the rows.

Small models extract more completely from a few pages than from a whole RFP, and many small calls run in parallel
finish sooner than one long one. Every page belongs to exactly one chunk; a chunk also shows the start of the next
page as read-only context, so a requirement that runs over the page break is seen whole but extracted once. The merge
drops rows taken from that context and repeated rows, and the citation check compares each row's text with the page
it cites.

A Word or text document has no pages, so it is chunked by sections instead (first_page = last_page = 0): rows cite
section headings, a chunk owns the rows whose location names one of its headings, and there is no page check.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from .extract import ExtractionResult

PAGE_REF = re.compile(r"\bpp?\.\s*(\d+)(?:\s*[–-]\s*(\d+))?")
# Share of a row's content words that must appear on a page for the row to count as found there.
FOUND_ON_PAGE = 0.8
# Token overlap (numbers included, so "Priority 1" and "Priority 2" stay apart) above which two rows are one row.
SAME_ROW = 0.9
# Characters of the next page shown after a chunk as read-only context.
LOOKAHEAD_CHARS = 1200


@dataclass
class Chunk:
    first_page: int
    last_page: int  # inclusive; 0 for a section chunk of a document without pages
    text: str
    headings: tuple[str, ...] = ()  # a section chunk's headings

    @property
    def label(self) -> str:
        if not self.last_page and self.headings:
            first, last = (h if len(h) <= 40 else h[:39] + "…" for h in (self.headings[0], self.headings[-1]))
            return f"§ {first}" if first == last else f"§ {first} – {last}"
        return (
            f"p. {self.first_page}" if self.first_page == self.last_page else f"pp. {self.first_page}–{self.last_page}"
        )

    def owns(self, location) -> bool:
        """Whether a row with this Source Location comes from the chunk's pages (or, without pages, its sections)."""
        if self.last_page:
            return any(self.first_page <= p <= self.last_page for p in cited_pages(location, 10**6))
        text = str(location or "").lower()
        return any(h.lower()[:67] in text for h in self.headings if h)


def content_words(text) -> list[str]:
    return [w for w in re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split() if len(w) > 3]


def page_texts_with_tables(result: ExtractionResult) -> list[str]:
    """Each page's text followed by the cells of the tables on it (tables are kept out of the page text)."""
    pages = list(result.page_texts)
    for table in result.tables:
        if table.page and 1 <= table.page <= len(pages):
            rows = "\n".join(" | ".join(c.replace("\n", " ") for c in row) for row in table.rows)
            pages[table.page - 1] += f"\n[Table]\n{rows}"
    return pages


def _table_text(table) -> str:
    return "\n".join(" | ".join(c.replace("\n", " ") for c in row) for row in table.rows)


def _ranges(sizes: list[int], starts: set[int], target_chars: int) -> list[tuple[int, int]]:
    """Consecutive 1-based index ranges of about `target_chars`, ending before an index in `starts` when possible."""
    n, ranges, start = len(sizes), [], 1
    while start <= n:
        end, size = start, sizes[start - 1]
        while end < n and size + sizes[end] <= target_chars:
            end += 1
            size += sizes[end - 1]
        if end < n:
            # Prefer ending just before a section starts, if that keeps at least half the chunk.
            cuts = [e for e in range(end, start + (end - start) // 2 - 1, -1) if e + 1 in starts]
            end = cuts[0] if cuts and cuts[0] >= start else end
        ranges.append((start, end))
        start = end + 1
    return ranges


def plan_chunks(result: ExtractionResult, target_chars: int) -> list[Chunk]:
    """Consecutive, non-overlapping page ranges of about `target_chars`, cut before a section start when possible.

    A document without pages (not a PDF) is cut into runs of sections instead (see _plan_section_chunks). With
    `target_chars` <= 0 the document is one chunk.
    """
    if result.file_type != "pdf":
        return _plan_section_chunks(result, target_chars)
    pages = page_texts_with_tables(result)
    n = len(pages)
    if n <= 1:
        return [Chunk(1, 1, "\n\n".join(pages))]
    if target_chars <= 0:
        return [Chunk(1, n, "\n\n".join(f"=== Page {p} ===\n{pages[p - 1]}" for p in range(1, n + 1)))]
    section_starts = {s.page for s in result.sections if s.level <= 2 and s.page}
    chunks = []
    for start, end in _ranges([len(p) for p in pages], section_starts, target_chars):
        text = "\n\n".join(f"=== Page {p} ===\n{pages[p - 1]}" for p in range(start, end + 1))
        if end < n:
            text += (
                f"\n\n=== Start of page {end + 1}: CONTEXT ONLY. Use it to finish a requirement that begins above; "
                f"do not return rows that begin here. ===\n{pages[end][:LOOKAHEAD_CHARS]}"
            )
        chunks.append(Chunk(start, end, text))
    return chunks


def _plan_section_chunks(result: ExtractionResult, target_chars: int) -> list[Chunk]:
    """Runs of whole sections of about `target_chars`, cut before a top-level heading when possible.

    A section longer than `target_chars` is split at paragraph boundaries; its later parts repeat the heading. The
    document's tables (which have no position in the text) form a last chunk. A short document stays one chunk.
    """
    whole = "\n\n".join(result.page_texts + [_table_text(t) for t in result.tables if not t.page])
    if target_chars <= 0 or len(whole) <= target_chars or not result.sections:
        return [Chunk(1, 1, whole)]
    units: list[tuple[str, str, bool]] = []  # (heading, text, starts a top-level section)
    for section in result.sections:
        part, top = [], section.level <= 2
        for paragraph in section.paragraphs:
            if part and len(section.heading) + sum(len(t) for t in part) + len(paragraph.text) > target_chars:
                units.append((section.heading, "\n".join([section.heading, *part]), top))
                part, top = [], False
            part.append(paragraph.text)
        units.append((section.heading, "\n".join([section.heading, *part]), top))
    starts = {i for i, (_, _, top) in enumerate(units, start=1) if top}
    note = "This document has no page numbers: cite the section heading as Source Location."
    chunks = []
    for start, end in _ranges([len(text) for _, text, _ in units], starts, target_chars):
        text = f"({note})\n\n" + "\n\n".join(text for _, text, _ in units[start - 1 : end])
        if end < len(units):
            text += (
                "\n\n=== Start of the next section: CONTEXT ONLY. Use it to finish a requirement that begins above; "
                f"do not return rows that begin here. ===\n{units[end][1][:LOOKAHEAD_CHARS]}"
            )
        headings = tuple(dict.fromkeys(h for h, _, _ in units[start - 1 : end]))
        chunks.append(Chunk(0, 0, text, headings))
    tables = [_table_text(t) for t in result.tables if not t.page]
    if tables:
        text = f'({note} For a table, cite "Table N".)\n\n' + "\n\n".join(
            f"=== Table {i} ===\n{t}" for i, t in enumerate(tables, start=1)
        )
        chunks.append(Chunk(0, 0, text, ("Tables",)))
    return chunks


def merge_chunk_rows(
    chunk_rows: list[list[dict]], key: Callable[[dict], str], chunks: list[Chunk] | None = None
) -> list[dict]:
    """Rows of all chunks in document order, without rows from the context-only lookahead and without repeats.

    A row that cites only pages after its chunk (or cites the CONTEXT ONLY marker) is dropped: the next chunk owns
    it. A row matching a kept row (SAME_ROW) is dropped, and its Source Location is added to the kept row.
    """
    merged: list[dict] = []
    kept: list[set[str]] = []
    for i, rows in enumerate(chunk_rows):
        chunk = chunks[i] if chunks else None
        for row in rows:
            location = str(row.get("source_location", ""))
            pages = cited_pages(location, 10**6)
            after = chunk and chunk.last_page and pages and min(pages) > chunk.last_page
            if chunk and ("CONTEXT ONLY" in location.upper() or after):
                continue
            tokens = set(re.sub(r"[^a-z0-9]+", " ", key(row).lower()).split())
            match = next((m for m, k in zip(merged, kept, strict=True) if _overlap(tokens, k) >= SAME_ROW), None)
            if match is None:
                merged.append(row)
                kept.append(tokens)
            elif location and location not in match.get("source_location", ""):
                match["source_location"] = f"{match.get('source_location', '')}; also {location}".lstrip("; ")
    return merged


def _overlap(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def cited_pages(location, page_count: int) -> list[int]:
    pages = []
    for a, b in PAGE_REF.findall(str(location or "")):
        lo, hi = int(a), int(b or a)
        pages += [p for p in range(lo, min(hi, lo + 5) + 1) if 1 <= p <= page_count]
    return pages


def check_citations(rows: list[dict], result: ExtractionResult, text_key: str) -> dict[str, int]:
    """Check each row's text against the page it cites; fix the page or flag the row.

    Text found on the cited page(s): left alone. Found on another page: the first page reference in Source Location
    is replaced with that page and Notes say so. Found nowhere: Notes say the row is unverified. Returns counts.
    Only PDFs have pages to check.
    """
    counts = {"checked": 0, "corrected": 0, "unverified": 0}
    if result.file_type != "pdf":
        return counts
    page_words = [set(content_words(p)) for p in page_texts_with_tables(result)]

    def share(words: list[str], pages) -> float:
        on = set().union(*(page_words[p - 1] for p in pages)) if pages else set()
        return sum(w in on for w in words) / len(words)

    for row in rows:
        words = content_words(row.get(text_key))
        if not words:
            continue
        counts["checked"] += 1
        cited = cited_pages(row.get("source_location"), len(page_words))
        if cited and share(words, cited) >= FOUND_ON_PAGE:
            continue
        best = max(range(1, len(page_words) + 1), key=lambda p: share(words, [p]))
        notes = row.get("notes", "")
        if share(words, [best]) >= FOUND_ON_PAGE:
            location = row.get("source_location", "")
            row["source_location"] = (
                PAGE_REF.sub(f"p. {best}", location, count=1) if PAGE_REF.search(location) else f"p. {best}, {location}"
            )
            row["notes"] = "; ".join(n for n in (notes, f"Page corrected to p. {best} by text check.") if n)
            counts["corrected"] += 1
        else:
            row["notes"] = "; ".join(n for n in (notes, "Unverified: text not found on the cited page.") if n)
            counts["unverified"] += 1
    return counts
