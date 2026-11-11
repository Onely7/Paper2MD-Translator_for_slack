"""The lab's term list: which rows are kept, how a paper's English finds them, and how the list is loaded."""

import os
import tempfile
import unittest
from pathlib import Path

from paper2md.terms import load_term_list, mentioned, named_phrases, parse_term_list

HEADER = (
    "区分\t行\t用語・表記 (日本語)\t用語・表記 (英語)\t本文に沿った定義と説明\t"
    "表記揺れしそうな表現リスト (日本語)\t表記揺れしそうな表現リスト (英語)\t採用根拠（書籍ごとの表記と票数）"
)
ROWS = [
    # category, Japanese, English, definition, English variants, evidence
    ("深層学習", "注意機構", "attention mechanism", "入力のどこに注目するかを学習で決める仕組み。", "attention、attention mechanisms",
     "日: 注意機構 6（A、B、C、D、E、F） ／ アテンション機構 1（G）　英: 5冊が attention mechanism"),
    ("深層学習", "エンコーダ", "encoder", "入力を内部表現に変える部分。", "encoder network", "日: 全8冊一致（A、B）"),
    ("深層学習", "インスタンス正規化", "instance normalization（IN）", "", "", "日: 全2冊一致（A、B）"),
    ("確率・統計", "重点サンプリング", "importance sampling＊（IS）", "", "", "1冊（X p.12）"),
    ("記法・記号（2冊以上）", "argmax", "argmax", "", "arg max", "10冊（A、B）"),  # notation: left out
    ("データ・ソフトウェア", "toメソッド", "to", "", "", "1冊（Y p.3）"),  # a code name: left out
    ("深層学習", "q_proj", "q_proj", "", "", "1冊（Y p.4）"),  # an identifier: left out
    ("評価・評価指標", "正解率", "accuracy", "正しく分類できた事例の割合。多クラスでも使う。", "", "日: 正解率 5（A、B、C、D、E）"),
    ("評価・評価指標", "精度", "accuracy＊", "予測の当たり具合を広く指す語。", "", "2冊（Y、Z）"),
    ("自然言語処理", "系列", "sequence", "順に並んだ要素の列。", "", "日: 全5冊一致（A）"),
    ("数学・記法", "数列", "sequence＊", "数を順に並べたもの。", "", "2冊（V、W）"),
    ("自然言語処理", "出現位置", "position＊", "", "", "1冊（V p.3）"),  # one book, one ordinary word
    ("自然言語処理", "句構造解析", "phrase structure parsing", "", "constituency parsing", "日: 全2冊一致（A、B）"),
    ("確率・統計", "説明変数", "explanatory variable", "", "input", "1冊（V p.9）"),
    ("評価・評価指標", "BLEU", "BLEU", "", "BLEU score", "日: 全8冊一致（A）"),
    ("深層学習", "ソフトマックス関数", "softmax function", "", "softmax", "日: 全7冊一致（A）"),
]  # fmt: skip


def tsv(rows=ROWS) -> str:
    lines = [HEADER]
    for category, ja, en, definition, variants, basis in rows:
        lines.append("\t".join((category, "", ja, en, definition, "", variants, basis)))
    return "\n".join(lines) + "\n"


TEXT = (
    "Attention is all we need. The attention mechanisms of the Encoders and the encoder network use "
    "instance normalization: IN layers. Here we use in and is. A sequence is a sequence. "
    "We apply softmax twice: softmax. BLEU and BLEU; accuracy and accuracy. "
    "constituency parsing beats constituency parsing. The input is the input. Importance sampling once."
)


class TermListTests(unittest.TestCase):
    def setUp(self):
        self.terms = parse_term_list(tsv(), "terms.tsv", "abc")

    def test_rows_left_out(self):
        self.assertEqual(len(self.terms), len(ROWS) - 3)
        self.assertFalse({"argmax", "toメソッド", "q_proj"} & {term.ja for term in self.terms.terms})
        books = {term.ja: term.books for term in self.terms.terms}
        self.assertEqual((books["注意機構"], books["エンコーダ"], books["重点サンプリング"]), (7, 8, 1))
        self.assertEqual(self.terms.label, "terms.tsv (abc)")

    def test_candidates(self):
        found = {c.term.ja: c for c in self.terms.candidates(TEXT)}
        self.assertEqual(
            set(found),
            {"注意機構", "エンコーダ", "インスタンス正規化", "正解率", "精度", "系列", "数列", "句構造解析", "BLEU",
             "ソフトマックス関数", "重点サンプリング"},
        )  # fmt: skip
        # "in" and "is" are not the acronyms IN and IS; "input" is no short form of "explanatory variable";
        # a term used once (importance sampling) is a candidate too
        self.assertEqual(found["インスタンス正規化"].count, 2)
        self.assertEqual(found["エンコーダ"].count, 3)  # plural, and the variant "encoder network"
        self.assertEqual(found["句構造解析"].via, "constituency parsing")
        self.assertEqual((found["ソフトマックス関数"].short, found["ソフトマックス関数"].via), ("softmax", ""))
        self.assertTrue(found["ソフトマックス関数"].line().endswith("（論文では softmax と書く）"))  # a short form
        self.assertEqual(found["注意機構"].via, "")  # the paper also writes the term itself
        self.assertTrue(found["注意機構"].line().endswith("（論文では attention とも書く）"))
        # rows of one category with the same English get their definitions; other categories do not need them
        self.assertTrue(found["正解率"].define and found["精度"].define)
        self.assertFalse(found["系列"].define or found["数列"].define)
        lines = self.terms.candidate_lines(TEXT).splitlines()
        self.assertIn(
            "- phrase structure parsing: 句構造解析［自然言語処理、2冊］（論文には近い表現の constituency parsing が出てくる）",
            lines,
        )
        self.assertIn("- accuracy: 正解率［評価・評価指標、5冊］ 定義: 正しく分類できた事例の割合。", lines)
        self.assertIn("- sequence: 数列［数学・記法、2冊］", lines)
        self.assertLess(
            lines.index("- sequence: 系列［自然言語処理、5冊］"), lines.index("- sequence: 数列［数学・記法、2冊］")
        )
        self.assertEqual(len(self.terms.candidate_lines(TEXT, max_chars=90).splitlines()), 2)

    def test_a_one_book_row_needs_more_than_one_ordinary_word(self):
        text = "Each position, every position; importance sampling and importance sampling, IS and IS."
        found = {c.term.ja for c in self.terms.candidates(text)}
        self.assertNotIn("出現位置", found)  # position: one word, and only one book has 出現位置
        self.assertIn("重点サンプリング", found)  # one book, but two words (and an acronym)

    def test_a_lab_row_counts_from_one_use(self):
        rows = [*ROWS, ("機械学習", "学習データ", "training set", "", "", "★研究室の方針で追加（2026-11-11）")]
        terms = parse_term_list(tsv(rows))
        found = {c.term.ja: c for c in terms.candidates("The WSJ training set only.")}
        self.assertIn("学習データ", found)
        self.assertEqual(found["学習データ"].line(), "- training set: 学習データ［機械学習、研究室の方針］")

    def test_the_papers_own_names_are_not_replaced(self):
        # a name the paper gives (in quotes or as a heading) is not taken for a row's variant
        text = TEXT + ' We call our parser "constituency parsing".'
        found = {c.term.ja for c in self.terms.candidates(text, names=named_phrases(text))}
        self.assertNotIn("句構造解析", found)
        found = {c.term.ja for c in self.terms.candidates(TEXT, names=named_phrases("", ["6.3 Constituency Parsing"]))}
        self.assertNotIn("句構造解析", found)
        # a row found through its own English stays
        names = named_phrases("", ["3 Softmax Function", "4 Encoders"])
        self.assertIn("エンコーダ", {c.term.ja for c in self.terms.candidates(TEXT, names=names)})

    def test_renderings_and_check(self):
        self.assertEqual(self.terms.renderings("attention"), ["注意機構"])
        self.assertEqual(self.terms.renderings("Encoders"), ["エンコーダ"])
        self.assertEqual(self.terms.renderings("accuracy"), ["正解率", "精度"])
        self.assertEqual(self.terms.renderings("Transformer"), [])
        glossary = [("attention", "アテンション"), ("encoder", "エンコーダ"), ("Transformer", "Transformer")]
        with self.assertLogs("paper2md.terms", "INFO") as logs:
            self.assertEqual(self.terms.check(glossary), 1)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("attention -> アテンション, but the term list has 注意機構", logs.output[0])

    def test_mentioned(self):
        glossary = [
            ("self-attention", "自己注意機構"),
            ("encoder", "エンコーダ"),
            ("BLEU", "BLEU"),
            ("dropout", "ドロップアウト"),
        ]
        self.assertEqual(
            mentioned(glossary, "Self attention in both encoders; bleu is lower-case here."),
            [("self-attention", "自己注意機構"), ("encoder", "エンコーダ")],
        )
        self.assertEqual(mentioned(glossary, ""), glossary)
        # a term ending in a generic head word also goes with the word alone
        glossary = [("attention mechanism", "注意機構"), ("softmax function", "ソフトマックス関数")]
        self.assertEqual(mentioned(glossary, "the attention key size"), [("attention mechanism", "注意機構")])
        self.assertEqual(mentioned([], "encoder"), [])


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "terms.tsv"

    def test_load_reads_again_when_the_file_changes(self):
        self.path.write_text(tsv(ROWS[:2]), encoding="utf-8")
        first = load_term_list(self.path)
        self.assertEqual(len(first), 2)
        self.assertIs(load_term_list(self.path), first)
        self.path.write_text(tsv(ROWS[:3]), encoding="utf-8")
        os.utime(self.path, ns=(1, 2_000_000_000_000_000_000))
        self.assertEqual(len(load_term_list(self.path)), 3)
        self.assertIsNone(load_term_list(None))

    def test_missing_or_unreadable(self):
        with self.assertLogs("paper2md.terms", "WARNING") as logs:
            self.assertIsNone(load_term_list(self.path))
            self.assertIsNone(load_term_list(self.path))  # warned once
        self.assertEqual(len(logs.output), 1)
        self.path.write_text("a\tb\n1\t2\n", encoding="utf-8")
        with self.assertLogs("paper2md.terms", "WARNING") as logs:
            self.assertIsNone(load_term_list(self.path))
        self.assertIn("needs the columns", logs.output[0])


if __name__ == "__main__":
    unittest.main()
