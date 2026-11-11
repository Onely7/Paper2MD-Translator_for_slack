"""Markdown post-processing (regressions of the old transform_markdown/mdformat chain)."""

import unittest
from unittest import mock

import paper2md.markdown as markdown_module
from paper2md.markdown import (
    finalize_markdown,
    first_heading,
    format_markdown,
    normalize_headings,
    normalize_math,
    normalize_text,
    reference_list,
    summary_for_slack,
    text_for_summary,
)


class FinalizeMarkdownTests(unittest.TestCase):
    def test_math_is_left_untouched(self):
        block = "$$\n\\max_{\\mathbf{z}} \\, p(\\mathbf{h}\\mid\\mathbf{z})\\,p(\\mathbf{z})\n$$"
        inline = "$a*b + c_i$ と $x_{1}^{2}$"
        out = finalize_markdown(f"本文 {inline} を使う。\n\n{block}\n\n\\[ y = x \\]")
        self.assertIn(block, out)
        self.assertIn(inline, out)
        self.assertIn("\n$$\ny = x\n$$", out)  # \[ \] becomes a $$ block

    def test_code_is_left_untouched(self):
        code = "```python\ndef foo(x):\n    return x * 2  # $not math$\n```"
        self.assertIn(code, finalize_markdown(f"本文\n\n{code}"))

    def test_title_and_paragraphs_are_not_headings(self):
        out = finalize_markdown("# A Survey of LLMs\n\nLLM は強力である。\n\nThe model works.\n\n1. 項目\n2. 項目")
        self.assertTrue(out.startswith("# A Survey of LLMs\n"))
        self.assertIn("\nLLM は強力である。\n", out)
        self.assertIn("\nThe model works.\n", out)
        self.assertNotIn("## 1.", out)

    def test_currency_is_not_math(self):
        self.assertIn("$5 と $10", finalize_markdown("費用は $5 と $10 です。"))


class HeadingTests(unittest.TestCase):
    def check(self, source, expected):
        self.assertEqual(normalize_headings(source), expected)

    def test_numbered_levels(self):  # the level follows the numbering; the text stays as printed
        self.check("# Title\n### 2 Method", "# Title\n## 2 Method")
        self.check("# Title\n## 2.1 Training", "# Title\n### 2.1 Training")
        self.check("# Title\n## 3.2.1. Scaled Attention", "# Title\n#### 3.2.1. Scaled Attention")
        self.check("# Title\n# 4 Experiments", "# Title\n## 4 Experiments")
        self.check("# Title\n# A.1 Details", "# Title\n### A.1 Details")

    def test_not_section_numbers(self):
        for heading in ("## A Survey Section", "## 2023 Results", "## Related Work", "## C#"):
            self.check(f"# Title\n{heading}", f"# Title\n{heading}")

    def test_keywords(self):
        self.check("# T\n#### References", "# T\n## References")
        self.check("# T\n**Abstract**", "# T\n## Abstract")
        self.check("# T\nAppendix A shows the proof.", "# T\nAppendix A shows the proof.")

    def test_run_in_heading_is_split(self):
        self.check("# T\n**3.1 Training** 本文が続く。", "# T\n### 3.1 Training\n\n本文が続く。")
        self.check("# T\n**Training.** We train.", "# T\n**Training.** We train.")


class MathTests(unittest.TestCase):
    def check(self, source, expected):
        self.assertEqual(normalize_math(source).strip(), expected)

    def test_delimiters(self):
        self.check("式 \\(a_i\\) を使う。", "式 $a_i$ を使う。")
        self.check("次式。\n\\[\nE = mc^2\n\\]\n続き。", "次式。\n\n$$\nE = mc^2\n$$\n\n続き。")
        self.check(
            "\\begin{align}\na &= b \\\\\nc &= d\n\\end{align}",
            "$$\n\\begin{align}\na &= b \\\\\nc &= d\n\\end{align}\n$$",
        )
        self.check("和は $$\\sum_i x_i$$ である。", "和は $\\sum_i x_i$ である。")  # $$ inside a sentence: inline
        self.check("$$x = 1$$", "$$\nx = 1\n$$")

    def test_repairs_inside_math(self):
        self.check("損失$L\\_{\\text{ce}}$を最小化する。", "損失 $L_{\\text{ce}}$ を最小化する。")
        self.check("$\\text{max\\_len}$", "$\\text{max\\_len}$")  # the escape is right inside \\text
        self.check("$$\n\\\\frac{a}{b}\n$$", "$$\n\\frac{a}{b}\n$$")
        self.check("$$\ny = Wx \\quad (2)\n$$", "$$\ny = Wx \\tag{2}\n$$")
        self.check("$ x + y $ と書く。", "$x + y$ と書く。")

    def test_names_keep_their_underscore(self):  # 1706.03762 eq. (3): step_num rendered as step subscript n
        equation = "$$\nlrate = \\min(step\\_num^{-0.5}, step\\_num \\cdot warmup\\_steps^{-1.5}) \\tag{3}\n$$"
        self.check(equation, equation)
        self.check("最初の$warmup_steps$回", "最初の $warmup\\_steps$ 回")  # a bare name gets the escape
        self.check("$h\\_{t-1}$ と $\\log\\_k(n)$ と $W\\_i^Q$", "$h_{t-1}$ と $\\log_k(n)$ と $W_i^Q$")

    def test_not_math(self):
        self.check("費用は $5 と $10 です。", "費用は $5 と $10 です。")
        code = "`$x$` と\n\n```python\nprint('\\(x\\)')\n```"
        self.check(code, code)


class FormatTests(unittest.TestCase):
    # a line break written with a trailing backslash, a list, inline math with * and \_, a math block with \\
    SOURCE = "Ashish Vaswani*\\\nGoogle Brain\n\n* 式 $a*b$ と $step\\_num$\n\n$$\nx \\\\\ny\n$$\n"

    def check(self, out):
        # a line break of two spaces, not a trailing backslash (mdformat also escapes the *, prettier does not)
        self.assertRegex(out, r"Ashish Vaswani\\?\*  \nGoogle Brain")
        self.assertIn("- 式 $a*b$ と $step\\_num$", out)
        self.assertIn("$$\nx \\\\\ny\n$$", out)

    def test_prettier(self):
        if markdown_module.prettier("x\n") is None:
            self.skipTest("prettier is not installed (uv run npm ci --prefix tools/prettier)")
        self.check(format_markdown(self.SOURCE))

    def test_mdformat_without_prettier(self):
        with mock.patch.object(markdown_module, "prettier", return_value=None):
            self.check(format_markdown(self.SOURCE))


class ReferenceAndSummaryInputTests(unittest.TestCase):
    def test_reference_list(self):
        self.assertEqual(
            reference_list(["[1] A. B. Title.  2020.", "3. C. D. Other.", "- weird"]),
            "- [1] A. B. Title. 2020.\n- 3\\. C. D. Other.\n- \\- weird",
        )
        # it survives the final formatting unchanged
        out = finalize_markdown("## References\n\n" + reference_list(["[1] A. Name_with_underscore. 2020."]))
        self.assertIn("- [1] A. Name_with_underscore. 2020.", out)

    def test_text_for_summary(self):
        markdown = '# T\n\n## 1 Intro\n\n本文\n\n<img src="figures/fig01.png">\n\n## References\n\n- [1] X\n\n## A Appendix\n\n付録'
        self.assertEqual(text_for_summary(markdown), "# T\n\n## 1 Intro\n\n本文\n\n## A Appendix\n\n付録\n")


class SummaryTests(unittest.TestCase):
    def test_slack_format(self):
        self.assertEqual(
            summary_for_slack("【背景】\n- a **b**\n    - c\n*【目的】*\n* d"),
            "*【背景】*\n・a *b*\n    ・c\n*【目的】*\n・d",
        )

    def test_first_heading(self):
        self.assertEqual(first_heading("intro\n# Title\n## 1. A"), "Title")
        self.assertIsNone(first_heading("## 1. A"))


class NormalizeTextTests(unittest.TestCase):
    def check(self, cases):
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(normalize_text(text), expected)
                self.assertEqual(normalize_text(expected), expected)  # nothing more to change

    def test_section_references(self):
        self.check(
            {
                "3.2節で述べた": "Section 3.2 で述べた",
                "（第5.4節）": "（Section 5.4）",
                "WSJ の第 23 節": "WSJ の Section 23",
                "セクション3を参照": "Section 3 を参照",
                "Sec. 3.2 と Sect. 4": "Section 3.2 と Section 4",
                "Section 3.2節で": "Section 3.2 で",
                "この節では節約と節点を扱う。": "この節では節約と節点を扱う。",
            }
        )

    def test_spaces_between_japanese_and_english(self):
        self.check(
            {
                "Transformerは": "Transformer は",
                "intra-attentionとも呼ばれる": "intra-attention とも呼ばれる",
                "「QKV注意機構」": "「QKV 注意機構」",
                "は**Transformer**を": "は **Transformer** を",
                "`f(x)`を使い`g`と": "`f(x)` を使い `g` と",
                "値$y=4x$とa$b$c": "値 $y=4x$ と a $b$ c",
                "コードはhttps://github.com/a/bで公開": "コードは https://github.com/a/b で公開",
                "Table 3の行とFigure 2を参照": "Table 3 の行と Figure 2 を参照",
                "8基のGPU、12時間": "8 基の GPU、12 時間",
                "第5層の2つ": "第 5 層の 2 つ",
                "約450万の文と3600万文": "約 450万の文と 3600万文",  # 万 belongs to the number
                "（WSJ）部分": "（WSJ）部分",
            }
        )

    def test_blocks_html_and_references_are_left_alone(self):
        text = (
            '```\nコードx\n```\n\n$$\na日本\n$$\n\n<img src="figures/fig01.png" alt="図a">\n\n'
            "## References\n\n- [1] 山田Taro. 論文Title. 2020.\n\n## Appendix\n\n付録A"
        )
        self.assertEqual(normalize_text(text), text.replace("付録A", "付録 A"))


if __name__ == "__main__":
    unittest.main()
