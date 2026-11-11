"""
Segments to Markdown, and the summary.

* Title, headings and references are written as printed (``source_text`` /
  ``entries`` copied from the PDF); the model only transcribes them, without
  translating, when that text could not be read.
* Text segments are translated from their images.  Each request also gets the
  paper title, the current section heading and the glossary entries whose
  English appears in the segment, so terms are translated consistently across
  requests, and the PDF's own text of the region, so symbols, numbers and
  names are copied right.
"""

from __future__ import annotations

import logging
import re

from openai import AuthenticationError, PermissionDeniedError

from . import prompts
from .config import Settings
from .layout import Segment
from .llm import InsufficientQuotaError, ModelNotAvailableError, OpenAIClient
from .markdown import reference_list, text_for_summary
from .progress import ProgressFactory, console_progress
from .terms import mentioned

logger = logging.getLogger(__name__)

# Errors that would fail every request in the same way: abort the paper.
FATAL_ERRORS = (AuthenticationError, PermissionDeniedError, InsufficientQuotaError, ModelNotAvailableError)


def strip_markdown_fence(text: str) -> str:
    """Remove a ```markdown fence wrapped around the whole answer (code blocks are kept)."""
    match = re.fullmatch(r"\s*```(?:markdown|md)[ \t]*\n(.*?)\n\s*```\s*", text, flags=re.S | re.I)
    return match.group(1) if match else text


def is_empty_answer(text: str) -> bool:
    """The model said the image has nothing to translate (e.g. an empty crop)."""
    return text.strip().strip("`").strip() == prompts.EMPTY_MARKER


def failure_note(error: BaseException) -> str:
    return f"> ⚠️ この部分の翻訳に失敗しました（{type(error).__name__}）。"


def printed_markdown(segment: Segment) -> str | None:
    """Markdown of a title, heading or reference list copied from the PDF; None if it must be read from images."""
    if segment.kind in ("title", "heading") and segment.source_text:
        return f"{'#' * segment.level} {segment.source_text}"
    if segment.kind == "reference" and segment.entries:
        return reference_list(segment.entries)
    return None


_LONE_MARK_LINE_RE = re.compile(r"^[ \t]*(?:\\?\*|[∗†‡§¶])+[ \t]*(?:\n|$)", re.M)
_LEADING_ASTERISK_RE = re.compile(r"^\*(?!\*)[ \t]*(?=\S)")  # "* text" would be a list item
_LEADING_MARK_SPACE_RE = re.compile(r"^((?:\\\*|[∗†‡§¶])+)[ \t]+(?=\S)")  # "† Work" -> "†Work", as printed


def clean_translation(segment: Segment, answer: str) -> str:
    r"""
    The model's translation of a text segment, tidied.

    A line with only a footnote mark (``*``, ``†``) carries nothing; author
    blocks keep their lines (name, affiliation, e-mail) as line breaks.

    >>> def text(label):
    ...     return Segment("text", label, [label], [], [], [])
    >>> clean_translation(text("fnote"), "†Google Brain在籍中。  \n*")
    '†Google Brain在籍中。'
    >>> clean_translation(text("author"), "A. Author*\nGoogle Brain\n\nB. Author")
    'A. Author*  \nGoogle Brain\n\nB. Author'

    A footnote that starts with an asterisk keeps it as a mark, not a list bullet:

    >>> note = Segment("text", "fnote", ["fnote"], [], [], [], layer_text="∗Equal contribution.")
    >>> clean_translation(note, "* 同等の貢献である。")
    '\\*同等の貢献である。'
    >>> clean_translation(text("fnote"), "† Google Brain在籍中に行われた研究である。")
    '†Google Brain在籍中に行われた研究である。'
    """
    text = _LONE_MARK_LINE_RE.sub("", strip_markdown_fence(answer).strip())
    if segment.layer_text.lstrip().startswith(("∗", "*", "＊")):
        text = _LEADING_ASTERISK_RE.sub(r"\\*", text)
    text = _LEADING_MARK_SPACE_RE.sub(r"\1", text)
    if segment.first_label == "author":
        text = re.sub(r"(?<=\S)[ \t]*\n(?=[ \t]*\S)", "  \n", text)
    return re.sub(r"[ \t]+(?=\n\n|\n?$)", "", text.strip())


def _squash(text: str) -> str:
    return re.sub(r"[\W_]+", "", text).lower()


def drop_repeated_heading(text: str, section: str) -> str:
    r"""
    A text block's translation without a first line that repeats the section heading
    (the request names the section, and the heading is a segment of its own).

    >>> drop_repeated_heading("### 5.2 Hardware and Schedule\n\nモデルは", "5.2 Hardware and Schedule")
    'モデルは'
    >>> drop_repeated_heading("### 5.3 Optimizer\n\n本文", "5.2 Hardware and Schedule")
    '### 5.3 Optimizer\n\n本文'
    """
    first, _, rest = text.partition("\n")
    if section and re.match(r"#{1,6}\s", first) and _squash(first.lstrip("#")) == _squash(section):
        return rest.lstrip("\n")
    return text


def _one_line(text: str) -> str:
    lines = [line.strip() for line in strip_markdown_fence(text).splitlines() if line.strip()]
    return re.sub(r"^#+\s*", "", lines[0]) if lines else ""


def _transcribed(segment: Segment, answer: str) -> str:
    """The model's transcription of a title, heading or reference list, as Markdown."""
    if segment.kind in ("title", "heading"):
        text = _one_line(answer)
        return f"{'#' * segment.level} {text}" if text else ""
    lines = strip_markdown_fence(answer).strip().splitlines()
    if lines and re.match(r"^#{1,6}\s", lines[0]):  # the heading is a segment of its own
        lines = lines[1:]
    return "\n".join(lines).strip()


def segment_request(
    segment: Segment, settings: Settings, title: str = "", section: str = "", glossary: prompts.Glossary | None = None
) -> dict | None:
    """The request for a segment, or None when nothing is sent (figures, printed text, no crop)."""
    if segment.kind == "figure" or printed_markdown(segment) is not None or not segment.images:
        return None
    if segment.kind == "title":
        return prompts.title_request(segment.images, settings)
    if segment.kind == "heading":
        return prompts.heading_request(segment.images, settings)
    if segment.kind == "reference":
        return prompts.reference_request(segment.images, settings)
    return prompts.translation_request(
        segment.images, settings, title=title, section=section, layer_text=segment.layer_text, glossary=glossary
    )


def translate_segments(
    segments: list[Segment],
    client: OpenAIClient,
    settings: Settings,
    progress: ProgressFactory = console_progress,
    glossary: prompts.Glossary | None = None,
) -> tuple[dict[int, str], int]:
    """Return ({segment index: Markdown}, number of failed segments).

    Figures are not handled here.  A failed request does not stop the others;
    its segment gets a visible note instead.  Errors that would fail every
    request (invalid key, no quota, unknown model) are raised.
    """
    parts: dict[int, str] = {}
    requests, owners, sections = [], [], {}
    title = section = ""
    for index, segment in enumerate(segments):
        if segment.kind == "title":
            title = segment.source_text or title
        elif segment.kind == "heading":
            section = segment.source_text or section
        printed = printed_markdown(segment)
        if printed is not None:
            parts[index] = printed
        elif (
            request := segment_request(segment, settings, title, section, mentioned(glossary or [], segment.english))
        ) is not None:
            requests.append(request)
            owners.append(index)
            sections[index] = section

    results: list = []
    if requests:
        with progress(len(requests), "翻訳中") as bar:
            results = client.generate_batch(requests, on_result=lambda _i, _r: bar.update(1), return_exceptions=True)
    for result in results:
        if isinstance(result, FATAL_ERRORS):
            raise result

    failed = 0
    for index, result in zip(owners, results, strict=True):
        segment = segments[index]
        if isinstance(result, BaseException):
            failed += 1
            parts[index] = failure_note(result)
        elif is_empty_answer(result):
            parts[index] = ""
        elif segment.kind in ("title", "heading", "reference"):
            parts[index] = _transcribed(segment, result)
        else:
            parts[index] = drop_repeated_heading(clean_translation(segment, result), sections[index])
    return parts, failed


def summarize(markdown: str, client: OpenAIClient, settings: Settings) -> str:
    """Japanese summary of the translated paper (a note instead of failing on errors)."""
    try:
        return client.generate(prompts.summary_request(text_for_summary(markdown), settings)).strip()
    except FATAL_ERRORS:
        raise
    except Exception as e:
        logger.exception("Summary generation failed")
        return f"（要約の生成に失敗しました: {type(e).__name__}）"
