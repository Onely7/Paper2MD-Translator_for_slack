"""
Segments to Markdown, and the summary.

* Title, headings and references are written as printed (``source_text`` /
  ``entries`` copied from the PDF); the model only transcribes them, without
  translating, when that text could not be read.
* Text segments are translated from their images.  Each request also gets the
  paper title and the current section heading, so terms are translated
  consistently across requests.
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


def segment_request(segment: Segment, settings: Settings, title: str = "", section: str = "") -> dict | None:
    """The request for a segment, or None when nothing is sent (figures, printed text, no crop)."""
    if segment.kind == "figure" or printed_markdown(segment) is not None or not segment.images:
        return None
    if segment.kind == "title":
        return prompts.title_request(segment.images, settings)
    if segment.kind == "heading":
        return prompts.heading_request(segment.images, settings)
    if segment.kind == "reference":
        return prompts.reference_request(segment.images, settings)
    return prompts.translation_request(segment.images, settings, title=title, section=section)


def translate_segments(
    segments: list[Segment],
    client: OpenAIClient,
    settings: Settings,
    progress: ProgressFactory = console_progress,
) -> tuple[dict[int, str], int]:
    """Return ({segment index: Markdown}, number of failed segments).

    Figures are not handled here.  A failed request does not stop the others;
    its segment gets a visible note instead.  Errors that would fail every
    request (invalid key, no quota, unknown model) are raised.
    """
    parts: dict[int, str] = {}
    requests, owners = [], []
    title = section = ""
    for index, segment in enumerate(segments):
        if segment.kind == "title":
            title = segment.source_text or title
        elif segment.kind == "heading":
            section = segment.source_text or section
        printed = printed_markdown(segment)
        if printed is not None:
            parts[index] = printed
        elif (request := segment_request(segment, settings, title, section)) is not None:
            requests.append(request)
            owners.append(index)

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
            parts[index] = strip_markdown_fence(result).strip()
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
