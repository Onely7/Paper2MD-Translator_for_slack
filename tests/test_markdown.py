"""Markdown post-processing (regressions of the old transform_markdown/mdformat chain)."""

import unittest

from paper2md.markdown import finalize_markdown, first_heading, normalize_headings, normalize_math, summary_for_slack


class FinalizeMarkdownTests(unittest.TestCase):
    def test_math_is_left_untouched(self):
        block = "$$\n\\max_{\\mathbf{z}} \\, p(\\mathbf{h}\\mid\\mathbf{z})\\,p(\\mathbf{z})\n$$"
        inline = "$a*b + c_i$ と $x_{1}^{2}$"
        out = finalize_markdown(f"本文 {inline} を使う。\n\n{block}\n\n\\[ y = x \\]")
        self.assertIn(block, out)
        self.assertIn(inline, out)
        self.assertIn("\n$$\ny = x\n$$", out)  # \\[ \\] becomes a $$ block

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

    def test_numbered_levels(self):
        self.check("# Title\n### 2 Method", "# Title\n## 2. Method")
        self.check("# Title\n## 2.1 Training", "# Title\n### 2.1. Training")
        self.check("# Title\n## 3.2.1. Scaled Attention", "# Title\n#### 3.2.1. Scaled Attention")
        self.check("# Title\n# 4 Experiments", "# Title\n## 4. Experiments")
        self.check("# Title\n# A.1 Details", "# Title\n### A.1. Details")

    def test_not_section_numbers(self):
        for heading in ("## A Survey Section", "## 2023 Results", "## Related Work", "## C#"):
            self.check(f"# Title\n{heading}", f"# Title\n{heading}")

    def test_keywords(self):
        self.check("# T\n#### References", "# T\n## References")
        self.check("# T\n**Abstract**", "# T\n## Abstract")
        self.check("# T\nAppendix A shows the proof.", "# T\nAppendix A shows the proof.")

    def test_run_in_heading_is_split(self):
        self.check("# T\n**3.1 Training** 本文が続く。", "# T\n### 3.1. Training\n\n本文が続く。")
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

    def test_not_math(self):
        self.check("費用は $5 と $10 です。", "費用は $5 と $10 です。")
        code = "`$x$` と\n\n```python\nprint('\\(x\\)')\n```"
        self.check(code, code)


class SummaryTests(unittest.TestCase):
    def test_slack_format(self):
        self.assertEqual(
            summary_for_slack("【背景】\n- a **b**\n    - c\n*【目的】*\n* d"),
            "*【背景】*\n・a *b*\n    ・c\n*【目的】*\n・d",
        )

    def test_first_heading(self):
        self.assertEqual(first_heading("intro\n# Title\n## 1. A"), "Title")
        self.assertIsNone(first_heading("## 1. A"))


if __name__ == "__main__":
    unittest.main()
