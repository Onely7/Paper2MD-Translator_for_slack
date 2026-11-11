"""
Polishing of the Japanese translation with yomiyasu.

yomiyasu (https://github.com/nanaism/yomiyasu, MIT) is a skill that rewrites
AI-written Japanese into natural Japanese without changing what it says;
v2.5.0 is vendored unchanged in ``yomiyasu/``.  The polish model gets its
SKILL.md and the references for papers as instructions and rewrites in
yomiyasu's "body only" mode.  Two passes, each switched on or off
(``Settings.polish_segments`` / ``polish_document``):

1. ``polish_segments``: each translated block (paragraphs, lists, captions,
   footnotes) on its own, after the translation.  One request per block, so
   it costs the most; off by default.
2. ``polish_document``: the whole Markdown, in chunks of a few pages; the
   model returns just the paragraphs it rewrites.  After the block pass it
   looks only at what shows across paragraphs; without it, it also polishes
   each paragraph.

Before a text is sent, math, code, citations, URLs, HTML tags, run-in
paragraph names (``**Encoder:**``) and figure / table labels are replaced by
``⟦n⟧`` placeholders, so they cannot change.  A rewrite is used only when every
placeholder comes back exactly once, its length stays within ``LENGTH_RANGE``
of the original, and yomiyasu's ``yomiyasu_diff.py`` (no model, no network)
finds no meaning risk in it (a changed number, negation, quotation, ...);
otherwise the text stays as translated.
"""

from __future__ import annotations

import json
import logging
import math
import re
import subprocess
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import prompts
from .config import Settings
from .layout import Segment
from .llm import OpenAIClient
from .markdown import _DISPLAY_MATH_RE, _FENCE_RE, _INLINE_CODE_RE, _INLINE_MATH_RE, _restore, _stash
from .progress import ProgressFactory, console_progress
from .terms import mentioned
from .translation import FATAL_ERRORS, strip_markdown_fence

logger = logging.getLogger(__name__)

YOMIYASU_DIFF = prompts.YOMIYASU_DIR / "scripts" / "yomiyasu_diff.py"
POLISHED_LABELS = frozenset({"para", "list", "cap", "fnote"})  # not author blocks, equations or code
LENGTH_RANGE = (0.6, 1.5)  # rewrite / original length, for texts of 40 characters or more
DIFF_TIMEOUT = 60  # seconds for one yomiyasu_diff.py run
DIFF_WORKERS = 8
# characters of text in one whole-Markdown request (about, the chunks are balanced): after the block pass the
# model rewrites few paragraphs; without it, it may rewrite most of them, and its answer has to fit in the
# output tokens.  Each request repeats yomiyasu's instructions, so the chunks are as few as possible.
DOCUMENT_CHUNK_CHARS = {True: 30_000, False: 16_000}  # by after_blocks

_JAPANESE_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")
_PLACEHOLDER_RE = re.compile(r"⟦\d+⟧")
_BODY_RE = re.compile(r"<本文>[ \t]*\n?(.*?)\n?[ \t]*</本文>", re.S)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_REFERENCES = {"references", "reference", "bibliography"}
_PROTECTED_RES = (
    _FENCE_RE,
    _DISPLAY_MATH_RE,
    _INLINE_CODE_RE,
    _INLINE_MATH_RE,
    re.compile(r"<[A-Za-z/][^<>\n]*>"),  # HTML tags (figures)
    re.compile(r"https?://[^\s<>()（）「」、。]+"),  # URLs
    re.compile(r"\[\d+(?:\s*[,–-]\s*\d+)*\]"),  # citations: [12] [2, 19] [3-5]
    re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+|\\\*|[∗†‡§¶]"),  # footnote marks
    re.compile(r"^(?:\*\*|__)[^\n]+?(?:\*\*|__)", re.M),  # run-in paragraph names: **Encoder:**
    re.compile(r"^(?:Figure|Fig\.|Table|Algorithm|Listing)\s*[A-Z]?\d+[a-z]?\s*[:.]", re.M),  # caption labels
    # references in the text: Section 3.2.2, Table 3, Figure 2
    re.compile(
        r"(?<![A-Za-z])(?:Sections?|Figures?|Fig\.|Tables?|Algorithms?|Appendix|Equations?|Eq\.)\s*[A-Z]?\d+(?:\.\d+)*"
    ),
)


@dataclass
class PolishStats:
    segments: int = 0  # segments sent to the polish model
    rewritten: int = 0  # their rewrites that were used
    unchanged: int = 0  # the model changed nothing
    kept: int = 0  # rewrites not used (see reasons): the translation stays
    failed: int = 0  # requests that failed
    document_paragraphs: int = 0  # paragraphs the whole-paper pass could rewrite
    document_edits: int = 0  # its rewrites that were used
    document_kept: int = 0  # its rewrites not used
    reasons: Counter = field(default_factory=Counter)  # why rewrites were not used

    def to_json(self) -> dict:
        data = {name: getattr(self, name) for name in self.__dataclass_fields__ if name != "reasons"}
        return {**data, "reasons": dict(self.reasons)}


# ---------------------------------------------------------------------------
# Placeholders and checks
# ---------------------------------------------------------------------------
def protect(text: str, start: int = 1) -> tuple[str, dict[str, str]]:
    """
    Replace what must not change with ⟦n⟧ placeholders (numbered from ``start``).

    >>> protect("**Encoder:** $N = 6$ 個の層 [11] を使う。")
    ('⟦3⟧ ⟦1⟧ 個の層 ⟦2⟧ を使う。', {'⟦1⟧': '$N = 6$', '⟦2⟧': '[11]', '⟦3⟧': '**Encoder:**'})
    """
    saved: dict[str, str] = {}

    def stash(match: re.Match) -> str:
        token = f"⟦{start + len(saved)}⟧"
        saved[token] = match.group(0)
        return token

    for pattern in _PROTECTED_RES:
        text = pattern.sub(stash, text)
    return text, saved


def restore(text: str, saved: dict[str, str]) -> str:
    for _ in range(3):  # a placeholder may contain another one
        restored = _PLACEHOLDER_RE.sub(lambda m: saved.get(m.group(0), m.group(0)), text)
        if restored == text:
            break
        text = restored
    return text


def extract_body(answer: str) -> str:
    """The rewritten body: the text in <本文> tags (or the whole answer when they are missing)."""
    match = _BODY_RE.search(answer)
    return strip_markdown_fence((match.group(1) if match else answer).strip()).strip()


def rewrite_problem(original: str, rewrite: str) -> str | None:
    """
    Why ``rewrite`` cannot replace ``original`` (both with placeholders), or None.

    >>> rewrite_problem("⟦1⟧ を使う。", "⟦1⟧ を用いる。") is None
    True
    >>> rewrite_problem("⟦1⟧ と ⟦2⟧ を使う。", "⟦1⟧ を使う。")
    'placeholders'
    """
    if not rewrite.strip():
        return "empty"
    if Counter(_PLACEHOLDER_RE.findall(rewrite)) != Counter(_PLACEHOLDER_RE.findall(original)):
        return "placeholders"
    old = len(_PLACEHOLDER_RE.sub("", original).strip())
    new = len(_PLACEHOLDER_RE.sub("", rewrite).strip())
    if old >= 40 and not LENGTH_RANGE[0] <= new / old <= LENGTH_RANGE[1]:
        return "length"
    return None


def meaning_risks(pairs: list[tuple[str, str]], workdir: Path) -> list[list[str] | None]:
    """yomiyasu_diff.py's meaning risks (their kinds) for each (original, rewrite); None if the check failed."""

    def one(pair: tuple[str, str]) -> list[str] | None:
        with tempfile.TemporaryDirectory(dir=workdir, prefix="yomiyasu-") as tmp:
            before, after = Path(tmp) / "original.md", Path(tmp) / "rewrite.md"
            before.write_text(pair[0].strip() + "\n", encoding="utf-8")
            after.write_text(pair[1].strip() + "\n", encoding="utf-8")
            command = [
                sys.executable,
                "-B",
                str(YOMIYASU_DIFF),
                "--json",
                "--doc-type",
                "paper",
                str(before),
                str(after),
            ]
            try:
                done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=DIFF_TIMEOUT)
                report = json.loads(done.stdout)
            except (OSError, subprocess.SubprocessError, ValueError) as e:
                logger.warning("yomiyasu_diff.py failed: %s", e)
                return None
        kinds = [str(risk.get("kind", "?")) for risk in report.get("meaning_risks") or []]
        if not kinds and report.get("meaning_risk_count"):
            kinds = ["?"]
        return kinds

    if not pairs:
        return []
    with ThreadPoolExecutor(max_workers=DIFF_WORKERS) as pool:
        return list(pool.map(one, pairs))


def _review(candidates: list[tuple[str, str]], workdir: Path, stats: PolishStats) -> list[bool]:
    """Which (original, rewrite) pairs may be used: placeholders and length first, then yomiyasu_diff.py."""
    verdicts = [False] * len(candidates)
    checked = []
    for i, (original, rewrite) in enumerate(candidates):
        problem = rewrite_problem(original, rewrite)
        if problem:
            stats.reasons[problem] += 1
        else:
            checked.append(i)
    for i, kinds in zip(checked, meaning_risks([candidates[i] for i in checked], workdir), strict=True):
        if kinds is None:
            stats.reasons["check failed"] += 1
        elif kinds:
            stats.reasons.update(f"meaning risk: {kind}" for kind in kinds)
        else:
            verdicts[i] = True
    return verdicts


def _raise_fatal(results: list) -> None:
    for result in results:
        if isinstance(result, FATAL_ERRORS):
            raise result


# ---------------------------------------------------------------------------
# 1. Each segment
# ---------------------------------------------------------------------------
def polishable(segment: Segment, text: str) -> bool:
    return (
        segment.kind == "text"
        and segment.first_label in POLISHED_LABELS
        and bool(_JAPANESE_RE.search(text))
        and not text.startswith("> ⚠️")  # a failure note
    )


def polish_segments(
    segments: list[Segment],
    parts: dict[int, str],
    client: OpenAIClient,
    settings: Settings,
    workdir: Path,
    progress: ProgressFactory = console_progress,
    glossary: prompts.Glossary | None = None,
) -> tuple[dict[int, str], PolishStats]:
    """Polish each translated block: ({segment index: Markdown}, stats).  Errors that fail every request are raised."""
    stats = PolishStats()
    jobs, requests = [], []
    title = section = ""
    for index, segment in enumerate(segments):
        if segment.kind == "title":
            title = segment.source_text or title
        elif segment.kind == "heading":
            section = segment.source_text or section
        text = parts.get(index, "")
        if polishable(segment, text):
            protected, saved = protect(text)
            jobs.append((index, protected, saved))
            terms = mentioned(glossary or [], segment.english)
            requests.append(prompts.polish_request(protected, settings, title, section, terms))
    polished = dict(parts)
    if not requests:
        return polished, stats
    stats.segments = len(requests)
    with progress(len(requests), "推敲中") as bar:
        results = client.generate_batch(requests, on_result=lambda _i, _r: bar.update(1), return_exceptions=True)
    _raise_fatal(results)

    candidates, owners = [], []
    for (index, protected, saved), result in zip(jobs, results, strict=True):
        if isinstance(result, BaseException):
            stats.failed += 1
            continue
        rewrite = extract_body(result)
        if rewrite == protected.strip():
            stats.unchanged += 1
            continue
        candidates.append((protected, rewrite))
        owners.append((index, saved))
    for (index, saved), (_, rewrite), ok in zip(owners, candidates, _review(candidates, workdir, stats), strict=True):
        if ok:
            polished[index] = restore(rewrite, saved)
            stats.rewritten += 1
        else:
            stats.kept += 1
    return polished, stats


# ---------------------------------------------------------------------------
# 2. The whole paper
# ---------------------------------------------------------------------------
def _editable(block: str) -> bool:
    """A Japanese paragraph or list (not a heading, figure, math, failure note or author block with e-mails)."""
    return (
        bool(_JAPANESE_RE.search(block)) and "@" not in block and not block.lstrip().startswith(("#", "<", ">", "P2MZ"))
    )


def polish_document(
    markdown: str,
    client: OpenAIClient,
    settings: Settings,
    workdir: Path,
    title: str = "",
    progress: ProgressFactory = console_progress,
    stats: PolishStats | None = None,
    glossary: prompts.Glossary | None = None,
    after_blocks: bool = False,
) -> tuple[str, PolishStats]:
    """Polish the whole Markdown; only the paragraphs the model rewrites change (references are not sent).

    ``after_blocks``: the block pass ran before, so only what shows across paragraphs is left to do.
    """
    stats = stats or PolishStats()
    stashed: list[str] = []
    text = _stash(markdown, [(_FENCE_RE, True), (_DISPLAY_MATH_RE, True)], stashed, "Z")
    blocks = [block.strip("\n") for block in re.split(r"\n{2,}", text) if block.strip()]

    lines: list[str] = []  # what the model reads
    paragraphs: dict[str, tuple[int, str, dict[str, str]]] = {}  # id -> (block index, protected text, placeholders)
    in_references, next_token = False, 1
    for i, block in enumerate(blocks):
        heading = _HEADING_RE.match(block) if "\n" not in block else None
        if heading:
            if len(heading.group(1)) <= 2:
                in_references = heading.group(2).strip().rstrip(":.").lower() in _REFERENCES
            if not in_references:
                lines.append(block)
        elif in_references:
            continue
        elif _editable(block):
            protected, saved = protect(block, start=next_token)
            next_token += len(saved)
            pid = f"P{len(paragraphs) + 1}"
            paragraphs[pid] = (i, protected, saved)
            lines.append(f"[{pid}] {protected}")
        elif block.startswith("P2MZ"):
            lines.append("（数式）" if _restore(block, stashed, "Z").startswith("$$") else "（コード）")
        elif block.lstrip().startswith("<img"):
            lines.append("（図表）")
    stats.document_paragraphs = len(paragraphs)
    if not paragraphs:
        return markdown, stats

    # as few chunks as the limit allows, of about the same size
    total = sum(len(line) for line in lines)
    target = total / math.ceil(total / DOCUMENT_CHUNK_CHARS[after_blocks])
    chunks: list[list[str]] = [[]]
    size = 0
    for line in lines:
        if size >= target:
            chunks.append([])
            size = 0
        chunks[-1].append(line)
        size += len(line)
    requests = [
        prompts.polish_document_request("\n\n".join(chunk), settings, title, glossary, after_blocks) for chunk in chunks
    ]
    with progress(len(requests), "全体の推敲中") as bar:
        results = client.generate_batch(requests, on_result=lambda _i, _r: bar.update(1), return_exceptions=True)
    _raise_fatal(results)

    edits: dict[str, str] = {}
    for result in results:
        if isinstance(result, BaseException):
            stats.failed += 1
            continue
        try:
            items = json.loads(result)["edits"]
        except (ValueError, KeyError, TypeError):
            logger.warning("The whole-paper polish did not answer with the expected JSON")
            stats.failed += 1
            continue
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("id") in paragraphs and isinstance(item.get("text"), str):
                edits.setdefault(item["id"], re.sub(r"^\[P\d+\]\s*", "", item["text"].strip()))

    ids = [pid for pid, rewrite in edits.items() if rewrite != paragraphs[pid][1].strip()]
    candidates = [(paragraphs[pid][1], edits[pid]) for pid in ids]
    for pid, ok in zip(ids, _review(candidates, workdir, stats), strict=True):
        index, _, saved = paragraphs[pid]
        if ok:
            blocks[index] = restore(edits[pid], saved)
            stats.document_edits += 1
        else:
            stats.document_kept += 1
    return _restore("\n\n".join(blocks), stashed, "Z").strip() + "\n", stats
