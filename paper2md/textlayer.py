"""
The text stored in the PDF (its text layer), for text that must stay exactly as printed.

Born-digital PDFs (arXiv and nearly all conference papers) contain the
characters they display.  The title, the headings and the reference list are
copied from there: they stay in English and keep every character, which
neither OCR nor a translation model can guarantee.  Scanned PDFs have no
usable text layer; the pipeline then falls back to the text read by Dolphin-v2.

Boxes are given in the pixel coordinates of the pages rendered by
``pdf.render_pages`` (longest side = ``target_size``) and converted to PDF
points here.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import pymupdf

LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"})
# Ligatures are expanded by not passing TEXT_PRESERVE_LIGATURES.
_TEXT_FLAGS = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP
_BOX_MARGIN = 2.0  # points added around a box: Dolphin's boxes can cut a glyph's edge

# Hyphenated line breaks: a hyphen is kept when the word is a compound
# ("pre-training", "multi-head") and dropped when TeX split a word ("con-ference").
COMPOUND_PREFIXES = frozenset(
    """
    anti auto bi co counter cross data deep domain double end few fine high human image in inter intra language large
    long low machine meta mid multi non object off on one open out over post pre real self semi short single small state
    sub task text three top two under up video vision well word zero
    """.split()
)
_HYPHEN_BREAK_RE = re.compile(r"([A-Za-z]+)[-­]$")
_LOWER_WORD_RE = re.compile(r"[a-z]+")
_WORD_RE = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)*")
_URL_TAIL_RE = re.compile(r"(?:https?://|www\.|doi\.org/)\S*[/._\-=?&#%~]$")

# up to three digits: "2018. Contextual ..." in an author-year list is a year, not a label
_NUMBERED_LABEL_RE = re.compile(r"^\[(\d{1,3})\]")
_DOTTED_LABEL_RE = re.compile(r"^(\d{1,3})\.\s")
_NAMED_LABEL_RE = re.compile(r"^\[[A-Za-z][\w+\-.]{0,15}\]")
_TRAILING_MARKS_RE = re.compile(r"[\s∗*†‡§¶⋆♠♣♢♡♦♥\d,]+$")

# TeX writes "T¨ackstr¨om" / "Lo¨ıc": a spacing accent before the letter it belongs to.
_SPACING_ACCENTS = {
    "¨": "̈",  # ¨
    "´": "́",  # ´
    "ˆ": "̂",  # ˆ
    "˜": "̃",  # ˜
    "¸": "̧",  # ¸
    "ˇ": "̌",  # ˇ
    "˘": "̆",  # ˘
    "˙": "̇",  # ˙
    "˚": "̊",  # ˚
    "˝": "̋",  # ˝
    "¯": "̄",  # ¯
}
_SPACING_ACCENT_RE = re.compile(f"([{''.join(_SPACING_ACCENTS)}])\\s?([A-Za-zıȷ])")


def fix_accents(text: str) -> str:
    """
    Combine TeX's separate accent glyphs with their letters.

    >>> fix_accents("Lo¨ıc Barrault, Oscar T¨ackstr¨om, Jos´e")
    'Loïc Barrault, Oscar Täckström, José'
    """

    def combine(match: re.Match) -> str:
        letter = {"ı": "i", "ȷ": "j"}.get(match.group(2), match.group(2))
        return unicodedata.normalize("NFC", letter + _SPACING_ACCENTS[match.group(1)])

    return _SPACING_ACCENT_RE.sub(combine, text)


@dataclass
class Line:
    """A line of text with its box in PDF points."""

    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def height(self) -> float:
        return self.y1 - self.y0


# ---------------------------------------------------------------------------
# Joining lines
# ---------------------------------------------------------------------------
def join_lines(lines: Iterable[str], vocabulary: frozenset[str] = frozenset()) -> str:
    """
    Join the lines of a paragraph, undoing the hyphenation and URL breaks of line ends.

    >>> join_lines(["In Con-", "ference on Vision", "and Pre-", "training."], frozenset({"conference"}))
    'In Conference on Vision and Pre-training.'
    >>> join_lines(["GPT-", "4 and https://github.com/", "tensorflow/t2t."])
    'GPT-4 and https://github.com/tensorflow/t2t.'
    """
    text = ""
    for line in lines:
        line = " ".join(line.split())
        if not line:
            continue
        text = _join(text, line, vocabulary) if text else line
    return text


def _join(left: str, right: str, vocabulary: frozenset[str]) -> str:
    if left.endswith("­"):  # soft hyphen: always a word split
        return left[:-1] + right
    head = _HYPHEN_BREAK_RE.search(left)
    if head:
        tail = _LOWER_WORD_RE.match(right)
        if tail is None:  # "GPT-" + "4", "Vision-" + "Language"
            return left + right
        first, second = head.group(1), tail.group(0)
        joined, hyphenated = (first + second).lower(), f"{first}-{second}".lower()
        if hyphenated in vocabulary and joined not in vocabulary:
            keep = True
        elif joined in vocabulary:
            keep = False
        else:
            keep = first.lower() in COMPOUND_PREFIXES
        return (left if keep else left[:-1]) + right
    if _URL_TAIL_RE.search(left):
        return left + right
    return f"{left} {right}"


def clean_title(text: str) -> str:
    """
    Title as printed, without the footnote marks that point to the authors' notes.

    >>> clean_title("Attention Is All You Need∗†")
    'Attention Is All You Need'
    >>> clean_title("GPT-4 Technical Report")
    'GPT-4 Technical Report'
    """
    text = " ".join(text.split())
    stripped = _TRAILING_MARKS_RE.sub("", text)
    # keep a trailing number that belongs to the title ("GPT-4", "Llama 2")
    if stripped and re.search(r"[∗*†‡§¶⋆♠♣♢♡♦♥]", text[len(stripped) :]):
        return stripped
    return text


# ---------------------------------------------------------------------------
# Text layer
# ---------------------------------------------------------------------------
class TextLayer:
    """Read text from a PDF by boxes given in rendered-page pixels."""

    def __init__(self, pdf_path: str | Path, target_size: int) -> None:
        self._doc = pymupdf.open(pdf_path, filetype="pdf")
        self._scales = [target_size / max(page.rect.width, page.rect.height) for page in self._doc]
        self._lines: dict[int, list[Line]] = {}
        self._usable: dict[int, bool] = {}

    def close(self) -> None:
        self._doc.close()

    def __enter__(self) -> TextLayer:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def page_count(self) -> int:
        return len(self._doc)

    def usable(self, page: int) -> bool:
        """The page has a text layer to rely on (not a scan, no undecodable fonts)."""
        if page not in self._usable:
            pdf_page = self._doc[page]
            text = pdf_page.get_text("text", flags=_TEXT_FLAGS)
            letters = sum(ch.isalpha() for ch in text)
            broken = sum(ch == "�" or unicodedata.category(ch) == "Co" for ch in text)
            self._usable[page] = pdf_page.rotation == 0 and letters >= 50 and broken <= 0.01 * len(text)
        return self._usable[page]

    def _page_lines(self, page: int) -> list[Line]:
        if page not in self._lines:
            data = self._doc[page].get_text("dict", flags=_TEXT_FLAGS)
            lines = []
            for block in data["blocks"]:
                if block.get("type") != 0:
                    continue
                for line in block["lines"]:
                    if abs(line["dir"][0] - 1) > 1e-3:  # vertical text, e.g. the arXiv stamp
                        continue
                    text = fix_accents("".join(span["text"] for span in line["spans"]).translate(LIGATURES))
                    if text.strip():
                        lines.append(Line(*line["bbox"], text=text))
            self._lines[page] = lines
        return self._lines[page]

    def to_points(self, page: int, bbox: list[float], margin: float = _BOX_MARGIN) -> tuple[float, ...]:
        """A box in rendered-page pixels as PDF points, widened by ``margin`` points."""
        scale = self._scales[page]
        x0, y0, x1, y1 = (value / scale for value in bbox)
        return x0 - margin, y0 - margin, x1 + margin, y1 + margin

    def raw_lines(self, page: int, bbox: list[float]) -> list[Line]:
        """Lines of the text layer whose centre lies in ``bbox`` (rendered-page pixels)."""
        box = self.to_points(page, bbox)
        return [line for line in self._page_lines(page) if _centre_in(line, box)]

    def lines(self, page: int, bbox: list[float]) -> list[Line]:
        """Visual lines in ``bbox``, column by column, top to bottom."""
        return [line for column in split_columns(self.raw_lines(page, bbox)) for line in visual_lines(column)]

    def text(self, page: int, bbox: list[float]) -> str:
        """The text in ``bbox`` as one paragraph ("" when the page has no usable text layer)."""
        if not self.usable(page):
            return ""
        return join_lines((line.text for line in self.lines(page, bbox)), self.vocabulary)

    @cached_property
    def vocabulary(self) -> frozenset[str]:
        """Lower-case words of the whole document (to decide on hyphens at line breaks)."""
        words: set[str] = set()
        for page in range(len(self._doc)):
            for line in self._page_lines(page):
                words.update(word.lower() for word in _WORD_RE.findall(line.text))
        return frozenset(words)


def _centre_in(line: Line, box: tuple[float, ...]) -> bool:
    return box[0] <= (line.x0 + line.x1) / 2 <= box[2] and box[1] <= (line.y0 + line.y1) / 2 <= box[3]


def split_columns(lines: list[Line]) -> list[list[Line]]:
    """Group lines into columns: clusters of overlapping horizontal extents, left to right.

    A cluster much narrower than the whole text is not a column but a piece of
    a justified line ("Oxford" after a wide gap); it joins its neighbour.
    """
    if not lines:
        return []
    columns: list[list[Line]] = []
    extents: list[list[float]] = []
    for line in sorted(lines, key=lambda line: line.x0):
        if extents and line.x0 <= extents[-1][1]:
            columns[-1].append(line)
            extents[-1][1] = max(extents[-1][1], line.x1)
        else:
            columns.append([line])
            extents.append([line.x0, line.x1])
    total = max(line.x1 for line in lines) - min(line.x0 for line in lines)
    merged: list[list[Line]] = []
    merged_extents: list[list[float]] = []
    for column, (x0, x1) in zip(columns, extents, strict=True):
        if merged and (x1 - x0 < 0.3 * total or merged_extents[-1][1] - merged_extents[-1][0] < 0.3 * total):
            merged[-1].extend(column)
            merged_extents[-1][1] = max(merged_extents[-1][1], x1)
        else:
            merged.append(list(column))
            merged_extents.append([x0, x1])
    return merged


def visual_lines(lines: list[Line]) -> list[Line]:
    """Merge the pieces of a line (PyMuPDF splits lines at wide gaps) and sort top to bottom."""
    rows: list[list[Line]] = []
    for line in sorted(lines, key=lambda line: (line.y0 + line.y1) / 2):
        if rows:
            row = rows[-1]
            top, bottom = min(piece.y0 for piece in row), max(piece.y1 for piece in row)
            overlap = min(bottom, line.y1) - max(top, line.y0)
            if overlap > 0.5 * min(bottom - top, line.height):
                row.append(line)
                continue
        rows.append([line])
    merged = []
    for row in rows:
        row.sort(key=lambda piece: piece.x0)
        text = " ".join(" ".join(piece.text.split()) for piece in row)
        merged.append(
            Line(min(p.x0 for p in row), min(p.y0 for p in row), max(p.x1 for p in row), max(p.y1 for p in row), text)
        )
    return merged


# ---------------------------------------------------------------------------
# Reference lists
# ---------------------------------------------------------------------------
@dataclass
class _RefLine:
    text: str
    indent: float  # distance from the left edge of its column (points)
    first_in_element: bool


def _sequential_starts(lines: list[_RefLine], pattern: re.Pattern) -> list[bool]:
    """Entry starts by label number; a label must follow the previous one (not a year such as "2020.")."""
    starts, previous = [], None
    for line in lines:
        match = pattern.match(line.text)
        number = int(match.group(1)) if match else None
        start = number is not None and (previous is None or previous < number <= previous + 5)
        if start:
            previous = number
        starts.append(start)
    return starts


def split_reference_entries(lines: list[_RefLine], vocabulary: frozenset[str] = frozenset()) -> list[str]:
    """Split the lines of a reference list into entries.

    * ``[1] ...`` / ``1. ...``: an entry starts at the next label in sequence,
    * ``[Vas+17] ...``: an entry starts at every label,
    * author-year lists with a hanging indent: an entry starts at every line
      that is not indented (a fragment continued in the next column is indented),
    * otherwise: an entry starts with each Dolphin element unless the element
      begins in lower case (the rest of an entry cut by a column or page break).
    """
    if not lines:
        return []
    total = len(lines)
    counts = {
        "numbered": sum(bool(_NUMBERED_LABEL_RE.match(line.text)) for line in lines),
        "dotted": sum(bool(_DOTTED_LABEL_RE.match(line.text)) for line in lines),
        "named": sum(bool(_NAMED_LABEL_RE.match(line.text)) for line in lines),
    }
    style = max(counts, key=counts.get)
    if counts[style] >= 2 or (total <= 3 and counts[style] >= 1):
        if style == "numbered":
            starts = _sequential_starts(lines, _NUMBERED_LABEL_RE)
        elif style == "dotted":
            starts = _sequential_starts(lines, _DOTTED_LABEL_RE)
        else:
            starts = [bool(_NAMED_LABEL_RE.match(line.text)) for line in lines]
    elif sum(line.indent > 3.0 for line in lines) >= 0.15 * total:
        starts = [line.indent <= 3.0 for line in lines]
    else:
        starts = [line.first_in_element and not line.text[:1].islower() for line in lines]

    entries: list[list[str]] = []
    for line, start in zip(lines, starts, strict=True):
        if start or not entries:
            entries.append([line.text])
        else:
            entries[-1].append(line.text)
    return [entry for entry in (join_lines(parts, vocabulary) for parts in entries) if re.search(r"[A-Za-z]{2}", entry)]


def _box_columns(boxes: list[list[float]]) -> list[list[list[float]]]:
    """Group boxes whose horizontal extents overlap, left to right."""
    columns: list[list[list[float]]] = []
    right = None
    for box in sorted(boxes, key=lambda box: box[0]):
        if columns and box[0] <= right:
            columns[-1].append(box)
            right = max(right, box[2])
        else:
            columns.append([box])
            right = box[2]
    return columns


def reference_entries(
    layer: TextLayer, elements: list[dict], exclude: dict[int, list[list[float]]] | None = None
) -> list[str] | None:
    """Entries of the reference list whose Dolphin boxes are ``elements`` (in reading order).

    Dolphin sometimes leaves entries out of every box (the end of a column, a
    gap between two boxes).  So each column is read from the top of its first
    reference box downwards: lines between the boxes are kept, and so are the
    lines that follow the last box without a larger vertical gap.  Lines inside
    ``exclude`` (the other elements of the page, such as the "References"
    heading or the page number) are skipped.  Returns None when a page has no
    usable text layer or nothing could be read.
    """
    boxes_by_page: dict[int, list[list[float]]] = {}
    for element in elements:
        if not layer.usable(element["page"]):
            return None
        boxes_by_page.setdefault(element["page"], []).append(element["bbox"])
    exclude = exclude or {}

    lines: list[_RefLine] = []
    for page, boxes in boxes_by_page.items():  # pages in reading order
        skip = [layer.to_points(page, box, margin=0.0) for box in exclude.get(page, [])]
        element_boxes = [layer.to_points(page, box) for box in boxes]
        for column in _box_columns(boxes):
            region = [min(b[0] for b in column), min(b[1] for b in column), max(b[2] for b in column), 10**7]
            raw = [line for line in layer.raw_lines(page, region) if not any(_centre_in(line, s) for s in skip)]
            for text_column in split_columns(raw):
                rows = _contiguous_rows(visual_lines(text_column), element_boxes)
                if not rows:
                    continue
                left = min(row.x0 for row in rows)
                firsts = set()
                for box in element_boxes:
                    inside = [i for i, row in enumerate(rows) if _centre_in(row, box)]
                    if inside:
                        firsts.add(inside[0])
                lines += [_RefLine(row.text, row.x0 - left, i in firsts) for i, row in enumerate(rows)]
    if not lines:
        return None
    return split_reference_entries(lines, layer.vocabulary)


def _contiguous_rows(rows: list[Line], boxes: list[tuple[float, ...]]) -> list[Line]:
    """The rows up to the last one inside a box, then the rows that follow it without a larger gap."""
    inside = [i for i, row in enumerate(rows) if any(_centre_in(row, box) for box in boxes)]
    if not inside:
        return []
    end = inside[-1]
    heights = sorted(row.height for row in rows[: end + 1])
    height = heights[len(heights) // 2]
    while end + 1 < len(rows) and rows[end + 1].y0 - rows[end].y1 <= height:
        end += 1
    return rows[: end + 1]


def split_ocr_reference_entries(texts: list[str]) -> list[str]:
    """Entries from the text Dolphin-v2 read for each reference element (scanned PDFs)."""
    lines: list[_RefLine] = []
    for text in texts:
        for i, line in enumerate(line for line in text.splitlines() if line.strip()):
            lines.append(_RefLine(" ".join(line.split()), 0.0, i == 0))
    return split_reference_entries(lines)
