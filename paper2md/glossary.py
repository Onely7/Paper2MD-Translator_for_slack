"""
The glossary of a paper's terms, made before the translation.

Every block is translated by its own request, so one term can come out in
several ways (注意機構 / アテンション / attention, エンコーダ / エンコーダー).
One request first reads the paper's English text (the PDF text layer, or
Dolphin-v2's text on scanned pages) and fixes one rendering for each main
term; the model chosen for 訳語表 in the Home tab makes it (``Settings.glossary_model``).
Terms of the lab's term list take its rendering; other terms stay in English
(the prompt says so, and the translation prompt keeps terms that are not in
the glossary in English).  With the lab's term list (``terms.py``), the request also gets the
list's rows whose English appears in the paper, and the model takes the
list's rendering for the terms it has, so that every paper uses the same
words.  Each translation and polishing request then gets the glossary
entries whose English appears in its block (``terms.mentioned``).  Without
the glossary the translation still runs.
"""

from __future__ import annotations

import json
import logging

from . import prompts
from .config import Settings
from .layout import Segment
from .llm import OpenAIClient
from .prompts import Glossary
from .terms import TermList, named_phrases
from .translation import FATAL_ERRORS

logger = logging.getLogger(__name__)

MAX_INPUT_CHARS = 60_000  # the beginning of a long paper is enough to find its terms
MAX_TERMS = 350  # the prompt asks for at most 300: every term of the list found in the paper
MAX_TERM_CHARS = 80


def paper_text(segments: list[Segment]) -> str:
    """The English text of the title, headings and text blocks (not the references), in order."""
    parts = []
    for segment in segments:
        if segment.kind in ("title", "heading"):
            parts.append(segment.source_text)
        elif segment.kind == "text":
            parts.append(segment.english)
    return "\n\n".join(part.strip() for part in parts if part and part.strip())[:MAX_INPUT_CHARS]


def parse_glossary(answer: str) -> Glossary:
    """
    The terms of the model's JSON answer: one rendering per term, the first one wins.

    >>> parse_glossary('{"terms": [{"en": "attention", "ja": "注意機構"}, {"en": "Attention", "ja": "アテンション"},'
    ...                ' {"en": "BLEU", "ja": "BLEU"}, {"en": "", "ja": "x"}]}')
    [('attention', '注意機構'), ('BLEU', 'BLEU')]
    """
    items = json.loads(answer).get("terms")
    glossary: Glossary = []
    seen = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        en, ja = (" ".join(str(item.get(key) or "").split()) for key in ("en", "ja"))
        if en and ja and len(en) <= MAX_TERM_CHARS and len(ja) <= MAX_TERM_CHARS and en.lower() not in seen:
            seen.add(en.lower())
            glossary.append((en, ja))
    return glossary[:MAX_TERMS]


def build_glossary(
    segments: list[Segment],
    client: OpenAIClient,
    settings: Settings,
    title: str = "",
    term_list: TermList | None = None,
) -> Glossary:
    """The glossary of the paper ([] when it has no readable text or the request fails)."""
    text = paper_text(segments)
    if not text:
        return []
    candidates = ""
    if term_list:
        names = named_phrases(text, [s.source_text for s in segments if s.kind == "heading" and s.source_text])
        candidates = term_list.candidate_lines(text, names=names)
    try:
        return parse_glossary(client.generate(prompts.glossary_request(text, settings, title, candidates)))
    except FATAL_ERRORS:
        raise
    except Exception as e:
        logger.warning("No glossary for this paper: %s: %s", type(e).__name__, e)
        return []


def glossary_records(glossary: Glossary, term_list: TermList | None = None) -> list[dict]:
    """
    The glossary as saved in glossary.json; ``term_list``: the list's renderings of a term it has.

    >>> glossary_records([("encoder", "エンコーダ")])
    [{'en': 'encoder', 'ja': 'エンコーダ'}]
    """
    records = []
    for en, ja in glossary:
        listed = term_list.renderings(en) if term_list else []
        records.append({"en": en, "ja": ja, **({"term_list": listed} if listed else {})})
    return records
