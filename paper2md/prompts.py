"""
Prompts and request builders for the OpenAI models.

The Japanese prompts live in ``prompt_texts/`` so they can be read and
proofread as plain text:

* ``translation.md``: instructions for translating the cropped page images,
* ``translation_input.md``: the text sent with the images (``{title}`` and
  ``{section}`` are filled in; a line whose value is unknown is left out),
* ``summary.md``: instructions for the summary.

The English prompts below only transcribe text that must not be translated
(the title, headings and references); they are used when the PDF has no
text layer and Dolphin-v2 could not read the text either.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Settings
from .models import lowest_effort

PROMPT_DIR = Path(__file__).with_name("prompt_texts")


def _load(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8").strip()


TRANSLATION_INSTRUCTIONS = _load("translation.md")
TRANSLATION_INPUT = _load("translation_input.md")
SUMMARY_INSTRUCTIONS = _load("summary.md")

EMPTY_MARKER = "[[EMPTY]]"  # the answer for an image without text to translate (see translation.md)
TRANSLATION_CACHE_KEY = "paper2md-translate-ja-v3"
SUMMARY_CACHE_KEY = "paper2md-summary-ja-v3"

TITLE_TRANSCRIPTION = (
    "Transcribe the paper title shown in the image exactly as printed. Do not translate or correct it. "
    "Output only the title on one line, without a leading '#'."
)
HEADING_TRANSCRIPTION = (
    "Transcribe the section heading shown in the image exactly as printed, including its number "
    '(for example "3.2 Training Details"). Do not translate or correct it. '
    "Output only the heading on one line, without a leading '#'."
)
REFERENCE_TRANSCRIPTION = (
    "The images show part of the reference list of a paper, in reading order. "
    "Transcribe every entry exactly as printed; do not translate, shorten, correct or reorder anything. "
    'Write each entry on one line as a Markdown list item starting with "- " and keep its label, such as [12]. '
    "Join the lines of an entry and remove hyphens that only split a word at a line break. "
    "Ignore running headers, footers and page numbers. Output only the list."
)


def translation_input(title: str = "", section: str = "") -> str:
    """
    The user text of a translation request.

    >>> print(translation_input("Attention Is All You Need", ""))
    論文: Attention Is All You Need
    <BLANKLINE>
    論文のページから切り出した画像です。指示に従って日本語に訳してください。
    """
    values = {"title": " ".join(title.split()), "section": " ".join(section.split())}
    lines = []
    for line in TRANSLATION_INPUT.splitlines():
        names = [name for name in values if f"{{{name}}}" in line]
        if names and not all(values[name] for name in names):
            continue
        for name in names:
            line = line.replace(f"{{{name}}}", values[name])
        lines.append(line)
    return "\n".join(lines).strip()


def translation_request(images: list[str], settings: Settings, title: str = "", section: str = "") -> dict:
    """Translate the cropped images of a segment into Japanese."""
    return {
        "text": translation_input(title, section),
        "images": images,
        "instructions": TRANSLATION_INSTRUCTIONS,
        "gen_kwargs": {
            "reasoning_effort": settings.translation_effort,
            "verbosity": "high",  # complete, literal output (OpenAI's advice for transcription)
            "image_detail": settings.image_detail,
            "prompt_cache_key": TRANSLATION_CACHE_KEY,
        },
    }


def _transcription_request(prompt: str, images: list[str], settings: Settings) -> dict[str, Any]:
    """Copy text from images: no reasoning needed, so the cheapest effort the model accepts."""
    return {
        "text": prompt,
        "images": images,
        "gen_kwargs": {
            "reasoning_effort": lowest_effort(settings.translation_model),
            "verbosity": "high",
            "image_detail": settings.image_detail,
        },
    }


def title_request(images: list[str], settings: Settings) -> dict:
    return _transcription_request(TITLE_TRANSCRIPTION, images, settings)


def heading_request(images: list[str], settings: Settings) -> dict:
    return _transcription_request(HEADING_TRANSCRIPTION, images, settings)


def reference_request(images: list[str], settings: Settings) -> dict:
    return _transcription_request(REFERENCE_TRANSCRIPTION, images, settings)


def summary_request(markdown: str, settings: Settings) -> dict:
    return {
        "text": markdown,
        "instructions": SUMMARY_INSTRUCTIONS,
        "gen_kwargs": {
            "reasoning_effort": settings.summary_effort,
            "verbosity": "medium",
            "prompt_cache_key": SUMMARY_CACHE_KEY,
        },
    }
