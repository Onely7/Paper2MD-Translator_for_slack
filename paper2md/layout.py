"""
Turn Dolphin-v2 layout elements into translation segments.

1. Normalize the labels (Dolphin-v2 names -> the vocabulary below) and drop
   page furniture (running headers, page numbers, arXiv stamps).
2. Split the document into blocks at headings.  Elements before the first
   heading (e.g. a notice above the title) form a block right after the title.
3. Order each block: title, text (headings, paragraphs, lists, formulas, code,
   references), then figures/tables/captions, then footnotes.
4. Merge consecutive text elements of a block into one region unless a large
   vertical gap separates them (a column or page change).
5. Join regions that continue across columns/pages (a paragraph, the reference
   list) into one segment with several crops.
6. Let a figure/table crop include the caption right after it.

Paragraphs, lists, formulas and captions are translated from the cropped
images.  Dolphin's text is used only for the title and the references
(``TEXT_LABELS``).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image

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
AUTHOR_TAGS = frozenset({"author", "author_affili", "author_mail"})
# Dolphin labels whose text is read: the title, headings (to find the
# reference section) and the reference entries.
TEXT_LABELS = frozenset({"sec_0", "sec_1", "sec_2", "sec_3", "sec_4", "sec_5", "reference", "catalogue"})

BLOCK_START_LABELS = ("title", "author", "sec", "sub_sec", "sub_sub_sec")
TEXT_FLOW_LABELS = ("author", "sec", "sub_sec", "sub_sub_sec", "para", "list", "equ", "code", "reference")
FIGURE_LABELS = ("fig", "tab", "cap")
FOOTNOTE_LABELS = ("fnote",)
CONTINUATION_LABELS = ("para", "list", "equ", "code", "reference")
_KNOWN_LABELS = frozenset(("title", *TEXT_FLOW_LABELS, *FIGURE_LABELS, *FOOTNOTE_LABELS))

REFERENCE_HEADING_RE = re.compile(r"(references?|bibliography)\b", re.IGNORECASE)
REFERENCE_PAGE_HEIGHT = 4000  # pixel thresholds below are tuned for pages rendered at this height


def normalize_label(label: str, tags: list[str] | None = None) -> str | None:
    """
    Map a Dolphin label to the vocabulary used here; None means "drop".

    >>> normalize_label("sec_0")
    'title'
    >>> normalize_label("para", ["author", "author_affili"])
    'author'
    >>> normalize_label("foot", ["page_num"]) is None
    True
    """
    if label in DROPPED_LABELS:
        return None
    mapped = DOLPHIN_LABEL_MAP.get(label, label if label in _KNOWN_LABELS else "para")
    if mapped in ("para", "fnote") and AUTHOR_TAGS.intersection(tags or ()):
        return "author"
    return mapped


@dataclass
class Segment:
    """A unit sent to the translation model (or embedded as an image)."""

    kind: str  # "title" | "text" | "reference" | "figure"
    first_label: str
    labels: list[str]
    bboxes: list[list[int]]
    pages: list[int]
    texts: list[str]  # Dolphin text of each element ("" when not read)
    images: list[str] = field(default_factory=list)  # crop of each bbox

    @property
    def text(self) -> str:
        return "\n".join(t for t in self.texts if t)

    @property
    def has_all_texts(self) -> bool:
        return all(t.strip() for t in self.texts)

    def to_json(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Building segments
# ---------------------------------------------------------------------------
def _flatten(recognition: dict) -> list[dict]:
    elements = []
    for page, page_data in enumerate(recognition["pages"]):
        for element in sorted(page_data.get("elements", []), key=lambda e: e.get("reading_order", 0)):
            tags = element.get("tags") or []
            label = normalize_label(element.get("label", ""), tags)
            if label is not None:
                elements.append(
                    {"label": label, "bbox": element["bbox"], "text": element.get("text") or "", "page": page}
                )
    return elements


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


def _order_block(block: list[dict]) -> list[dict]:
    def pick(labels):
        return [e for e in block if e["label"] in labels]

    return pick(("title",)) + pick(TEXT_FLOW_LABELS) + pick(FIGURE_LABELS) + pick(FOOTNOTE_LABELS)


def _merge_bbox(bboxes: list[list[int]], scale: float) -> list[int]:
    return [
        int(min(b[0] for b in bboxes) - 10 * scale),
        int(min(b[1] for b in bboxes) - 10 * scale),
        int(max(b[2] for b in bboxes) + 20 * scale),
        int(max(b[3] for b in bboxes) + 10 * scale),
    ]


def _regions(block: list[dict], scale: float) -> list[dict]:
    """Merge consecutive text elements that are vertically close (same column and page)."""
    regions: list[dict] = []
    for element in block:
        previous = regions[-1] if regions else None
        if (
            element["label"] in TEXT_FLOW_LABELS
            and previous is not None
            and previous["mergeable"]
            and element["page"] == previous["page"]
            and abs(element["bbox"][1] - previous["bottom"]) < 100 * scale
        ):
            previous["elements"].append(element)
            previous["bottom"] = element["bbox"][3]
        else:
            regions.append(
                {
                    "elements": [element],
                    "page": element["page"],
                    "bottom": element["bbox"][3],
                    "mergeable": element["label"] in TEXT_FLOW_LABELS,
                }
            )
    for region in regions:
        elements = region["elements"]
        region["first_label"] = elements[0]["label"]
        region["last_label"] = elements[-1]["label"]
        region["bbox"] = _merge_bbox([e["bbox"] for e in elements], scale)
    return regions


def _is_reference(labels: list[str], text: str) -> bool:
    return "reference" in labels or bool(REFERENCE_HEADING_RE.match(text.lstrip(" #*_\n\t")))


def build_segments(recognition: dict, page_height: int = REFERENCE_PAGE_HEIGHT) -> list[Segment]:
    """Group Dolphin's elements into segments in reading order."""
    scale = page_height / REFERENCE_PAGE_HEIGHT
    regions = [r for block in _split_blocks(_flatten(recognition)) for r in _regions(_order_block(block), scale)]

    # join regions that continue across columns/pages
    groups: list[list[dict]] = []
    for region in regions:
        if (
            groups
            and groups[-1][-1]["last_label"] in CONTINUATION_LABELS
            and region["first_label"] in CONTINUATION_LABELS
        ):
            groups[-1].append(region)
        else:
            groups.append([region])

    # a figure/table crop includes the caption that follows it
    for current, following in zip(groups, groups[1:], strict=False):
        if current[0]["last_label"] in ("fig", "tab") and following[0]["first_label"] == "cap":
            current[0]["bbox"] = _merge_bbox([current[0]["bbox"], following[0]["bbox"]], scale)

    segments = []
    for group in groups:
        elements = [e for region in group for e in region["elements"]]
        labels = [e["label"] for e in elements]
        texts = [e["text"] for e in elements]
        if labels[0] == "title":
            kind = "title"
        elif labels[-1] in ("fig", "tab"):
            kind = "figure"
        elif _is_reference(labels, "\n".join(t for t in texts if t)):
            kind = "reference"
        else:
            kind = "text"
        segments.append(
            Segment(
                kind=kind,
                first_label=labels[0],
                labels=labels,
                bboxes=[region["bbox"] for region in group],
                pages=[region["page"] for region in group],
                texts=texts,
            )
        )
    return segments


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
