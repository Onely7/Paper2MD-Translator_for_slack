"""Translating segments and summarizing the result with the OpenAI models."""

from __future__ import annotations

import logging
import re

from openai import AuthenticationError, PermissionDeniedError

from . import prompts
from .config import Settings
from .layout import Segment
from .llm import InsufficientQuotaError, ModelNotAvailableError, OpenAIClient
from .progress import ProgressFactory, console_progress

logger = logging.getLogger(__name__)

# Errors that would fail every request in the same way: abort the paper.
FATAL_ERRORS = (AuthenticationError, PermissionDeniedError, InsufficientQuotaError, ModelNotAvailableError)


def strip_markdown_fence(text: str) -> str:
    """Remove a ```markdown fence wrapped around the whole answer (code blocks are kept)."""
    match = re.fullmatch(r"\s*```(?:markdown|md)[ \t]*\n(.*?)\n\s*```\s*", text, flags=re.S | re.I)
    return match.group(1) if match else text


def is_untranslatable(text: str) -> bool:
    """The model answered with the "could not translate" sentence (e.g. an empty crop)."""
    return prompts.UNTRANSLATABLE_SENTINEL in text and len(text) < 200


def failure_note(error: BaseException) -> str:
    return f"> ⚠️ この部分の翻訳に失敗しました（{type(error).__name__}）。"


def segment_request(segment: Segment, settings: Settings) -> dict | None:
    """The request for a segment, or None when nothing can be sent."""
    if segment.kind == "figure":
        return None
    if segment.kind == "title":
        if segment.text.strip():
            return prompts.title_request(segment.text, settings)
        request = prompts.title_image_request(segment.images, settings)
    elif segment.kind == "reference" and segment.has_all_texts:
        return prompts.reference_request(segment.text, settings)
    elif segment.kind == "reference":
        request = prompts.reference_image_request(segment.images, settings)
    else:
        request = prompts.translation_request(segment.images, settings)
    return request if request["images"] else None  # nothing could be cropped


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
    requests, owners = [], []
    for index, segment in enumerate(segments):
        if (request := segment_request(segment, settings)) is not None:
            requests.append(request)
            owners.append(index)

    results: list = []
    if requests:
        with progress(len(requests), "翻訳中") as bar:
            results = client.generate_batch(requests, on_result=lambda _i, _r: bar.update(1), return_exceptions=True)
    for result in results:
        if isinstance(result, FATAL_ERRORS):
            raise result

    parts: dict[int, str] = {}
    failed = 0
    for index, result in zip(owners, results, strict=True):
        if isinstance(result, BaseException):
            failed += 1
            parts[index] = failure_note(result)
        elif segments[index].kind == "text" and is_untranslatable(result):
            parts[index] = ""
        else:
            parts[index] = strip_markdown_fence(result).strip()
    return parts, failed


def summarize(markdown: str, client: OpenAIClient, settings: Settings) -> str:
    """Japanese summary of the translated paper (a note instead of failing on errors)."""
    try:
        return client.generate(prompts.summary_request(markdown, settings)).strip()
    except FATAL_ERRORS:
        raise
    except Exception as e:
        logger.exception("Summary generation failed")
        return f"（要約の生成に失敗しました: {type(e).__name__}）"
