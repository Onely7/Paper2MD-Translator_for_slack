"""
The term list: the Japanese rendering the lab uses for each English term.

The list is a TSV file kept out of the repository (``Settings.term_list``,
``$TERM_LIST``; ``references/`` is not committed).  Each row gives an English
term, the Japanese rendering chosen by counting how textbooks write it, the
variants to avoid and the books.  About 10,000 rows are far too many to send
with every paper, so the glossary request only gets the rows whose English
appears in the paper (``TermList.candidates``), and the model picks the rows
that have the paper's meaning (``prompt_texts/glossary.md``).

A row whose evidence column says 研究室の方針で追加 is a rendering the lab
decided (development set -> 開発データ); it is sent as such and ranks first.

Rows that are left out:

* 記法・記号（2冊以上）: how each book writes a symbol; a paper keeps its own notation.
* Code names: rows of データ・ソフトウェア whose rendering has Latin letters
  (``to`` -> ``toメソッド``, ``head``), and identifiers such as ``q_proj`` in
  any category.  Passed on, ``training`` -> ``training`` would read as "keep
  this word in English".

Matching (``words``) ignores case, hyphens and a plural ending, except for
acronyms (IN, IS, BLEU), which match only as written, so that "in" and "is"
are not taken for instance normalization and importance sampling.  A one-word
variant is used only as the term without a generic head word (``attention``
for attention mechanism), so ``input`` does not pull in 説明変数.  A row that
only one book has is not taken through a single ordinary word (position for
出現位置): such a row gives the word the sense of one field.

The variants column also holds expressions that are only close to the term
(scaled dot-product attention for QKV attention, compatibility function for
attention score).  A row found only through a variant is sent as such, so the
model takes its rendering only for the same thing, and never for a name the
paper gives in quotes or as a section heading of two words or more
(``named_phrases``): those names stay the paper's own.

``mentioned`` uses the same matching to send each block only the glossary
entries whose English appears in it (an entry ending in a generic head word
also for the word without it: attention mechanism for "attention").
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import threading
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .prompts import Glossary

logger = logging.getLogger(__name__)

# The columns used, found by their header (NFKC, without spaces); the first two are required.
COLUMNS = {
    "ja": "用語・表記 (日本語)",
    "en": "用語・表記 (英語)",
    "category": "区分",
    "definition": "本文に沿った定義と説明",
    "variants": "表記揺れしそうな表現リスト (英語)",
    "basis": "採用根拠（書籍ごとの表記と票数）",
}
NOTATION_CATEGORY = "記法・記号（2冊以上）"
LAB_MARK = "研究室の方針で追加"  # in the evidence column: a row the lab added (sent as 研究室の方針, not N冊)
CODE_CATEGORY = "データ・ソフトウェア"
MAX_KEY_WORDS = 8  # longer terms are descriptions that a paper does not write word for word
MIN_COUNT = 1  # a term used once also takes the list's rendering (terms not in the list stay in English)
MAX_CANDIDATE_CHARS = 30_000  # the candidate lines sent with the glossary request (about 10,000 tokens)
DEFINITION_CHARS = 60
_STOPWORDS = frozenset("a an and are as at be by for from in is it of on or that the this to we with".split())
# A one-word variant is used when the rest of the term is one of these words (attention for attention
# mechanism, softmax for softmax function); other one-word variants (input for input value, supervised
# for supervised learning, values for observation) are ordinary words of every paper.
_GENERIC_HEADS = frozenset(
    "algorithm approach function mechanism method model module network operation procedure process technique".split()
)

_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_QUOTED_RE = re.compile(r"""[“"]([^“”"\n]{3,80})[”"]|``([^`'\n]{3,80})''""")
_SECTION_NUMBER_RE = re.compile(r"^\s*(?:[A-Z]|\d+)(?:\.\d+)*\.?\s+")
_PARENS_RE = re.compile(r"\(([^)]*)\)")
# identifiers, options and calls (q_proj, --lr, forward(), te.Linear, <extra_id_0>, a=b) in a rendering
_CODE_RE = re.compile(r"[_()\[\]{}<>=\\]|::|--|[A-Za-z0-9]\.[A-Za-z]")

Key = tuple[str, ...]


def words(text: str) -> list[str]:
    """
    The English words of a text: letters and digits; hyphens, apostrophes and other marks split words.

    >>> words("Self-attention, BLEU-4 and Cohen's kappa")
    ['Self', 'attention', 'BLEU', '4', 'and', 'Cohen', 's', 'kappa']
    """
    return _WORD_RE.findall(unicodedata.normalize("NFKC", text))


def _case_sensitive(tokens: list[str]) -> bool:
    """
    Acronyms and names with inner capitals match only as written.

    >>> [_case_sensitive(words(t)) for t in ("IN", "3D", "ResNet", "Transformer", "t-SNE", "label smoothing")]
    [True, True, True, False, False, False]
    """
    if not any(c.islower() for c in "".join(tokens)):
        return True
    return len(tokens) == 1 and any(c.isupper() for c in tokens[0][1:])


def _forms(key: Key, case_sensitive: bool) -> Iterator[Key]:
    """The key and its plural forms (LLMs; encoders, losses, ontologies)."""
    yield key
    last = key[-1]
    if case_sensitive:
        yield (*key[:-1], last + "s")
    elif last.isalpha():
        yield (*key[:-1], last + "s")
        yield (*key[:-1], last + "es")
        if last.endswith("y"):
            yield (*key[:-1], last[:-1] + "ies")


def _keys(term: str) -> list[tuple[list[str], bool]]:
    """
    The word sequences of a term and of the acronym in its parentheses, each with whether case matters.

    >>> _keys("self-attention（SA）")
    [(['self', 'attention'], False), (['SA'], True)]
    """
    text = unicodedata.normalize("NFKC", term.replace("＊", ""))
    parts = [_PARENS_RE.sub(" ", text)] + [inner for inner in _PARENS_RE.findall(text) if len(words(inner)) <= 3]
    keys = []
    for part in parts:
        tokens = words(part)
        if tokens and len(tokens) <= MAX_KEY_WORDS:
            case_sensitive = _case_sensitive(tokens)
            keys.append((tokens if case_sensitive else [t.lower() for t in tokens], case_sensitive))
    return keys


def _ngrams(text: str) -> tuple[set[Key], set[Key]]:
    """All word sequences of a text up to MAX_KEY_WORDS: (lower-cased, as written)."""
    tokens = words(text)
    lower = [t.lower() for t in tokens]
    folded, exact = set(), set()
    for i in range(len(tokens)):
        for n in range(1, min(MAX_KEY_WORDS, len(tokens) - i) + 1):
            folded.add(tuple(lower[i : i + n]))
            exact.add(tuple(tokens[i : i + n]))
    return folded, exact


def _name_key(text: str) -> Key:
    """How names are compared: lower-case words, without a plural ending on the last one."""
    tokens = [t.lower() for t in words(text)]
    if tokens and len(tokens[-1]) > 3 and tokens[-1].endswith("s") and not tokens[-1].endswith("ss"):
        tokens[-1] = tokens[-1][:-1]
    return tuple(tokens)


def named_phrases(text: str, headings: Iterable[str] = ()) -> set[Key]:
    """
    The names a paper gives: phrases in quotes, and its section headings of two words or more
    without their numbers (a heading such as "Attention" or "Training" names no component).

    >>> sorted(named_phrases('We call it "Scaled Dot-Product Attention" (Figure 2).', ["3.2.2 Multi-Head Attention"]))
    [('multi', 'head', 'attention'), ('scaled', 'dot', 'product', 'attention')]
    >>> named_phrases("", ["3.2 Attention", "5 Training"])
    set()
    """
    names = {_name_key(a or b) for a, b in _QUOTED_RE.findall(text)}
    titles = (_name_key(_SECTION_NUMBER_RE.sub("", heading)) for heading in headings)
    names |= {title for title in titles if len(title) > 1}
    return {name for name in names if name}


def _glossary_keys(en: str) -> list[tuple[Key, bool]]:
    """The keys of a glossary term, and of the term without a generic head word (attention mechanism)."""
    keys = []
    for tokens, case_sensitive in _keys(en):
        keys.append((tuple(tokens), case_sensitive))
        if not case_sensitive and len(tokens) > 1 and tokens[-1] in _GENERIC_HEADS and _usable(tokens[:-1], False):
            keys.append((tuple(tokens[:-1]), False))
        if not case_sensitive and len(tokens) == 2:
            keys.append((("".join(tokens),), False))  # word-piece also as wordpiece
        if case_sensitive and len(tokens) == 1 and re.fullmatch(r"(?:[A-Z][a-z]+){2,}", tokens[0]):
            keys.append((tuple(w.lower() for w in re.findall(r"[A-Z][a-z]+", tokens[0])), False))  # WordPiece
    return keys


def mentioned(glossary: Glossary, text: str) -> Glossary:
    """
    The glossary entries whose English term appears in the text (the English of one block);
    the whole glossary when there is no text (a scanned page whose text was not read).

    >>> glossary = [("attention mechanism", "注意機構"), ("BLEU", "BLEU"), ("encoder", "エンコーダ"),
    ...             ("instance normalization (IN)", "インスタンス正規化")]
    >>> mentioned(glossary, "Encoders use self-attention in every layer.")
    [('attention mechanism', '注意機構'), ('encoder', 'エンコーダ')]
    >>> mentioned(glossary, "IN layers")[0]
    ('instance normalization (IN)', 'インスタンス正規化')
    >>> mentioned(glossary, " ") == glossary
    True
    >>> mentioned([("WordPiece", "WordPiece")], "a 32000 word-piece vocabulary")
    [('WordPiece', 'WordPiece')]
    >>> mentioned([("word-piece", "WordPiece")], "per-wordpiece perplexities")
    [('word-piece', 'WordPiece')]
    """
    if not glossary or not text.strip():
        return glossary
    folded, exact = _ngrams(text)
    return [
        (en, ja)
        for en, ja in glossary
        if any(
            form in (exact if case_sensitive else folded)
            for key, case_sensitive in _glossary_keys(en)
            for form in _forms(key, case_sensitive)
        )
    ]


def book_count(basis: str) -> int:
    """
    How many books have the term, from the evidence column.

    >>> book_count("1冊（大学教養統計学 p.11, 12）")
    1
    >>> book_count("日: 全8冊一致（深層学習、画像認識の基礎）　英: 5冊が dropout")
    8
    >>> book_count("日: 注意機構 6（A、B、C、D、E、F） ／ アテンション機構 1（G） ／ 注意 1（A）　英: 3冊が attention")
    7
    """
    if match := re.match(r"\s*(\d+)冊", basis):
        return int(match.group(1))
    japanese = basis.split("英:")[0]
    if match := re.search(r"全(\d+)冊一致", japanese):
        return int(match.group(1))
    books = {
        re.sub(r"\s*p\.[\d\s,\-–]*$", "", name).strip()
        for group in re.findall(r"（([^）]*)）", japanese)
        for name in group.split("、")
    }
    return len(books - {""})


def short_definition(text: str) -> str:
    """
    The first sentence of a definition, without notes.

    >>> short_definition("正しく分類できた事例の割合。多クラスでも使う。　［注］書籍により呼び方が違う。")
    '正しく分類できた事例の割合。'
    """
    text = " ".join(text.split("［注］")[0].split())
    if match := re.match(r".+?。", text):
        text = match.group(0)
    return text if len(text) <= DEFINITION_CHARS else text[: DEFINITION_CHARS - 1] + "…"


def _code_name(category: str, ja: str) -> bool:
    return (category == CODE_CATEGORY and re.search(r"[A-Za-z]", ja) is not None) or _CODE_RE.search(ja) is not None


@dataclass(frozen=True)
class Term:
    en: str  # as in the list, without ＊ (an acronym in （）)
    ja: str
    category: str = ""
    books: int = 0
    definition: str = ""  # the first sentence
    lab: bool = False  # a rendering the lab decided (LAB_MARK in the evidence column), not counted from books

    @property
    def weight(self) -> int:
        """How much the row counts when rows are ordered: the lab's rows first, then by books."""
        return 1_000 if self.lab else self.books


@dataclass(frozen=True)
class Candidate:
    """A row of the list whose English appears in the paper."""

    term: Term
    count: int  # how often the paper uses it
    via: str = ""  # a close expression of the list that the paper uses instead of the term ("" otherwise)
    define: bool = False  # another row of the same category has the same English: show the definition
    short: str = ""  # a short form of the term that the paper uses (attention for attention mechanism)
    as_term: bool = True  # the paper also writes the term itself

    def line(self) -> str:
        """
        >>> term = Term("phrase structure parsing", "句構造解析", "自然言語処理", 2, "句構造を求める解析。")
        >>> Candidate(term, 3, via="constituency parsing").line()
        '- phrase structure parsing: 句構造解析［自然言語処理、2冊］（論文には近い表現の constituency parsing が出てくる）'
        >>> Candidate(Term("attention mechanism", "注意機構", "深層学習", 10), 30, short="attention").line()
        '- attention mechanism: 注意機構［深層学習、10冊］（論文では attention とも書く）'
        >>> Candidate(Term("accuracy", "正解率", "評価・評価指標", 7, "正しく分類できた割合。"), 9, define=True).line()
        '- accuracy: 正解率［評価・評価指標、7冊］ 定義: 正しく分類できた割合。'
        """
        source = "研究室の方針" if self.term.lab else f"{self.term.books}冊" if self.term.books else ""
        meta = "、".join(filter(None, (self.term.category, source)))
        line = f"- {self.term.en}: {self.term.ja}" + (f"［{meta}］" if meta else "")
        if self.short:
            line += f"（論文では {self.short} {'とも書く' if self.as_term else 'と書く'}）"
        elif self.via:
            line += f"（論文には近い表現の {self.via} が出てくる）"
        return line + (f" 定義: {self.term.definition}" if self.define else "")


def _usable(tokens: list[str], case_sensitive: bool, taken: frozenset[str] | set[str] = frozenset()) -> bool:
    """
    Whether a key may match: no single letters, no stopwords; ``taken``: words that are terms of their own.

    >>> [_usable(*key) for key in ((["I"], True), (["IN"], True), (["in"], False), (["layer", "norm"], False))]
    [False, True, False, True]
    """
    if len(tokens) > 1:
        return True
    word = tokens[0]
    if case_sensitive:
        return len(word) >= 2
    return len(word) >= 2 and word not in _STOPWORDS and word not in taken


class TermList:
    def __init__(self, terms: list[Term], variants: list[list[str]] | None = None, name: str = "", digest: str = ""):
        """``variants[i]``: the English variants of ``terms[i]`` (other ways papers write the term)."""
        self.terms, self.name, self.digest = terms, name, digest
        # key -> [(term index, the variant ("" for the term itself), whether the variant is a short form)]
        self._index: dict[bool, dict[Key, list[tuple[int, str, bool]]]] = {
            False: defaultdict(list),
            True: defaultdict(list),
        }
        # one-word terms: a one-word variant of another row must not take them
        own_words = {tokens[0] for term in terms for tokens, _ in _keys(term.en) if len(tokens) == 1}
        for i, term in enumerate(terms):
            keys = _keys(term.en)
            for tokens, case_sensitive in keys:
                if _usable(tokens, case_sensitive):
                    self._add(tokens, case_sensitive, i, "", False)
            term_words = keys[0][0] if keys and not keys[0][1] else []
            for variant in variants[i] if variants else []:
                for tokens, case_sensitive in _keys(variant):
                    if len(tokens) == 1 and not case_sensitive:
                        rest = [word for word in term_words if word != tokens[0]]
                        if len(rest) != len(term_words) - 1 or not set(rest) <= _GENERIC_HEADS:
                            continue
                    # one word (attention for attention mechanism) or an acronym (BPE): the term, shortened
                    short = len(tokens) == 1
                    if _usable(tokens, case_sensitive, own_words):
                        self._add(tokens, case_sensitive, i, variant.replace("＊", "").strip(), short)

    def _add(self, tokens: list[str], case_sensitive: bool, term_index: int, via: str, short: bool) -> None:
        for form in _forms(tuple(tokens), case_sensitive):
            entries = self._index[case_sensitive][form]
            if all(index != term_index for index, *_ in entries):
                entries.append((term_index, via, short))

    def __len__(self) -> int:
        return len(self.terms)

    @property
    def label(self) -> str:
        """The file and its digest, recorded with each translation."""
        return f"{self.name} ({self.digest})" if self.digest else self.name

    def candidates(self, text: str, min_count: int = MIN_COUNT, names: Iterable[Key] = ()) -> list[Candidate]:
        """
        The rows whose English appears in the text at least ``min_count`` times, most used first.

        ``names``: the paper's own names (``named_phrases``); a row found only through a variant
        that is one of them is left out.
        """
        names = set(names)
        tokens = words(text)
        lower = [t.lower() for t in tokens]
        counts: Counter[int] = Counter()
        via_counts: dict[int, Counter[str]] = defaultdict(Counter)
        short_counts: dict[int, Counter[str]] = defaultdict(Counter)
        as_term: set[int] = set()
        precise: set[int] = set()  # found through more than one word, or an acronym
        rows_of_key: dict[tuple[bool, Key], set[int]] = defaultdict(set)
        for i in range(len(tokens)):
            for n in range(1, min(MAX_KEY_WORDS, len(tokens) - i) + 1):
                for case_sensitive, key in ((False, tuple(lower[i : i + n])), (True, tuple(tokens[i : i + n]))):
                    for index, via, short in self._index[case_sensitive].get(key, ()):
                        counts[index] += 1
                        rows_of_key[case_sensitive, key].add(index)
                        if case_sensitive or len(key) > 1:
                            precise.add(index)
                        if not via:
                            as_term.add(index)
                        elif short:
                            short_counts[index][via] += 1
                        else:
                            via_counts[index][via] += 1
        # rows of one category that the same English points to: the category does not tell them apart
        define = set()
        for indices in rows_of_key.values():
            by_category = defaultdict(set)
            for index in indices:
                by_category[self.terms[index].category].add(index)
            for same in by_category.values():
                if len({self.terms[index].ja for index in same}) > 1:
                    define |= same
        found = []
        for index, count in counts.items():
            short = short_counts[index].most_common(1)[0][0] if short_counts[index] else ""
            via = "" if index in as_term or short else via_counts[index].most_common(1)[0][0]
            one_book_word = self.terms[index].weight <= 1 and index not in precise
            enough = count >= (1 if self.terms[index].lab else min_count)  # the lab's rows: from one use
            if enough and not one_book_word and not (via and _name_key(via) in names):
                defined = index in define and bool(self.terms[index].definition)
                found.append(Candidate(self.terms[index], count, via, defined, short, index in as_term))
        return sorted(found, key=lambda c: (-c.count, -c.term.weight, c.term.en.lower(), c.term.ja))

    def candidate_lines(self, text: str, max_chars: int = MAX_CANDIDATE_CHARS, names: Iterable[Key] = ()) -> str:
        """The candidates as lines for the glossary request, within ``max_chars``."""
        lines, size = [], 0
        for candidate in self.candidates(text, names=names):
            line = candidate.line()
            size += len(line) + 1
            if size > max_chars:
                break
            lines.append(line)
        return "\n".join(lines)

    def renderings(self, en: str) -> list[str]:
        """The list's renderings of an English term (as a term, an acronym or a variant), most books first."""
        indices = {
            index
            for tokens, case_sensitive in _keys(en)
            for index, *_ in self._index[case_sensitive].get(tuple(tokens), ())
        }
        terms = sorted((self.terms[i] for i in indices), key=lambda t: (-t.weight, t.ja))
        return list(dict.fromkeys(term.ja for term in terms))

    def check(self, glossary: Glossary) -> int:
        """Log each glossary rendering that the list does not have for its term; return how many follow the list."""
        following = 0
        for en, ja in glossary:
            listed = self.renderings(en)
            if not listed:
                continue
            if "".join(ja.split()) in {"".join(item.split()) for item in listed}:
                following += 1
            else:
                logger.info("Glossary: %s -> %s, but the term list has %s", en, ja, " / ".join(listed[:4]))
        return following


def parse_term_list(text: str, name: str = "", digest: str = "") -> TermList:
    """Read the TSV text of a term list (see ``COLUMNS``); rows without English or Japanese are skipped."""
    rows = csv.reader(io.StringIO(text), delimiter="\t")
    header = next(rows, [])
    position = {"".join(unicodedata.normalize("NFKC", h).split()): i for i, h in enumerate(header)}
    columns = {
        key: position.get("".join(unicodedata.normalize("NFKC", title).split())) for key, title in COLUMNS.items()
    }
    if columns["ja"] is None or columns["en"] is None:
        raise ValueError(f"the header needs the columns {COLUMNS['ja']} and {COLUMNS['en']}")

    def cell(row: list[str], key: str) -> str:
        i = columns[key]
        return row[i].strip() if i is not None and i < len(row) else ""

    terms, variants = [], []
    for row in rows:
        en, ja, category = cell(row, "en").replace("＊", "").strip(), cell(row, "ja"), cell(row, "category")
        if not en or not ja or category == NOTATION_CATEGORY or _code_name(category, ja):
            continue
        basis = cell(row, "basis")
        lab = LAB_MARK in basis
        books = 0 if lab else book_count(basis)
        terms.append(Term(en, ja, category, books, short_definition(cell(row, "definition")), lab))
        variants.append([v.strip() for v in cell(row, "variants").split("、") if v.strip()])
    return TermList(terms, variants, name, digest)


def read_term_list(path: Path) -> TermList:
    data = path.read_bytes()
    return parse_term_list(data.decode("utf-8-sig"), path.name, hashlib.sha256(data).hexdigest()[:12])


_lock = threading.Lock()
_loaded: dict[Path, tuple[tuple[int, int], TermList | None]] = {}


def load_term_list(path: str | Path | None) -> TermList | None:
    """The term list at ``path``, read again when the file changes; None without a readable list."""
    if path is None:
        return None
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        with _lock:
            if path not in _loaded:
                logger.warning("No term list at %s: glossaries are made without it", path)
            _loaded[path] = ((0, 0), None)
        return None
    stamp = (stat.st_mtime_ns, stat.st_size)
    with _lock:
        if (cached := _loaded.get(path)) and cached[0] == stamp:
            return cached[1]
        try:
            term_list = read_term_list(path)
            logger.info("Term list: %d terms from %s", len(term_list), term_list.label)
        except (OSError, UnicodeDecodeError, ValueError, csv.Error) as e:
            logger.warning("Term list %s could not be read: %s", path, e)
            term_list = None
        _loaded[path] = (stamp, term_list)
        return term_list
