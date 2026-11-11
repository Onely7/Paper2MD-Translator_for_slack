"""yomiyasu polishing: placeholders, the checks (with the real yomiyasu_diff.py), and both passes with a fake model."""

import json
import re
import tempfile
import unittest
from pathlib import Path

from paper2md import polish, prompts
from paper2md.config import Settings
from paper2md.layout import Segment
from paper2md.llm import ModelNotAvailableError


def segment(kind, label="para", source_text=""):
    return Segment(kind, label, [label], [], [], [], source_text=source_text)


class FakePolisher:
    """Rewrites each segment with ``rewrite(body)``; the whole-paper pass answers ``edits`` (id -> text)."""

    def __init__(self, rewrite=lambda body: body, edits=None, error=None):
        self.rewrite, self.edits, self.error = rewrite, edits or {}, error
        self.requests = []

    def generate_batch(self, requests, on_result=None, return_exceptions=False):
        out = []
        for i, request in enumerate(requests):
            self.requests.append(request)
            if self.error is not None:
                result = self.error
            elif request["instructions"] == prompts.POLISH_INSTRUCTIONS:
                body = re.search(r"<本文>\n(.*)\n</本文>", request["text"], re.S).group(1)
                result = f"前置きは無視される\n<本文>\n{self.rewrite(body)}\n</本文>"
            else:
                edits = [{"id": pid, "text": text} for pid, text in self.edits.items()]
                result = json.dumps({"edits": edits}, ensure_ascii=False)
            out.append(result)
            if on_result:
                on_result(i, result)
        return out


class PlaceholderTests(unittest.TestCase):
    def test_protect_and_restore(self):
        text = (
            "**Encoder:** 層は $N = 6$ 個で、式\n\n$$\ny = x\n$$\n\nを使う [11, 12]。"
            "コードは `f(x)`、詳細は https://example.com/a_b にある⁴。"
        )
        protected, saved = polish.protect(text)
        for kept in ("$N = 6$", "$$", "[11, 12]", "`f(x)`", "https://", "⁴", "**Encoder:**"):
            self.assertNotIn(kept, protected)
        self.assertEqual(polish.restore(protected, saved), text)
        caption, _ = polish.protect("Figure 2: 左は Scaled Dot-Product Attention。")
        self.assertTrue(caption.startswith("⟦1⟧ 左は"))
        references, _ = polish.protect("Section 3.2.2 で述べたように、Table 3 の行 (A) を見る。†Google")
        self.assertEqual(references, "⟦2⟧ で述べたように、⟦3⟧ の行 (A) を見る。⟦1⟧Google")

    def test_rewrite_problems(self):
        original = "⟦1⟧ と ⟦2⟧ を使い、" + "長い説明が続く。" * 10
        self.assertIsNone(polish.rewrite_problem(original, original.replace("使い", "用い")))
        self.assertEqual(polish.rewrite_problem(original, "⟦1⟧ を使い、" + "長い説明が続く。" * 10), "placeholders")
        self.assertEqual(polish.rewrite_problem(original, original.replace("⟦2⟧", "⟦2⟧ ⟦2⟧")), "placeholders")
        self.assertEqual(polish.rewrite_problem(original, "⟦1⟧ と ⟦2⟧ を使う。"), "length")
        self.assertEqual(polish.rewrite_problem(original, " "), "empty")

    def test_extract_body(self):
        self.assertEqual(polish.extract_body("説明\n<本文>\n訳文。\n</本文>\n以上"), "訳文。")
        self.assertEqual(polish.extract_body("```markdown\n訳文。\n```"), "訳文。")


class YomiyasuDiffTests(unittest.TestCase):
    def test_meaning_risks(self):
        original = "Transformer は RNN を用いず、自己注意だけで表現を計算する初めてのモデルである。"
        faithful = "Transformer は、RNN を用いずに自己注意だけで表現を計算する初めてのモデルである。"
        negated = "Transformer は RNN を用いる、自己注意だけで表現を計算する初めてのモデルである。"
        with tempfile.TemporaryDirectory() as tmp:
            risks = polish.meaning_risks([(original, faithful), (original, negated)], Path(tmp))
            self.assertEqual(risks[0], [])
            self.assertIn("negation", risks[1])
            self.assertEqual(list(Path(tmp).iterdir()), [])  # temporary files are removed


class PolishSegmentsTests(unittest.TestCase):
    def setUp(self):
        self.workdir = Path(tempfile.mkdtemp())
        self.segments = [
            segment("title", "title", "A Paper"),
            segment("heading", "sec_1", "1 Intro"),
            segment("text", "para"),
            segment("text", "author"),
            segment("text", "para"),
            segment("text", "para"),
        ]
        self.parts = {
            0: "# A Paper",
            1: "## 1 Intro",
            2: "モデルは $x$ を使う [1]。",
            3: "A. Author  \nトロント大学",
            4: "> ⚠️ この部分の翻訳に失敗しました（RuntimeError）。",
            5: "結果は 3 つの表に示す。",
        }

    def run_polish(self, client):
        return polish.polish_segments(self.segments, self.parts, client, Settings(), self.workdir)

    def test_rewrites_are_used(self):
        client = FakePolisher(lambda body: body.replace("使う", "用いる"))
        parts, stats = self.run_polish(client)
        self.assertEqual(parts[2], "モデルは $x$ を用いる [1]。")
        self.assertEqual((parts[3], parts[4]), (self.parts[3], self.parts[4]))  # author block, failure note
        self.assertEqual((stats.segments, stats.rewritten, stats.unchanged), (2, 1, 1))
        request = client.requests[0]
        self.assertIn("論文: A Paper\n節: 1 Intro", request["text"])
        self.assertNotIn("$x$", request["text"])
        self.assertEqual(request["gen_kwargs"]["reasoning_effort"], "high")
        self.assertTrue(request["instructions"].startswith("# yomiyasu v2.5.0"))

    def test_unsafe_rewrites_keep_the_translation(self):
        def rewrite(body):
            return body.replace(" ⟦2⟧", "") if "⟦2⟧" in body else body.replace("3 つ", "4 つ")

        parts, stats = self.run_polish(FakePolisher(rewrite))
        self.assertEqual((parts[2], parts[5]), (self.parts[2], self.parts[5]))
        self.assertEqual((stats.rewritten, stats.kept), (0, 2))
        self.assertEqual(stats.reasons["placeholders"], 1)
        self.assertTrue(any(reason.startswith("meaning risk") for reason in stats.reasons))

    def test_glossary_goes_with_each_block(self):
        client = FakePolisher()
        polish.polish_segments(
            self.segments, self.parts, client, Settings(), self.workdir, glossary=[("attention", "注意機構")]
        )
        self.assertIn("<訳語表>\n- attention: 注意機構\n</訳語表>\n\n<本文>", client.requests[0]["text"])

    def test_each_block_gets_the_glossary_terms_it_uses(self):
        self.segments[2].layer_text = "The model uses attention [1]."
        self.segments[5].layer_text = "Results are in three tables."
        client = FakePolisher()
        glossary = [("attention", "注意機構"), ("table", "表")]
        polish.polish_segments(self.segments, self.parts, client, Settings(), self.workdir, glossary=glossary)
        self.assertIn("<訳語表>\n- attention: 注意機構\n</訳語表>", client.requests[0]["text"])
        self.assertIn("<訳語表>\n- table: 表\n</訳語表>", client.requests[1]["text"])

    def test_fatal_errors_are_raised(self):
        with self.assertRaises(ModelNotAvailableError):
            self.run_polish(FakePolisher(error=ModelNotAvailableError("no")))


class PolishDocumentTests(unittest.TestCase):
    MARKDOWN = (
        "# A Paper\n\n## 1 Intro\n\nまず $x$ を定義する。\n\n$$\ny = x\n$$\n\nこの式を使う [2]。\n\n"
        '<img src="figures/fig01.png" width=60% alt="fig01.png">\n\nFigure 1: 図の説明。\n\n'
        "## References\n\n- [1] A. Author. Title. 2020.\n\n## A Appendix\n\n付録の本文。\n"
    )

    def test_only_rewritten_paragraphs_change(self):
        client = FakePolisher(
            edits={"P2": "[P2] そして、この式を用いる ⟦2⟧。", "P3": "図の説明を書き換えた。", "P9": "無い段落"}
        )
        out, stats = polish.polish_document(self.MARKDOWN, client, Settings(), Path(tempfile.mkdtemp()), "A Paper")
        self.assertIn("そして、この式を用いる [2]。", out)
        self.assertIn("Figure 1: 図の説明。", out)  # P3 lost the caption label ⟦3⟧: not used
        self.assertIn("$$\ny = x\n$$", out)
        self.assertIn("- [1] A. Author. Title. 2020.", out)
        self.assertEqual((stats.document_paragraphs, stats.document_edits, stats.document_kept), (4, 1, 1))
        text = client.requests[0]["text"]
        self.assertTrue(text.startswith("論文: A Paper"))
        self.assertIn("## 1 Intro", text)
        self.assertIn("（数式）", text)
        self.assertIn("[P4] 付録の本文。", text)
        self.assertNotIn("A. Author", text)  # references are not sent
        self.assertEqual(client.requests[0]["gen_kwargs"]["text"]["format"]["name"], "paragraph_edits")

    def test_instructions_glossary_and_chunks(self):
        glossary = [("attention", "注意機構")]
        cases = ((False, prompts.POLISH_DOCUMENT_ONLY_INSTRUCTIONS), (True, prompts.POLISH_DOCUMENT_INSTRUCTIONS))
        for after_blocks, instructions in cases:
            client = FakePolisher()
            polish.polish_document(
                self.MARKDOWN, client, Settings(), Path(tempfile.mkdtemp()), "A Paper", glossary=glossary,
                after_blocks=after_blocks,
            )  # fmt: skip
            self.assertEqual(client.requests[0]["instructions"], instructions)
            self.assertIn("<訳語表>\n- attention: 注意機構\n</訳語表>", client.requests[0]["text"])
        long = "# A Paper\n\n" + "\n\n".join(f"段落 {i}。" + "あ" * 1000 for i in range(25))
        for after_blocks, requests in ((False, 2), (True, 1)):  # without the block pass the chunks are smaller
            client = FakePolisher()
            polish.polish_document(long, client, Settings(), Path(tempfile.mkdtemp()), after_blocks=after_blocks)
            self.assertEqual(len(client.requests), requests)

    def test_bad_answers_change_nothing(self):
        client = FakePolisher()
        client.generate_batch = lambda requests, **kwargs: ["not json"]
        out, stats = polish.polish_document(self.MARKDOWN, client, Settings(), Path(tempfile.mkdtemp()))
        self.assertEqual(out, self.MARKDOWN)
        self.assertEqual(stats.failed, 1)


if __name__ == "__main__":
    unittest.main()
