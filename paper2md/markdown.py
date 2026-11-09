"""
Post-processing of the translated Markdown and the Slack summary.

``finalize_markdown`` normalizes the concatenated model outputs with mdformat.
Math (``$...$``, ``$$...$$``, ``\\[...\\]``) and code are swapped for placeholders
first, because mdformat escapes their characters (``\\max`` -> ``\\\\max``,
``\\,`` -> ``,``, ``*`` -> ``\\*``) and would break the LaTeX.  Then only
lines that are headings are normalized:

* numbered headings get the level of their numbering
  (``2`` -> ``##``, ``2.1`` -> ``###``, ``2.1.3`` -> ``####``),
* Abstract / References / Appendix / Acknowledgments become ``##``,
* a line made only of a bold numbered heading (``**3.1 Training**``) becomes a
  heading; when text follows it, the text stays a separate paragraph.

The title (``#``) and all other lines (paragraphs, lists, code) are left alone.
"""

from __future__ import annotations

import re

import mdformat

_PLACEHOLDER = "P2MPROTECTED{:04d}X"
_PLACEHOLDER_RE = re.compile(r"P2MPROTECTED(\d{4})X")

_FENCE_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^(?P=indent)(?P=fence)[ \t]*$", re.M | re.S
)
_DISPLAY_MATH_RE = re.compile(r"\$\$.+?\$\$|\\\[.+?\\\]", re.S)
_INLINE_CODE_RE = re.compile(r"(`+)(?!`).+?(?<!`)\1")
_INLINE_MATH_RE = re.compile(r"(?<![\\$])\$(?=\S)(?:[^$\n\\]|\\.)+?(?<=\S)\$(?!\$)")

_KEYWORDS = {
    "abstract": "Abstract",
    "reference": "Reference",
    "references": "References",
    "bibliography": "Bibliography",
    "appendix": "Appendix",
    "appendices": "Appendices",
    "acknowledgment": "Acknowledgment",
    "acknowledgments": "Acknowledgments",
    "acknowledgement": "Acknowledgement",
    "acknowledgements": "Acknowledgements",
}
# 2 / 2.1 / 2.1.3 / 2.1.3.4, A.1 / A.1.2, A. / II.  (a lone letter or numeral needs a dot)
_SECTION_NUMBER_RE = re.compile(
    r"^(?P<num>\d{1,2}(?:\.\d{1,2}){0,3}|[A-Z](?:\.\d{1,2}){1,3}|[A-Z](?=\.)|[IVX]{1,4}(?=\.))\.?\s+(?P<title>\S.*)$"
)
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*#*\s*$")
_BOLD_LINE_RE = re.compile(r"^(?P<em>\*\*|__)(?P<inner>.+?)(?P=em)(?P<rest>.*)$")


def _protect(markdown: str) -> tuple[str, list[str]]:
    saved: list[str] = []

    def stash(match: re.Match, block: bool = False) -> str:
        saved.append(match.group(0))
        token = _PLACEHOLDER.format(len(saved) - 1)
        return f"\n\n{token}\n\n" if block else token

    markdown = _FENCE_RE.sub(lambda m: stash(m, block=True), markdown)
    markdown = _DISPLAY_MATH_RE.sub(lambda m: stash(m, block="\n" in m.group(0)), markdown)
    markdown = _INLINE_CODE_RE.sub(stash, markdown)
    markdown = _INLINE_MATH_RE.sub(stash, markdown)
    return markdown, saved


def _restore(markdown: str, saved: list[str]) -> str:
    # restore repeatedly: a placeholder may contain another one
    for _ in range(3):
        markdown, count = _PLACEHOLDER_RE.subn(lambda m: saved[int(m.group(1))], markdown)
        if not count:
            break
    return markdown


def _heading_for(text: str) -> str | None:
    """Normalized heading line for a heading text, or None to leave it unchanged."""
    text = re.sub(r"^(\*\*|__)(.+)\1$", r"\2", text.strip()).strip()
    keyword = _KEYWORDS.get(text.rstrip(":.").lower())
    if keyword:
        return f"## {keyword}"
    match = _SECTION_NUMBER_RE.match(text)
    if not match:
        return None
    number = match["num"].rstrip(".")
    depth = number.count(".") + 1
    return f"{'#' * min(depth + 1, 6)} {number}. {match['title'].strip()}"


def normalize_headings(markdown: str) -> str:
    out: list[str] = []
    title_seen = False
    for line in markdown.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            if len(heading["hashes"]) == 1 and not title_seen:  # the paper title
                title_seen = True
                out.append(line)
            else:
                out.append(_heading_for(heading["text"]) or line)
            continue

        stripped = line.strip()
        if stripped.rstrip(":.").lower() in _KEYWORDS:  # "Abstract" on its own line
            out.append(_heading_for(stripped))
            continue
        bold = _BOLD_LINE_RE.match(stripped)
        if bold:
            inner, rest = bold["inner"].strip(), bold["rest"].strip()
            numbered = _SECTION_NUMBER_RE.match(inner) is not None
            keyword = inner.rstrip(":.").lower() in _KEYWORDS
            if numbered or (keyword and not rest):
                out.append(_heading_for(inner))
                if rest:  # a run-in heading: keep the text as its own paragraph
                    out += ["", rest]
                continue
        out.append(line)
    return "\n".join(out)


def finalize_markdown(markdown: str) -> str:
    protected, saved = _protect(markdown)
    formatted = mdformat.text(protected)
    formatted = normalize_headings(formatted)
    formatted = mdformat.text(formatted)
    return _restore(formatted, saved).strip() + "\n"


def first_heading(markdown: str) -> str | None:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or None
    return None


# ---------------------------------------------------------------------------
# Summary for Slack
# ---------------------------------------------------------------------------
SUMMARY_SECTIONS = ("【背景】", "【目的】", "【手法】", "【実験方法】", "【実験結果】", "【考察】")


def summary_for_slack(summary: str) -> str:
    """
    Slack mrkdwn: bold section titles, "・" bullets, ``*bold*`` instead of ``**bold**``.

    >>> summary_for_slack("【背景】\\n- a **b**\\n    - c")
    '*【背景】*\\n・a *b*\\n    ・c'
    """
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", summary)
    for section in SUMMARY_SECTIONS:
        text = re.sub(rf"(?<!\*){re.escape(section)}(?!\*)", f"*{section}*", text)
    return re.sub(r"^(\s*)[-*] ", r"\1・", text, flags=re.M)
