"""
Turn Dolphin-v2 layout elements into segments.

1. Normalize the labels (Dolphin-v2 names -> the vocabulary below) and drop
   page furniture (running headers, page numbers, the arXiv stamp).
2. Split the document into blocks at headings.  Elements before the first
   heading (e.g. a notice above the title) form a block right after the title.
3. In a block under a "References" heading, the elements that look like
   reference entries become ``reference`` elements.
4. Order each block: heading, text (paragraphs, lists, formulas, code),
   then figures/tables/captions, then footnotes.
5. Merge consecutive text elements of a block into one region unless a large
   vertical gap separates them (a column or page change).
6. Join regions that continue across columns/pages (a paragraph, the reference
   list) into one segment with several crops.
7. Let a figure/table crop include the caption right after it.

Segment kinds:

* ``title`` / ``heading``: copied from the PDF in English (``source_text``),
* ``text``: translated from the cropped images,
* ``reference``: the entries copied from the PDF in English (``entries``),
* ``figure``: embedded as an image.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image

from .textlayer import TextLayer, clean_title, join_lines, reference_entries, split_ocr_reference_entries

# Dolphin-v2 label -> label used below (Dolphin 1.x names)
DOLPHIN_LABEL_MAP = {
    "sec_0": "title",
    "sec_1": "sec",
    "sec_2": "sub_sec",
    "sec_3": "sub_sub_sec",
    "sec_4": "sub_sub_sec",
    "sec_5": "sub_sub_sec",
    "para": "para",
    "half_para": "para",
    "catalogue": "para",
    "anno": "para",
    "distorted_page": "para",
    "list": "list",
    "equ": "equ",
    "code": "code",
    "reference": "reference",
    "fig": "fig",
    "tab": "tab",
    "cap": "cap",
    "fnote": "fnote",
}
DROPPED_LABELS = frozenset({"header", "foot", "watermark"})
META_TAGS = frozenset({"meta_num", "meta_pub_date"})  # the arXiv stamp in the margin, dates
AUTHOR_TAGS = frozenset({"author", "author_affili", "author_mail"})
# Labels whose text Dolphin reads on pages without a text layer (scans): the
# title, the headings and the reference entries.
TEXT_LABELS = frozenset({"sec_0", "sec_1", "sec_2", "sec_3", "sec_4", "sec_5", "reference", "catalogue"})

HEADING_LEVELS = {"sec": 2, "sub_sec": 3, "sub_sub_sec": 4}
BLOCK_START_LABELS = ("title", "author", *HEADING_LEVELS)
TEXT_FLOW_LABELS = ("author", "para", "list", "equ", "code")
FIGURE_LABELS = ("fig", "tab", "cap")
FOOTNOTE_LABELS = ("fnote",)
CONTINUATION_LABELS = ("para", "list", "equ", "code")
_KNOWN_LABELS = frozenset(("title", *HEADING_LEVELS, *TEXT_FLOW_LABELS, "reference", *FIGURE_LABELS, *FOOTNOTE_LABELS))

REFERENCE_HEADING_RE = re.compile(
    r"^(?:[A-Z0-9]{1,3}(?:\.\d{1,2})*\.?\s+)?(references?|bibliography|literature cited|works cited)\b", re.IGNORECASE
)
REFERENCE_PAGE_HEIGHT = 4000  # pixel thresholds below are tuned for pages rendered at this height

TextOf = Callable[[dict], str]


def normalize_label(label: str, tags: list[str] | None = None) -> str | None:
    """
    Map a Dolphin label to the vocabulary used here; None means "drop".

    >>> normalize_label("sec_0")
    'title'
    >>> normalize_label("para", ["author", "author_affili"])
    'author'
    >>> normalize_label("foot", ["page_num"]) is None
    True
    >>> normalize_label("para", ["meta_num", "meta_pub_date"]) is None   # the arXiv stamp
    True
    """
    if label in DROPPED_LABELS:
        return None
    mapped = DOLPHIN_LABEL_MAP.get(label, label if label in _KNOWN_LABELS else "para")
    tags = set(tags or ())
    if mapped in ("para", "fnote") and META_TAGS & tags:
        return None
    if mapped in ("para", "fnote") and AUTHOR_TAGS & tags:
        return "author"
    return mapped


def looks_like_references(text: str) -> bool:
    """
    Whether a text is part of a reference list (years, "[12]" labels, venues).

    >>> looks_like_references("[1] A. Author. A title. In NeurIPS, 2020.")
    True
    >>> looks_like_references("We thank the reviewers for their comments and the funding agencies.")
    False
    """
    lines = [line for line in text.splitlines() if line.strip()] or [text]
    hits = len(re.findall(r"\b(?:19|20)\d{2}[a-z]?\b", text))
    hits += len(re.findall(r"(?m)^\s*\[\w{1,8}\]", text))
    hits += len(re.findall(r"\bet al\.|\bProceedings\b|\barXiv\b|\bJournal\b|\bConference\b|\bpp?\.\s*\d", text))
    return hits >= max(1.0, 0.25 * len(lines))


@dataclass
class Segment:
    """A unit of the document: translated, copied from the PDF, or embedded as an image."""

    kind: str  # "title" | "heading" | "text" | "reference" | "figure"
    first_label: str
    labels: list[str]
    bboxes: list[list[int]]  # crop regions
    pages: list[int]
    texts: list[str]  # Dolphin's text of each element ("" when not read)
    elements: list[dict] = field(default_factory=list)  # {"label", "bbox", "page"} of each element
    exclude: dict[int, list[list[int]]] = field(default_factory=dict)  # references: other boxes of their pages
    images: list[str] = field(default_factory=list)  # crop of each region
    source_text: str = ""  # title / heading as printed
    entries: list[str] = field(default_factory=list)  # reference entries as printed
    layer_text: str = ""  # text blocks: the PDF's own text of the region (a hint for the translation)

    @property
    def text(self) -> str:
        return "\n".join(t for t in self.texts if t)

    @property
    def english(self) -> str:
        """The English of a text block: the PDF's own text, else Dolphin's ("" when neither was read)."""
        return self.layer_text or self.text

    @property
    def level(self) -> int:
        """Markdown heading level (1 for the title)."""
        return 1 if self.kind == "title" else HEADING_LEVELS.get(self.first_label, 2)

    def to_json(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Building segments
# ---------------------------------------------------------------------------
def _flatten(recognition: dict) -> tuple[list[dict], dict[int, list[dict]]]:
    """(kept elements in reading order, every element of each page)."""
    elements: list[dict] = []
    every: dict[int, list[dict]] = {}
    for page, page_data in enumerate(recognition["pages"]):
        for order, element in enumerate(sorted(page_data.get("elements", []), key=lambda e: e.get("reading_order", 0))):
            item = {
                "id": (page, order),
                "label": normalize_label(element.get("label", ""), element.get("tags")),
                "bbox": element["bbox"],
                "text": element.get("text") or "",
                "page": page,
            }
            every.setdefault(page, []).append(item)
            if item["label"] is not None:
                elements.append(item)
    return elements, every


def _split_blocks(elements: list[dict]) -> list[list[dict]]:
    leading: list[dict] = []
    blocks: list[list[dict]] = []
    for element in elements:
        if element["label"] in BLOCK_START_LABELS:
            blocks.append([element])
        elif blocks:
            blocks[-1].append(element)
        else:
            leading.append(element)
    if leading:
        # keep what precedes the first heading, right after the title
        blocks.insert(1 if blocks and blocks[0][0]["label"] == "title" else 0, leading)
    return blocks


def _mark_references(block: list[dict], text_of: TextOf) -> None:
    """Label the entries of a reference list ``reference`` (Dolphin often calls them para/catalogue)."""
    heading = block[0] if block[0]["label"] in HEADING_LEVELS else None
    in_references = heading is not None and bool(REFERENCE_HEADING_RE.match(text_of(heading).lstrip(" #*_")))
    if not in_references:
        return
    for element in block[1:]:
        if element["label"] in ("para", "list"):
            text = text_of(element)
            if not text.strip() or looks_like_references(text):
                element["label"] = "reference"


def _order_block(block: list[dict]) -> list[dict]:
    def pick(labels):
        return [e for e in block if e["label"] in labels]

    return (
        pick(("title", *HEADING_LEVELS))
        + pick((*TEXT_FLOW_LABELS, "reference"))
        + pick(FIGURE_LABELS)
        + pick(FOOTNOTE_LABELS)
    )


def _merge_bbox(bboxes: list[list[int]], scale: float) -> list[int]:
    return [
        int(min(b[0] for b in bboxes) - 10 * scale),
        int(min(b[1] for b in bboxes) - 10 * scale),
        int(max(b[2] for b in bboxes) + 20 * scale),
        int(max(b[3] for b in bboxes) + 10 * scale),
    ]


def _flow(label: str) -> str | None:
    """Elements of the same flow can share a region: running text, or a reference list."""
    if label in TEXT_FLOW_LABELS:
        return "text"
    return "reference" if label == "reference" else None


def _regions(block: list[dict], scale: float) -> list[dict]:
    """Merge consecutive elements of one flow that are vertically close (same column and page)."""
    regions: list[dict] = []
    for element in block:
        previous = regions[-1] if regions else None
        flow = _flow(element["label"])
        if (
            flow is not None
            and previous is not None
            and previous["flow"] == flow
            and element["page"] == previous["page"]
            and abs(element["bbox"][1] - previous["bottom"]) < 100 * scale
        ):
            previous["elements"].append(element)
            previous["bottom"] = element["bbox"][3]
        else:
            regions.append({"elements": [element], "page": element["page"], "bottom": element["bbox"][3], "flow": flow})
    for region in regions:
        elements = region["elements"]
        region["first_label"] = elements[0]["label"]
        region["last_label"] = elements[-1]["label"]
        region["bbox"] = _merge_bbox([e["bbox"] for e in elements], scale)
    return regions


def _continues(previous: dict, region: dict) -> bool:
    """A paragraph or a reference list going on in the next column or page."""
    if previous["flow"] == region["flow"] == "reference":
        return True
    return previous["last_label"] in CONTINUATION_LABELS and region["first_label"] in CONTINUATION_LABELS


def build_segments(
    recognition: dict, page_height: int = REFERENCE_PAGE_HEIGHT, text_of: TextOf | None = None
) -> list[Segment]:
    """Group Dolphin's elements into segments in reading order.

    ``text_of(element)`` returns the text of an element (from the PDF's text
    layer when there is one); it is used to find the reference list.  By
    default Dolphin's own text is used.
    """
    text_of = text_of or (lambda element: element["text"])
    scale = page_height / REFERENCE_PAGE_HEIGHT
    elements, every = _flatten(recognition)
    regions = []
    for block in _split_blocks(elements):
        _mark_references(block, text_of)
        regions += _regions(_order_block(block), scale)

    # join regions that continue across columns/pages
    groups: list[list[dict]] = []
    for region in regions:
        if groups and _continues(groups[-1][-1], region):
            groups[-1].append(region)
        else:
            groups.append([region])

    # a figure/table crop includes the caption that follows it
    for current, following in zip(groups, groups[1:], strict=False):
        if current[0]["last_label"] in ("fig", "tab") and following[0]["first_label"] == "cap":
            current[0]["bbox"] = _merge_bbox([current[0]["bbox"], following[0]["bbox"]], scale)

    segments: list[Segment] = []
    for group in groups:
        members = [e for region in group for e in region["elements"]]
        labels = [e["label"] for e in members]
        if labels[0] == "title":
            kind = "title"
        elif labels[0] in HEADING_LEVELS:
            kind = "heading"
        elif labels[-1] in ("fig", "tab"):
            kind = "figure"
        elif all(label == "reference" for label in labels):
            kind = "reference"
        else:
            kind = "text"
        segment = Segment(
            kind=kind,
            first_label=labels[0],
            labels=labels,
            bboxes=[region["bbox"] for region in group],
            pages=[region["page"] for region in group],
            texts=[e["text"] for e in members],
            elements=[{"label": e["label"], "bbox": e["bbox"], "page": e["page"]} for e in members],
        )
        if kind == "reference":
            ids = {e["id"] for e in members}
            segment.exclude = {
                page: [e["bbox"] for e in every[page] if e["id"] not in ids] for page in sorted(set(segment.pages))
            }
        if kind == "title" and segments and segments[-1].kind == "title":  # a title on two elements
            previous = segments[-1]
            previous.labels += segment.labels
            previous.bboxes += segment.bboxes
            previous.pages += segment.pages
            previous.texts += segment.texts
            previous.elements += segment.elements
            continue
        segments.append(segment)
    return segments


MAX_HEADING_CHARS = 200  # a longer "heading" is a paragraph Dolphin mislabelled: translate it


def clean_ocr_text(text: str) -> str:
    """
    Dolphin's text of a title or heading without Markdown/LaTeX markup, on one line.

    >>> clean_ocr_text("## 3.1 Encoder and\\nDecoder Stacks")
    '3.1 Encoder and Decoder Stacks'
    >>> clean_ocr_text("\\\\title{Improved Baselines with Visual Instruction Tuning}")
    'Improved Baselines with Visual Instruction Tuning'
    """
    text = re.sub(
        r"\\(?:title|section|subsection|subsubsection|paragraph|textbf|textit|emph)\*?\s*\{(.*?)\}",
        r"\1",
        text,
        flags=re.S,
    )
    return join_lines(re.sub(r"^#+\s*", "", line).strip().strip("*_").strip() for line in text.splitlines())


def promote_header_headings(recognition: dict, layer: TextLayer) -> list[str]:
    """
    Turn page headers that are really headings into headings; return their text.

    Dolphin-v2 sometimes labels a heading at the top of a page "header" (the
    appendix title "Attention Visualizations" of 1706.03762), and headers are
    dropped.  A header becomes a heading when no other page has the same text in
    its header or footer (so it is not a running header) and the text layer
    shows it in a heading font (``TextLayer.heading_like``).
    """
    pages_with: dict[str, set[int]] = {}
    headers = []
    for page, page_data in enumerate(recognition.get("pages", [])):
        for element in page_data.get("elements", []):
            if element.get("label") not in ("header", "foot"):
                continue
            text = layer.text(page, element["bbox"])
            key = re.sub(r"[\W\d_]+", " ", text).strip().lower()  # page numbers do not count
            pages_with.setdefault(key, set()).add(page)
            if element["label"] == "header" and key:
                headers.append((page, element, text, key))
    promoted = []
    for page, element, text, key in headers:
        if len(pages_with[key]) == 1 and layer.heading_like(page, element["bbox"]):
            element["label"] = "sec_1"
            promoted.append(text)
    return promoted


def attach_printed_text(segments: list[Segment], layer: TextLayer | None) -> None:
    """Fill the title and headings (``source_text``), the reference entries (``entries``) and,
    for text blocks, the PDF's own text of the region (``layer_text``).

    The text comes from the PDF's text layer, or from Dolphin's OCR on pages
    without one.  What stays empty is transcribed from the images by the model.
    """
    for segment in segments:
        if segment.kind == "text" and layer is not None:
            texts = (layer.text(e["page"], e["bbox"]) for e in segment.elements if e.get("label") != "equ")
            segment.layer_text = "\n".join(text for text in texts if text)
            continue
        if segment.kind in ("title", "heading"):
            parts = []
            for element, ocr in zip(segment.elements, segment.texts, strict=True):
                text = layer.text(element["page"], element["bbox"]) if layer is not None else ""
                parts.append(text or clean_ocr_text(ocr))
            text = " ".join(part for part in parts if part).strip()
            if segment.kind == "title":
                segment.source_text = clean_title(text)
            elif len(text) > MAX_HEADING_CHARS:
                segment.kind = "text"
            else:
                segment.source_text = text
        elif segment.kind == "reference":
            entries = reference_entries(layer, segment.elements, segment.exclude) if layer is not None else None
            if entries is None and segment.texts and all(text.strip() for text in segment.texts):
                entries = split_ocr_reference_entries(segment.texts)
            segment.entries = entries or []


def crop_segments(segments: list[Segment], pages: list[Image.Image], out_dir: str | Path) -> None:
    """Save the crop of every segment region and record the paths in ``segment.images``."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    crop_id = 0
    for segment in segments:
        segment.images = []
        for bbox, page in zip(segment.bboxes, segment.pages, strict=True):
            image = pages[page]
            box = (max(0, bbox[0]), max(0, bbox[1]), min(image.width, bbox[2]), min(image.height, bbox[3]))
            if box[2] - box[0] > 3 and box[3] - box[1] > 3:
                path = out_dir / f"p{page + 1:03d}_{crop_id:04d}.png"
                image.crop(box).save(path)
                segment.images.append(str(path))
            crop_id += 1
