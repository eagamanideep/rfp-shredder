"""Turn PDF, Word and text documents into structured rows: pages, sections, requirements and tables."""

from __future__ import annotations

import io
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePath

SUPPORTED_TYPES = ("pdf", "docx", "txt", "md")

# Keyword -> requirement type. Order matters: the first match in a sentence wins, strongest first.
KEYWORD_TYPES = {
    "must": "Mandatory",
    "shall": "Mandatory",
    "required": "Mandatory",
    "mandatory": "Mandatory",
    "should": "Recommended",
    "expected": "Recommended",
    "will": "Commitment",
    "responsible for": "Commitment",
    "may": "Optional",
}
DEFAULT_KEYWORDS = ("must", "shall", "required", "mandatory", "should")

_ENUM_RE = re.compile(r"^(?:[•▪◦●○■□➢►\-–*]|o(?=\s)|\(?\d{1,2}[.)]|\(?[a-zA-Z][.)]|\(?[ivxIVX]{1,4}[.)])\s+")
_PAGE_NUMBER_RE = re.compile(r"^(?:page\s+)?\d+(?:\s+of\s+\d+)?$", re.I)
_TOC_RE = re.compile(r"\.{4,}\s*\d+\s*$")
_ABBREV_END_RE = re.compile(r"(?:(?:^|\s)(?:[A-Za-z]\.){2,3}|\b(?:e\.g|i\.e|etc|vs|No|St|Mr|Ms|Dr|Inc|Ltd)\.)$")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"“'‘])")

_DOC_PART = r"(?:SECTION|Section|ARTICLE|Article|PART|Part|CHAPTER|Chapter|APPENDIX|Appendix|EXHIBIT|Exhibit)"
_HEADING_RULES: list[tuple[int, re.Pattern[str], bool]] = [
    # (level, pattern, needs title-like wording)
    (1, re.compile(rf"^{_DOC_PART}\s+[0-9IVX]+[A-Z]?\s*(?:[-–—:.]\s*\S.{{0,100}})?$"), False),
    (2, re.compile(r"^Priority\s+\d+\s*[-–—:]\s*\S.{0,100}$"), False),
    (2, re.compile(r"^(?:[A-Z]|II|III|IV|VI{1,3}|IX)\.\s+[A-Z].{1,90}$"), True),
    (3, re.compile(r"^Step\s+\d+\s*:\s*\S.{0,80}$"), False),
    (3, re.compile(r"^\d{1,2}[.)]\s+[A-Z].{2,80}$"), True),
]
# "2. SERVICES TO BE PERFORMED. The State of New" -> heading plus the start of its body text.
_CAPS_NUMBERED_RE = re.compile(r"^(\d{1,2}\.\s+[A-Z][A-Z0-9 /&,'’()\-]+?[A-Z)])(?:\.(?:\s+(.+))?)?$")
# "1.1 State Agency Name" -> level from the depth of the number.
_DECIMAL_RE = re.compile(r"^(\d+(?:\.\d+)+)\.?\s+([A-Z].{1,80})$")
_ALL_CAPS_RE = re.compile(r"^[A-Z][A-Z0-9 &/,'’()\-–—:]{3,80}$")


@dataclass
class Line:
    text: str
    page: int | None
    bold: bool = False


@dataclass
class Paragraph:
    text: str
    page: int | None
    enumerated: bool = False
    heading_level: int | None = None


@dataclass
class Table:
    page: int | None
    rows: list[list[str]]


@dataclass
class Section:
    id: int
    level: int
    heading: str
    path: str
    page: int | None
    paragraphs: list[Paragraph] = field(default_factory=list)

    @property
    def content(self) -> str:
        return "\n".join(p.text for p in self.paragraphs)


@dataclass
class Requirement:
    id: str
    section: str
    page: int | None
    text: str
    keyword: str
    type: str


@dataclass
class ExtractionResult:
    name: str
    file_type: str
    page_texts: list[str]
    sections: list[Section]
    requirements: list[Requirement]
    tables: list[Table]
    warnings: list[str] = field(default_factory=list)


def extract_document(
    data: bytes,
    filename: str,
    keywords: tuple[str, ...] = DEFAULT_KEYWORDS,
    inherit_list_items: bool = True,
    strip_headers: bool = True,
    exclude_table_text: bool = True,
) -> ExtractionResult:
    file_type = PurePath(filename).suffix.lower().lstrip(".")
    warnings: list[str] = []

    if file_type == "pdf":
        page_lines, tables = _read_pdf(data, exclude_table_text)
        if strip_headers:
            page_lines = _strip_repeated_lines(page_lines)
        page_texts = ["\n".join(line.text for line in lines) for lines in page_lines]
        paragraphs = _lines_to_paragraphs([line for lines in page_lines for line in lines])
        if not any(t.strip() for t in page_texts):
            warnings.append("No text layer found. This looks like a scanned PDF; OCR is not supported.")
    elif file_type == "docx":
        paragraphs, tables = _read_docx(data)
        page_texts = ["\n".join(p.text for p in paragraphs)]
    elif file_type in ("txt", "md"):
        text = data.decode("utf-8", errors="replace")
        page_texts = [text]
        paragraphs = _lines_to_paragraphs([Line(raw, None) for raw in text.splitlines()])
        tables = []
    else:
        raise ValueError(f"Unsupported file type '.{file_type}'. Supported: {', '.join(SUPPORTED_TYPES)}")

    sections = _build_sections(paragraphs)
    requirements = _find_requirements(sections, keywords, inherit_list_items)
    if not requirements and keywords:
        warnings.append("No requirement statements matched the selected keywords.")
    return ExtractionResult(filename, file_type, page_texts, sections, requirements, tables, warnings)


# ---------------------------------------------------------------- PDF


def _read_pdf(data: bytes, exclude_table_text: bool) -> tuple[list[list[Line]], list[Table]]:
    import pdfplumber

    page_lines: list[list[Line]] = []
    tables: list[Table] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            text_page = page
            for found in page.find_tables():
                rows = clean_table_rows(found.extract())
                if rows:
                    tables.append(Table(number, rows))
                if exclude_table_text:
                    text_page = text_page.outside_bbox(found.bbox, strict=False)
            words = text_page.extract_words(extra_attrs=["fontname"])
            page_lines.append([Line(text, number, bold) for text, bold in _words_to_lines(words, page.width)])
    return page_lines, tables


def _words_to_lines(words: list[dict], page_width: float) -> list[tuple[str, bool]]:
    """Rebuild reading-order lines from positioned words, reading two-column layouts column by column."""
    visual = _group_visual_lines(words)
    gutter = _find_gutter(words, page_width)
    if gutter is None:
        return [_render(line) for line in visual]

    out: list[tuple[str, bool]] = []
    left: list[tuple[str, bool]] = []
    right: list[tuple[str, bool]] = []
    for line in visual:
        lhs = [w for w in line if w["x1"] <= gutter]
        rhs = [w for w in line if w["x0"] >= gutter]
        crosses = len(lhs) + len(rhs) < len(line)
        # A small gap between the halves means ordinary word spacing on a full-width line (e.g. a footer).
        narrow_gap = bool(lhs and rhs and rhs[0]["x0"] - lhs[-1]["x1"] < 8)
        if crosses or narrow_gap:
            out += left + right + [_render(line)]
            left, right = [], []
            continue
        if lhs:
            left.append(_render(lhs))
        if rhs:
            right.append(_render(rhs))
    return out + left + right


def _group_visual_lines(words: list[dict]) -> list[list[dict]]:
    """Group words that overlap vertically, so offset glyphs (bullets, superscripts) stay on their line."""
    lines: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines:
            anchor = lines[-1][0]
            overlap = min(anchor["bottom"], word["bottom"]) - max(anchor["top"], word["top"])
            smaller = min(anchor["bottom"] - anchor["top"], word["bottom"] - word["top"])
            if overlap >= 0.5 * smaller:
                lines[-1].append(word)
                continue
        lines.append([word])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def _find_gutter(words: list[dict], page_width: float) -> float | None:
    """Return the x position of a vertical gap splitting the page into two text columns, if there is one."""
    if len(words) < 60:
        return None
    best_x, best_cross = 0.0, len(words)
    x = page_width * 0.35
    while x <= page_width * 0.65:
        cross = sum(1 for w in words if w["x0"] < x < w["x1"])
        if cross < best_cross:
            best_x, best_cross = x, cross
        x += 2
    left = sum(1 for w in words if w["x1"] <= best_x)
    right = sum(1 for w in words if w["x0"] >= best_x)
    if best_cross <= max(3, len(words) * 0.01) and min(left, right) >= len(words) * 0.25:
        return best_x
    return None


def _render(words: list[dict]) -> tuple[str, bool]:
    text = " ".join(w["text"] for w in words)
    bold = all(_is_bold(w.get("fontname", "")) for w in words)
    return text, bold


def _is_bold(fontname: str) -> bool:
    name = fontname.lower()
    return any(k in name for k in ("bold", "black", "heavy", "semibold")) and not any(
        k in name for k in ("italic", "oblique")
    )


def _strip_repeated_lines(page_lines: list[list[Line]]) -> list[list[Line]]:
    """Drop page numbers and running headers/footers that repeat on most pages."""

    def key(text: str) -> str:
        return re.sub(r"\d+", "#", text.strip().lower())

    counts = Counter(k for lines in page_lines for k in {key(line.text) for line in lines})
    threshold = max(3, len(page_lines) // 2)
    repeated = {k for k, n in counts.items() if n >= threshold and len(k) <= 80}
    return [
        [line for line in lines if key(line.text) not in repeated and not _PAGE_NUMBER_RE.match(line.text.strip())]
        for lines in page_lines
    ]


def clean_table_rows(raw: list[list[str | None]]) -> list[list[str]]:
    """Normalise cell text and drop the empty rows/columns that merged cells leave behind."""
    rows = [[re.sub(r"[ \t]+", " ", cell or "").strip() for cell in row] for row in raw]
    rows = [row for row in rows if any(row)]
    if not rows:
        return []
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    keep = [i for i in range(width) if any(row[i] for row in rows)]
    rows = [[row[i] for i in keep] for row in rows]
    return _merge_split_columns(rows)


def _merge_split_columns(rows: list[list[str]]) -> list[list[str]]:
    """Merge neighbouring columns that are really one merged cell split by the PDF grid.

    Two columns merge when no row fills both and one of them is mostly empty, which keeps genuinely sparse
    columns (e.g. Yes/No tick columns) apart.
    """
    if not rows:
        return rows
    n = len(rows)
    columns = [[row[i] for row in rows] for i in range(len(rows[0]))]
    out = [columns[0]]
    for column in columns[1:]:
        current = out[-1]
        clash = any(a and b for a, b in zip(current, column, strict=True))
        sparse = min(sum(1 for v in current if v), sum(1 for v in column if v)) <= n / 3
        if not clash and sparse:
            out[-1] = [a or b for a, b in zip(current, column, strict=True)]
        else:
            out.append(column)
    return [[col[r] for col in out] for r in range(n)]


# ---------------------------------------------------------------- DOCX


def _read_docx(data: bytes) -> tuple[list[Paragraph], list[Table]]:
    import docx

    document = docx.Document(io.BytesIO(data))
    paragraphs: list[Paragraph] = []
    for p in document.paragraphs:
        text = p.text.strip()
        if not text:
            continue
        style = (p.style.name if p.style is not None else "").lower()
        level = None
        if m := re.match(r"heading (\d)", style):
            level = int(m.group(1))
        elif style == "title":
            level = 1
        else:
            level, text, rest = _detect_heading(text, bold=False)
            if rest:
                paragraphs.append(Paragraph(text, None, heading_level=level))
                text, level = rest, None
        enumerated = "list" in style or bool(_ENUM_RE.match(text))
        paragraphs.append(Paragraph(text, None, enumerated and level is None, level))

    tables = []
    for t in document.tables:
        rows = clean_table_rows([[cell.text for cell in row.cells] for row in t.rows])
        if rows:
            tables.append(Table(None, rows))
    return paragraphs, tables


# ---------------------------------------------------------------- structure


def _detect_heading(text: str, bold: bool) -> tuple[int | None, str, str]:
    """Return (level, heading text, trailing body text); level is None when the line is not a heading."""
    if len(text) > 120 or _TOC_RE.search(text) or text.endswith((",", ";")):
        return None, text, ""
    if m := _CAPS_NUMBERED_RE.match(text):
        return 2, m.group(1), m.group(2) or ""
    for level, pattern, needs_title in _HEADING_RULES:
        if pattern.match(text) and not (needs_title and not _is_title_like(text)) and not text.endswith(":"):
            return level, text, ""
    if (m := _DECIMAL_RE.match(text)) and _is_title_like(m.group(2), 0) and not text.endswith((".", ":")):
        return min(4, m.group(1).count(".") + 2), text, ""
    if _ALL_CAPS_RE.match(text) and re.search(r"[A-Z]{4,}", text) and not _ENUM_RE.match(text):
        return 1, text, ""
    if bold and len(text) <= 100 and text[0].isupper() and not text.endswith((".", ":")) and not _ENUM_RE.match(text):
        return 3, text, ""
    return None, text, ""


def _is_title_like(text: str, max_lower_ratio: float = 1 / 3) -> bool:
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'’\-]*", text) if len(w) >= 4]
    if not words:
        return True
    lower = sum(1 for w in words if w[0].islower())
    return lower <= len(words) * max_lower_ratio


def _is_caps(text: str) -> bool:
    return text.upper() == text and bool(re.search(r"[A-Z]{3,}", text))


def _lines_to_paragraphs(lines: list[Line]) -> list[Paragraph]:
    paragraphs: list[Paragraph] = []
    current: Paragraph | None = None
    previous_len = 0
    previous_was_bold_heading = False
    widest = max((len(line.text.strip()) for line in lines), default=0)
    toc_pages = {
        page for page, n in Counter(line.page for line in lines if _TOC_RE.search(line.text)).items() if n >= 3
    }

    for line in lines:
        text = line.text.strip()
        if not text:
            current = None
            continue
        last = paragraphs[-1] if paragraphs else None
        # An ALL-CAPS heading that wraps continues on the next ALL-CAPS line.
        if last is not None and last.heading_level and _is_caps(last.text) and _is_caps(text) and len(text) <= 60:
            if not last.text.endswith(".") and not _ENUM_RE.match(text):
                last.text = f"{last.text} {text}".rstrip(".")
                continue

        mid_sentence = current is not None and not current.text.endswith((".", ":", ";", "?", "!"))
        level, heading, rest = _detect_heading(text, line.bold and not mid_sentence)
        if line.page in toc_pages and not _ALL_CAPS_RE.match(text):
            level, heading, rest = None, text, ""
        if level is not None:
            # A bold heading that wraps onto a second bold line continues the same heading.
            bold_only = line.bold and _detect_heading(text, bold=False)[0] is None
            if bold_only and previous_was_bold_heading and last is not None:
                last.text = f"{last.text} {heading}"
            else:
                paragraphs.append(Paragraph(heading, line.page, heading_level=level))
            current = None
            previous_was_bold_heading = line.bold and not rest
            if rest:
                current = Paragraph(rest, line.page, enumerated=bool(_ENUM_RE.match(rest)))
                paragraphs.append(current)
            previous_len = len(text)
            continue

        previous_was_bold_heading = False
        # "(15) calendar days" wrapping mid-sentence is a continuation, not a list item.
        enumerated = bool(_ENUM_RE.match(text)) and not (mid_sentence and re.match(r"^\(\d+\)", text))
        ended = current is not None and current.text.endswith((".", ":", "?", "!")) and previous_len < widest * 0.8
        if current is None or enumerated or ended or current.text.endswith(":"):
            current = Paragraph(text, line.page, enumerated=enumerated)
            paragraphs.append(current)
        elif current.text.endswith("-") and text[:1].islower():
            current.text += text
        else:
            current.text = f"{current.text} {text}"
        previous_len = len(text)
    return paragraphs


def _build_sections(paragraphs: list[Paragraph]) -> list[Section]:
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    current: Section | None = None
    for p in paragraphs:
        if p.heading_level is not None:
            while stack and stack[-1][0] >= p.heading_level:
                stack.pop()
            stack.append((p.heading_level, p.text))
            current = Section(len(sections) + 1, p.heading_level, p.text, " > ".join(h for _, h in stack), p.page)
            sections.append(current)
            continue
        if current is None:
            current = Section(1, 1, "(Preamble)", "(Preamble)", p.page)
            sections.append(current)
        current.paragraphs.append(p)
    return sections


def split_sentences(text: str) -> list[str]:
    sentences: list[str] = []
    for piece in _SENTENCE_SPLIT_RE.split(text):
        if sentences and _ABBREV_END_RE.search(sentences[-1]):
            sentences[-1] = f"{sentences[-1]} {piece}"
        else:
            sentences.append(piece)
    return [s.strip() for s in sentences if s.strip()]


def _find_requirements(
    sections: list[Section], keywords: tuple[str, ...], inherit_list_items: bool
) -> list[Requirement]:
    if not keywords:
        return []
    ordered = [k for k in KEYWORD_TYPES if k in keywords] + [k for k in keywords if k not in KEYWORD_TYPES]
    pattern = re.compile(r"\b(" + "|".join(re.escape(k) for k in ordered) + r")\b", re.I)

    def classify(sentence: str) -> str | None:
        # A sentence-initial "Should ..." is a condition ("Should a vendor fail ..."), not a recommendation.
        body = re.sub(r"^\W*should\b", "", sentence, flags=re.I)
        found = {m.group(1).lower() for m in pattern.finditer(body)}
        return next((k for k in ordered if k in found), None)

    found: list[tuple[Section, Paragraph, str, str]] = []
    for section in sections:
        inherited: str | None = None
        for p in section.paragraphs:
            matched = False
            for sentence in split_sentences(p.text):
                if kw := classify(sentence):
                    found.append((section, p, sentence, kw))
                    matched = True
            if not matched and p.enumerated and inherited:
                found.append((section, p, p.text, f"{inherited} (list item)"))
            if p.text.endswith(":") and (kw := classify(split_sentences(p.text)[-1])):
                inherited = kw
            elif not p.enumerated:
                inherited = None
            if not inherit_list_items:
                inherited = None

    return [
        Requirement(
            id=f"REQ-{i:03d}",
            section=section.path,
            page=p.page,
            text=sentence,
            keyword=kw,
            type=KEYWORD_TYPES.get(kw.split(" (")[0], "Other"),
        )
        for i, (section, p, sentence, kw) in enumerate(found, start=1)
    ]
