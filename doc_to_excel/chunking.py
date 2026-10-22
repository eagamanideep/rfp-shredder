"""Split a document into section-aligned page chunks for parallel extraction, then merge and check the rows.

Small models extract more completely from a few pages than from a whole RFP, and many small calls run in parallel
finish sooner than one long one. Every page belongs to exactly one chunk; a chunk also shows the start of the next
page as read-only context, so a requirement that runs over the page break is seen whole but extracted once. The merge
drops rows taken from that context and repeated rows, and the citation check compares each row's text with the page
it cites.
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
    last_page: int  # inclusive
    text: str

    @property
    def label(self) -> str:
        return (
            f"p. {self.first_page}" if self.first_page == self.last_page else f"pp. {self.first_page}–{self.last_page}"
        )


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


def plan_chunks(result: ExtractionResult, target_chars: int) -> list[Chunk]:
    """Consecutive, non-overlapping page ranges of about `target_chars`, cut before a section start when possible.

    A document without pages (not a PDF), or `target_chars` <= 0, is one chunk.
    """
    pages = page_texts_with_tables(result)
    if result.file_type != "pdf" or len(pages) <= 1:
        return [Chunk(1, 1, "\n\n".join(pages))]
    n = len(pages)
    if target_chars <= 0:
        return [Chunk(1, n, "\n\n".join(f"=== Page {p} ===\n{pages[p - 1]}" for p in range(1, n + 1)))]
    section_starts = {s.page for s in result.sections if s.level <= 2 and s.page}
    chunks, start = [], 1
    while True:
        end, size = start, len(pages[start - 1])
        while end < n and size + len(pages[end]) <= target_chars:
            end += 1
            size += len(pages[end - 1])
        if end < n:
            # Prefer ending just before a section starts, if that keeps at least half the chunk.
            cuts = [e for e in range(end, start + (end - start) // 2 - 1, -1) if e + 1 in section_starts]
            end = cuts[0] if cuts and cuts[0] >= start else end
        text = "\n\n".join(f"=== Page {p} ===\n{pages[p - 1]}" for p in range(start, end + 1))
        if end < n:
            text += (
                f"\n\n=== Start of page {end + 1}: CONTEXT ONLY. Use it to finish a requirement that begins above; "
                f"do not return rows that begin here. ===\n{pages[end][:LOOKAHEAD_CHARS]}"
            )
        chunks.append(Chunk(start, end, text))
        if end >= n:
            return chunks
        start = end + 1


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
            if chunk and ("CONTEXT ONLY" in location.upper() or (pages and min(pages) > chunk.last_page)):
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
