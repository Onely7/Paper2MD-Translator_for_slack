"""
Post-processing of the translated Markdown and the Slack summary.

``finalize_markdown`` turns the concatenated segments into one document:

1. ``normalize_math`` rewrites the math of the model outputs into the form
   Markdown renderers (GitHub, VS Code, Obsidian, Typora) display:
   ``$...$`` inline and ``$$`` blocks on their own lines.  ``\\( \\)`` and
   ``\\[ \\]`` delimiters, bare ``\\begin{align}`` environments, spaces inside
   ``$ x $``, Markdown escapes inside math (``x\\_i``), doubled backslashes and
   ``\\quad (3)`` equation numbers are fixed; a space is put between inline
   math and Japanese text.  The underscore of a name (``step\\_num``) stays
   escaped, otherwise it would render as a subscript.
2. Only heading lines are adjusted: numbered headings get the level of their
   numbering (``2`` -> ``##``, ``2.1`` -> ``###``, ``2.1.3`` -> ``####``) and
   Abstract / References / Appendix / Acknowledgments become ``##``; the
   heading text is kept as printed.  The title (``#``) and all other lines
   (paragraphs, lists, code) are left alone.

``normalize_text`` writes section references as ``Section 3.2`` and puts a
space between Japanese and English words, numbers, inline code, inline math
and URLs (not in code blocks, display math, HTML tags or the reference list).

``format_markdown`` is the last step: prettier (``tools/prettier``, run with
the Node.js of the ``nodejs-wheel`` package) formats the file, or mdformat when
prettier is not installed.  Math and code are swapped for placeholders while
formatting, so their characters are never escaped, and line breaks are written
as two trailing spaces: a trailing ``\\`` is shown as a stray backslash by
renderers that do not follow CommonMark.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

import mdformat

from .config import REPO_ROOT

logger = logging.getLogger(__name__)

PRETTIER = REPO_ROOT / "tools" / "prettier" / "node_modules" / "prettier" / "bin" / "prettier.cjs"
PRETTIER_TIMEOUT = 120  # seconds

_FENCE_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^(?P=indent)(?P=fence)[ \t]*$", re.M | re.S
)
_DISPLAY_MATH_RE = re.compile(r"\$\$.+?\$\$", re.S)
_INLINE_CODE_RE = re.compile(r"(`+)(?!`).+?(?<!`)\1")
_INLINE_MATH_RE = re.compile(r"(?<![\\$])\$(?=\S)(?:[^$\n\\]|\\.)+?(?<=\S)\$(?!\$)")

_KEYWORDS = {
    "abstract",
    "reference",
    "references",
    "bibliography",
    "appendix",
    "appendices",
    "acknowledgment",
    "acknowledgments",
    "acknowledgement",
    "acknowledgements",
}
# 2 / 2.1 / 2.1.3 / 2.1.3.4, A.1 / A.1.2, A. / II.  (a lone letter or numeral needs a dot)
_SECTION_NUMBER_RE = re.compile(
    r"^(?P<num>\d{1,2}(?:\.\d{1,2}){0,3}|[A-Z](?:\.\d{1,2}){1,3}|[A-Z](?=\.)|[IVX]{1,4}(?=\.))\.?\s+(?P<title>\S.*)$"
)
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*#*\s*$")
_BOLD_LINE_RE = re.compile(r"^(?P<em>\*\*|__)(?P<inner>.+?)(?P=em)(?P<rest>.*)$")


# ---------------------------------------------------------------------------
# Placeholders
# ---------------------------------------------------------------------------
def _stash(markdown: str, patterns: list[tuple[re.Pattern, bool]], saved: list[str], tag: str = "A") -> str:
    """Replace the matches of ``patterns`` with ``P2M<tag><n>X`` tokens (blocks get their own paragraph)."""

    def stash(match: re.Match, block: bool) -> str:
        saved.append(match.group(0))
        token = f"P2M{tag}{len(saved) - 1:04d}X"
        return f"\n\n{token}\n\n" if block else token

    for pattern, block in patterns:
        markdown = pattern.sub(lambda m, block=block: stash(m, block and "\n" in m.group(0)), markdown)
    return markdown


def _restore(markdown: str, saved: list[str], tag: str = "A") -> str:
    token = re.compile(f"P2M{tag}(\\d{{4}})X")
    # restore repeatedly: a placeholder may contain another one
    for _ in range(3):
        markdown, count = token.subn(lambda m: saved[int(m.group(1))], markdown)
        if not count:
            break
    return markdown


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------
_PAREN_MATH_RE = re.compile(r"\\\((.+?)\\\)", re.S)
_BRACKET_MATH_RE = re.compile(r"\\\[(.+?)\\\]", re.S)
_BARE_ENV_RE = re.compile(
    r"\\begin\{(equation|align|alignat|gather|multline|flalign|eqnarray)(\*?)\}.*?\\end\{\1\2\}", re.S
)
_LOOSE_INLINE_RE = re.compile(r"(?<![\\$])\$(?!\$)([^$\n]+?)(?<!\\)\$(?!\$)")
_MATHY_RE = re.compile(r"[\\^_{}=]")
_TEXT_GROUP_RE = re.compile(r"\\(?:text|textrm|textit|textbf|texttt|mathrm|operatorname|mbox)\s*\{")
_EQUATION_NUMBER_RE = re.compile(r"\s*(?:\\q?quad|\\hfill|\\;|~)\s*\(\s*(\d{1,3}[a-z]?|[A-Z]\.?\d{1,3})\s*\)\s*$")
_CJK = r"\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"


def _outside_text_groups(content: str, fix) -> str:
    """Apply ``fix`` to the parts of ``content`` outside ``\\text{...}``-like groups."""
    parts, position = [], 0
    for match in _TEXT_GROUP_RE.finditer(content):
        if match.start() < position:
            continue
        depth, end = 1, match.end()
        while end < len(content) and depth:
            depth += {"{": 1, "}": -1}.get(content[end], 0)
            end += 1
        parts += [fix(content[position : match.start()]), content[match.start() : end]]
        position = end
    parts.append(fix(content[position:]))
    return "".join(parts)


# An underscore between two words of two or more letters belongs to a name (step_num, warmup_steps):
# escaped (step\_num) it is kept, bare it gets escaped.  Elsewhere "\_" is a needlessly escaped subscript.
_ESCAPED_NAME_UNDERSCORE_RE = re.compile(r"(?<![\\A-Za-z])([A-Za-z]{2,})\\_(?=[A-Za-z]{2,})")
_BARE_NAME_UNDERSCORE_RE = re.compile(r"(?<![\\A-Za-z])([A-Za-z]{2,})_(?=[A-Za-z]{2,})")


def _repair_escapes(part: str) -> str:
    part = _ESCAPED_NAME_UNDERSCORE_RE.sub(lambda m: m.group(1) + "\0", part)
    part = re.sub(r"\\([_*])", r"\1", part)
    part = _BARE_NAME_UNDERSCORE_RE.sub(lambda m: m.group(1) + "\0", part)
    return part.replace("\0", "\\_")


def clean_math(content: str, display: bool = False) -> str:
    """
    Repair what models often get wrong inside math.

    >>> clean_math(r"\\max\\_{z} p(x\\_i \\* y)")
    '\\\\max_{z} p(x_i * y)'
    >>> clean_math(r"\\min(step\\_num^{-0.5}, warmup_steps) + lr\\_t")    # names keep their underscore
    '\\\\min(step\\\\_num^{-0.5}, warmup\\\\_steps) + lr_t'
    >>> clean_math(r"\\\\frac{a}{b} + \\\\alpha")    # every backslash doubled
    '\\\\frac{a}{b} + \\\\alpha'
    >>> clean_math(r"y = x \\qquad (3)", display=True)
    'y = x \\\\tag{3}'
    """
    content = content.strip()
    # doubled backslashes everywhere ("\\frac", "\\alpha") but no single command: halve them
    if re.search(r"\\\\[A-Za-z]", content) and not re.search(r"\\[A-Za-z]", content.replace("\\\\", "")):
        content = content.replace("\\\\", "\\")
    content = _outside_text_groups(content, _repair_escapes)
    if display and "\\tag" not in content:
        content = _EQUATION_NUMBER_RE.sub(lambda m: f" \\tag{{{m.group(1)}}}", content)
    return content


def _display_or_inline(match: re.Match, text: str, body: str) -> str:
    """``$$...$$`` written inside a sentence stays inline; otherwise it becomes a block."""
    line_start = text.rfind("\n", 0, match.start()) + 1
    line_end = text.find("\n", match.end())
    line_end = len(text) if line_end < 0 else line_end
    inline = "\n" not in body and (text[line_start : match.start()].strip() or text[match.end() : line_end].strip())
    if inline:
        return f"${clean_math(body)}$"
    return f"\n\n$$\n{clean_math(body, display=True)}\n$$\n\n"


def normalize_math(markdown: str) -> str:
    r"""
    Rewrite math into ``$...$`` and ``$$`` blocks (code is not touched).

    >>> normalize_math(r"式 \(x\_i\) と \[ y = x \]")
    '式 $x_i$ と\n\n$$\ny = x\n$$\n'
    >>> normalize_math("損失は$ L = \\sum_i l_i $である。")
    '損失は $L = \\sum_i l_i$ である。'
    >>> normalize_math("費用は $5 と $10 です。")
    '費用は $5 と $10 です。'
    """
    saved: list[str] = []
    text = _stash(markdown, [(_FENCE_RE, True), (_INLINE_CODE_RE, False)], saved, "C")

    def block(body: str) -> str:
        return f"\n\n$$\n{clean_math(body, display=True)}\n$$\n\n"

    text = _BRACKET_MATH_RE.sub(lambda m: block(m.group(1)), text)
    text = _PAREN_MATH_RE.sub(lambda m: f"${' '.join(m.group(1).split())}$", text)

    # bare \begin{align}...\end{align} outside $$: make it a block (existing $$ blocks are set aside first)
    blocks: list[str] = []
    text = _stash(text, [(_DISPLAY_MATH_RE, False)], blocks, "D")
    text = _BARE_ENV_RE.sub(lambda m: block(m.group(0)), text)
    text = _restore(text, blocks, "D")

    text = _DISPLAY_MATH_RE.sub(lambda m: _display_or_inline(m, text, m.group(0)[2:-2]), text)

    def inline(match: re.Match) -> str:
        body = match.group(1)
        if body != body.strip() and body.strip()[:1].isdigit() and not _MATHY_RE.search(body):
            return match.group(0)  # "$5 and $10": amounts of money, not math
        return f"${clean_math(body)}$"

    blocks = []
    text = _stash(text, [(_DISPLAY_MATH_RE, False)], blocks, "E")
    text = _LOOSE_INLINE_RE.sub(inline, text)
    text = re.sub(rf"(?<=[{_CJK}])(?={_INLINE_MATH_RE.pattern})", " ", text)
    text = re.sub(rf"({_INLINE_MATH_RE.pattern})(?=[{_CJK}])", r"\1 ", text)
    text = _restore(text, blocks, "E")

    text = re.sub(r"[ \t]+(?=\n\n)", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return _restore(text, saved, "C").strip("\n") + ("\n" if text.endswith("\n") else "")


# ---------------------------------------------------------------------------
# Headings
# ---------------------------------------------------------------------------
def _heading_for(text: str) -> str | None:
    """Heading line at the level of its numbering or keyword, or None to leave it unchanged."""
    text = re.sub(r"^(\*\*|__)(.+)\1$", r"\2", text.strip()).strip()
    if text.rstrip(":.").lower() in _KEYWORDS:
        return f"## {text}"
    match = _SECTION_NUMBER_RE.match(text)
    if not match:
        return None
    depth = match["num"].rstrip(".").count(".") + 1
    return f"{'#' * min(depth + 1, 6)} {text}"


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


# ---------------------------------------------------------------------------
# Whole document
# ---------------------------------------------------------------------------
_PROTECTED_PATTERNS = [(_FENCE_RE, True), (_DISPLAY_MATH_RE, True), (_INLINE_CODE_RE, False), (_INLINE_MATH_RE, False)]
_BACKSLASH_BREAK_RE = re.compile(r"(?<!\\)\\\n(?=[^\n])")


def finalize_markdown(markdown: str) -> str:
    """The segments as one document, with math and headings normalized (``format_markdown`` comes last)."""
    saved: list[str] = []
    protected = _stash(normalize_math(markdown), _PROTECTED_PATTERNS, saved)
    protected = re.sub(r"\n{3,}", "\n\n", normalize_headings(protected))
    return _restore(protected, saved).strip() + "\n"


def node_executable() -> str | None:
    """The Node.js of the nodejs-wheel package (installed by ``uv sync``), else ``node`` on PATH."""
    try:
        import nodejs_wheel
    except ImportError:
        return shutil.which("node")
    binary = Path(nodejs_wheel.__file__).parent / "bin" / "node"
    return str(binary) if binary.is_file() else shutil.which("node")


def prettier(markdown: str) -> str | None:
    """``markdown`` formatted by prettier, or None when prettier is not installed or fails."""
    node = node_executable()
    if node is None or not PRETTIER.is_file():
        return None
    command = [node, str(PRETTIER), "--parser", "markdown", "--no-config", "--no-editorconfig"]
    try:
        done = subprocess.run(
            command, input=markdown, capture_output=True, text=True, encoding="utf-8", timeout=PRETTIER_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("prettier could not run: %s", e)
        return None
    if done.returncode != 0:
        logger.warning("prettier failed: %s", done.stderr.strip()[:500])
        return None
    return done.stdout


def format_markdown(markdown: str) -> str:
    r"""
    Format the final Markdown with prettier (mdformat when prettier is missing); math and code are not touched.

    >>> format_markdown("* a $x\\_i * y$\n* b\\\nc")
    '- a $x\\_i * y$\n- b  \n  c\n'
    """
    saved: list[str] = []
    protected = _stash(markdown, _PROTECTED_PATTERNS, saved)
    formatted = prettier(protected)
    if formatted is None:
        logger.warning("prettier is not available (see README); formatting with mdformat")
        formatted = mdformat.text(protected)
    formatted = _BACKSLASH_BREAK_RE.sub("  \n", formatted)  # "a\" + newline is a line break: write it as "a  "
    return _restore(formatted, saved).strip() + "\n"


# ---------------------------------------------------------------------------
# Section references and spaces between Japanese and English
# ---------------------------------------------------------------------------
_JA = "\u3041-\u3096\u30a1-\u30fa\u30fc\u3005\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"  # kana, ー, 々, kanji
_LATIN = "A-Za-z\u00c0-\u024f"
_HTML_TAG_RE = re.compile(r"<[A-Za-z/][^<>\n]*>")
_URL_RE = re.compile(rf"https?://[^\s<>()（）「」、。{_JA}]+")
_REFERENCE_LIST_RE = re.compile(
    r"^#{1,2}[ \t]+(?:references?|bibliography)[ \t]*$.*?(?=^#{1,2}[ \t]|\Z)", re.M | re.S | re.I
)
_SECTION_REFS = (
    (re.compile(r"第\s*(\d+(?:\.\d+)*)\s*節"), r"Section \1"),
    (re.compile(r"(?<![\d.])(\d+(?:\.\d+)*)\s*節(?![点約])"), r"Section \1"),
    (re.compile(r"セクション\s*(\d+(?:\.\d+)*)"), r"Section \1"),
    (re.compile(r"(?<![A-Za-z])Sect?\.\s*(?=\d)"), "Section "),
    (re.compile(r"(?<![A-Za-z])Section\s+Section(?![A-Za-z])"), "Section"),
)
_NUMBER_KANJI = "万億兆"  # part of a number (3600万, 1億): no space before it
_JA_ALNUM_RE = re.compile(
    rf"(?<=[{_JA}])(?=[{_LATIN}])|(?<=[{_LATIN}])(?=[{_JA}])"
    rf"|(?<=[{_JA}])(?<![{_NUMBER_KANJI}])(?=[0-9])|(?<=[0-9])(?=[{_JA}])(?![{_NUMBER_KANJI}])"
)
_EMPHASIS_RE = re.compile(rf"(?<=[{_LATIN}0-9])(\*{{1,2}})(?=[{_JA}])|(?<=[{_JA}])(\*{{1,2}})(?=[{_LATIN}0-9])")
_SPACED, _KEPT = ("\ue000", "\ue001"), ("\ue002", "\ue003")  # placeholder brackets (private use)
_PLACEHOLDER_RE = re.compile("[\ue000\ue002](\\d+)[\ue001\ue003]")
_BEFORE_SPACED_RE = re.compile(rf"(?<=[{_JA}{_LATIN}0-9])(?=\ue000)")
_AFTER_SPACED_RE = re.compile(rf"(?<=\ue001)(?=[{_JA}{_LATIN}0-9])")


def normalize_text(markdown: str) -> str:
    """
    Section references as ``Section 3.2``, and a space between Japanese and English words, numbers,
    inline code, inline math or URLs (but not before 万, 億 and 兆, which belong to the number).
    Code blocks, display math, HTML tags and the reference list stay as they are.

    >>> normalize_text("3.2節で述べたTransformerは`f(x)`と$y=4x$を使う（第5.4節）。Table 3の最下段。")
    'Section 3.2 で述べた Transformer は `f(x)` と $y=4x$ を使う（Section 5.4）。Table 3 の最下段。'
    >>> normalize_text("**Transformer**は節約し、8基のGPUとhttps://example.com/aで公開する。")
    '**Transformer** は節約し、8 基の GPU と https://example.com/a で公開する。'
    >>> normalize_text("約450万の文対と3600万文を12時間で")
    '約 450万の文対と 3600万文を 12 時間で'
    """
    saved: list[str] = []

    def stash(brackets: tuple[str, str]):
        def replace(match: re.Match) -> str:
            saved.append(match.group(0))
            return f"{brackets[0]}{len(saved) - 1}{brackets[1]}"

        return replace

    text = markdown
    for pattern in (_REFERENCE_LIST_RE, _FENCE_RE, _DISPLAY_MATH_RE, _HTML_TAG_RE):
        text = pattern.sub(stash(_KEPT), text)
    for pattern in (_INLINE_CODE_RE, _INLINE_MATH_RE, _URL_RE):
        text = pattern.sub(stash(_SPACED), text)
    for pattern, replacement in _SECTION_REFS:
        text = pattern.sub(replacement, text)
    text = _EMPHASIS_RE.sub(lambda m: f"{m.group(1)} " if m.group(1) else f" {m.group(2)}", text)
    text = _JA_ALNUM_RE.sub(" ", text)
    text = _AFTER_SPACED_RE.sub(" ", _BEFORE_SPACED_RE.sub(" ", text))
    for _ in range(3):  # a stashed span may hold another placeholder
        text, count = _PLACEHOLDER_RE.subn(lambda m: saved[int(m.group(1))], text)
        if not count:
            break
    return text


def reference_list(entries: list[str]) -> str:
    r"""
    The reference entries as a Markdown list, each exactly as printed.

    >>> print(reference_list(["[1] A. Author. Title. 2020.", "2. B. Author. Title."]))
    - [1] A. Author. Title. 2020.
    - 2\. B. Author. Title.
    """
    items = []
    for entry in entries:
        entry = " ".join(entry.split())
        entry = re.sub(r"^(\d+)([.)])(\s)", r"\1\\\2\3", entry)  # not a nested ordered list
        entry = re.sub(r"^([#>+\-*])", r"\\\1", entry)
        items.append(f"- {entry}")
    return "\n".join(items)


def first_heading(markdown: str) -> str | None:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or None
    return None


def text_for_summary(markdown: str) -> str:
    """The translated paper without figure tags and the reference list (they add nothing to a summary)."""
    lines, skipping = [], False
    for line in markdown.splitlines():
        heading = _HEADING_RE.match(line)
        if heading and len(heading["hashes"]) <= 2:
            skipping = heading["text"].strip().rstrip(":.").lower() in ("references", "reference", "bibliography")
        if not skipping and not line.lstrip().startswith("<img "):
            lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


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
