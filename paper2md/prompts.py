"""
Prompts and request builders for the OpenAI models.

The Japanese prompts live in ``prompt_texts/`` so they can be read and
proofread as plain text:

* ``translation.md``: instructions for translating the cropped page images,
* ``translation_input.md``: the text sent with the images (``{title}``,
  ``{section}``, the glossary and the PDF's own text of the region are filled
  in; a line whose value is unknown is left out),
* ``glossary.md`` / ``glossary_input.md``: the glossary of the paper's terms,
  made before the translation so that separately translated blocks agree (with
  the rows of the lab's term list whose English appears in the paper),
* ``summary.md``: instructions for the summary,
* ``polish.md``: how yomiyasu is applied to one block; ``polish_document.md``
  / ``polish_document_only.md``: to the whole Markdown after / without the
  block pass (with ``polish_input.md`` / ``polish_document_input.md``).  The
  polishing instructions start with yomiyasu's own files (``YOMIYASU_FILES``,
  vendored unchanged in ``yomiyasu/``).

The English prompts below only transcribe text that must not be translated
(the title, headings and references); they are used when the PDF has no
text layer and Dolphin-v2 could not read the text either.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import REPO_ROOT, Settings
from .models import lowest_effort

PROMPT_DIR = Path(__file__).with_name("prompt_texts")
YOMIYASU_DIR = REPO_ROOT / "yomiyasu"
YOMIYASU_VERSION = "2.5.0"
# SKILL.md and the references it asks for with papers (doc type "paper" -> domain "tech"), plus the
# literal-translation patterns (calques.md), which matter most for a translation from English
YOMIYASU_FILES = ("SKILL.md", "references/doc-types/paper.md", "references/domains/tech.md", "references/calques.md")


def _load(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8").strip()


def _yomiyasu() -> str:
    files = "\n\n".join(
        f'<file path="{name}">\n{(YOMIYASU_DIR / name).read_text(encoding="utf-8").strip()}\n</file>'
        for name in YOMIYASU_FILES
    )
    return f"# yomiyasu v{YOMIYASU_VERSION} (https://github.com/nanaism/yomiyasu)\n\n{files}"


TRANSLATION_INSTRUCTIONS = _load("translation.md")
TRANSLATION_INPUT = _load("translation_input.md")
GLOSSARY_INSTRUCTIONS = _load("glossary.md")
GLOSSARY_INPUT = _load("glossary_input.md")
SUMMARY_INSTRUCTIONS = _load("summary.md")
YOMIYASU = _yomiyasu()
POLISH_INSTRUCTIONS = f"{YOMIYASU}\n\n{_load('polish.md')}"
POLISH_DOCUMENT_INSTRUCTIONS = f"{YOMIYASU}\n\n{_load('polish_document.md')}"  # after the block pass
POLISH_DOCUMENT_ONLY_INSTRUCTIONS = f"{YOMIYASU}\n\n{_load('polish_document_only.md')}"  # without it
POLISH_INPUT = _load("polish_input.md")
POLISH_DOCUMENT_INPUT = _load("polish_document_input.md")

EMPTY_MARKER = "[[EMPTY]]"  # the answer for an image without text to translate (see translation.md)
GLOSSARY_TAG = "訳語表"  # the tags the instructions refer to
LAYER_TEXT_TAG = "PDFの文字"
TERM_LIST_TAG = "用語の一覧"
TRANSLATION_CACHE_KEY = "paper2md-translate-ja-v4"
SUMMARY_CACHE_KEY = "paper2md-summary-ja-v3"
GLOSSARY_CACHE_KEY = "paper2md-glossary-ja-v2"
POLISH_CACHE_KEY = "paper2md-yomiyasu-v1"  # both polishing passes start with the same yomiyasu text
# the answer of the whole-paper pass: the rewritten paragraphs only
EDITS_FORMAT = {
    "type": "json_schema",
    "name": "paragraph_edits",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
                    "required": ["id", "text"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["edits"],
        "additionalProperties": False,
    },
}
# the answer of the glossary request
GLOSSARY_FORMAT = {
    "type": "json_schema",
    "name": "glossary",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "terms": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"en": {"type": "string"}, "ja": {"type": "string"}},
                    "required": ["en", "ja"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["terms"],
        "additionalProperties": False,
    },
}

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


Glossary = list[tuple[str, str]]  # (English term, Japanese rendering), in the glossary's order


def _fill(template: str, **values: str) -> str:
    """Fill ``{name}`` fields; a line whose value is empty is left out (and so are extra blank lines)."""
    lines = []
    for line in template.splitlines():
        names = [name for name in values if f"{{{name}}}" in line]
        if names and not all(values[name] for name in names):
            continue
        for name in names:
            line = line.replace(f"{{{name}}}", values[name])
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _tagged(tag: str, content: str) -> str:
    return f"<{tag}>\n{content.strip()}\n</{tag}>" if content.strip() else ""


def glossary_block(glossary: Glossary | None) -> str:
    """
    >>> print(glossary_block([("attention", "注意機構"), ("BLEU", "BLEU")]))
    <訳語表>
    - attention: 注意機構
    - BLEU: BLEU
    </訳語表>
    """
    return _tagged(GLOSSARY_TAG, "\n".join(f"- {en}: {ja}" for en, ja in glossary or []))


def translation_input(
    title: str = "", section: str = "", layer_text: str = "", glossary: Glossary | None = None
) -> str:
    """
    The user text of a translation request.

    >>> print(translation_input("Attention Is All You Need", ""))
    論文: Attention Is All You Need
    <BLANKLINE>
    論文のページから切り出した画像です。指示に従って日本語に訳してください。
    >>> print(translation_input("T", "1 Intro", "†Work performed at Google."))
    論文: T
    節: 1 Intro
    <BLANKLINE>
    <PDFの文字>
    †Work performed at Google.
    </PDFの文字>
    <BLANKLINE>
    論文のページから切り出した画像です。指示に従って日本語に訳してください。
    """
    return _fill(
        TRANSLATION_INPUT,
        title=_one_line(title),
        section=_one_line(section),
        glossary=glossary_block(glossary),
        layer_text=_tagged(LAYER_TEXT_TAG, layer_text),
    )


def polish_input(body: str, title: str = "", section: str = "", glossary: Glossary | None = None) -> str:
    """
    The user text of a polishing request for one block.

    >>> print(polish_input("訳文。", "Attention Is All You Need", "3.1 Encoder"))
    論文: Attention Is All You Need
    節: 3.1 Encoder
    <BLANKLINE>
    <本文>
    訳文。
    </本文>
    """
    return _fill(
        POLISH_INPUT,
        title=_one_line(title),
        section=_one_line(section),
        glossary=glossary_block(glossary),
        body=body.strip(),
    )


def translation_request(
    images: list[str],
    settings: Settings,
    title: str = "",
    section: str = "",
    layer_text: str = "",
    glossary: Glossary | None = None,
) -> dict:
    """Translate the cropped images of a segment into Japanese (with the PDF's own text of them, if any)."""
    return {
        "text": translation_input(title, section, layer_text, glossary),
        "images": images,
        "instructions": TRANSLATION_INSTRUCTIONS,
        "gen_kwargs": {
            "reasoning_effort": settings.translation_effort,
            "verbosity": "high",  # complete, literal output (OpenAI's advice for transcription)
            "image_detail": settings.image_detail,
            "prompt_cache_key": TRANSLATION_CACHE_KEY,
        },
    }


def glossary_request(text: str, settings: Settings, title: str = "", candidates: str = "") -> dict:
    """
    Make the glossary of the paper's terms from its text (the answer is JSON); the model and effort
    chosen for 訳語表 (``Settings.glossary_model`` / ``glossary_effort``) make it.

    ``candidates``: the lines of the term list's rows found in the paper (``TermList.candidate_lines``).

    >>> request = glossary_request("The encoder ...", Settings(), "T", "- encoder: エンコーダ［深層学習、8冊］")
    >>> print(request["text"])
    論文: T
    <BLANKLINE>
    <本文>
    The encoder ...
    </本文>
    <BLANKLINE>
    <用語の一覧>
    - encoder: エンコーダ［深層学習、8冊］
    </用語の一覧>
    >>> "用語の一覧" in glossary_request("The encoder ...", Settings())["text"]
    False
    """
    return {
        "text": _fill(
            GLOSSARY_INPUT, title=_one_line(title), text=text.strip(), candidates=_tagged(TERM_LIST_TAG, candidates)
        ),
        "instructions": GLOSSARY_INSTRUCTIONS,
        "gen_kwargs": {
            "reasoning_effort": settings.glossary_effort,
            "text": {"format": GLOSSARY_FORMAT},
            "prompt_cache_key": GLOSSARY_CACHE_KEY,
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


def polish_request(
    body: str, settings: Settings, title: str = "", section: str = "", glossary: Glossary | None = None
) -> dict:
    """Polish one translated block with yomiyasu (the answer is the body in <本文> tags)."""
    return {
        "text": polish_input(body, title, section, glossary),
        "instructions": POLISH_INSTRUCTIONS,
        "gen_kwargs": {"reasoning_effort": settings.polish_effort, "prompt_cache_key": POLISH_CACHE_KEY},
    }


def polish_document_request(
    document: str,
    settings: Settings,
    title: str = "",
    glossary: Glossary | None = None,
    after_blocks: bool = False,
) -> dict:
    """Polish the whole Markdown with yomiyasu (the answer is JSON: the rewritten paragraphs).

    ``after_blocks``: every block was polished already, so only what shows across paragraphs is left.
    """
    return {
        "text": _fill(
            POLISH_DOCUMENT_INPUT, title=_one_line(title), glossary=glossary_block(glossary), document=document.strip()
        ),
        "instructions": POLISH_DOCUMENT_INSTRUCTIONS if after_blocks else POLISH_DOCUMENT_ONLY_INSTRUCTIONS,
        "gen_kwargs": {
            "reasoning_effort": settings.polish_effort,
            "text": {"format": EDITS_FORMAT},
            "prompt_cache_key": POLISH_CACHE_KEY,
        },
    }


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
