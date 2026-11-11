"""translate_paper end to end on a real PDF, with Dolphin and the OpenAI API faked."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pymupdf

from paper2md import pipeline, prompts
from paper2md.cancel import Cancelled, CancelToken
from paper2md.config import Settings
from paper2md.llm import ModelNotAvailableError
from paper2md.pdf import PdfError, check_limits
from tests.test_terms import tsv


def make_pdf(path: Path, pages: int = 2) -> Path:
    doc = pymupdf.open()
    for number in range(pages):
        page = doc.new_page(width=600, height=800)
        page.insert_text((72, 72), f"Page {number + 1}")
    doc.save(path)
    return path


def el(label, bbox, order, text=""):
    return {"label": label, "bbox": bbox, "reading_order": order, "text": text, "tags": []}


# boxes in the pixels of pages rendered at 1000px (750 x 1000)
RECOGNITION = {
    "pages": [
        {
            "elements": [
                el("sec_0", [100, 50, 650, 100], 0, "Test Paper"),
                el("para", [50, 150, 700, 300], 1),
                el("fig", [50, 320, 700, 600], 2),
                el("cap", [50, 610, 700, 650], 3),
            ]
        },
        {
            "elements": [
                el("sec_1", [50, 50, 300, 90], 0, "References"),
                el("reference", [50, 100, 700, 200], 1, "[1] A. B. Title. 2020."),
            ]
        },
    ]
}


DOCUMENT_INSTRUCTIONS = (prompts.POLISH_DOCUMENT_INSTRUCTIONS, prompts.POLISH_DOCUMENT_ONLY_INSTRUCTIONS)


class FakeClient:
    """Translates every image to "翻訳テキスト N"; the glossary, polishing and summary answers are set below."""

    requests: list = []
    models: list = []  # the model of each request
    glossary = [{"en": "layout analysis", "ja": "レイアウト解析"}, {"en": "attention", "ja": "注意機構"}]
    polish = staticmethod(lambda body: body)  # the yomiyasu rewrite of one block
    document_edits: dict = {}  # the whole-Markdown pass: paragraph id -> new text
    polish_error: Exception | None = None

    def __init__(self, model, api_key, **kwargs):
        self.model = model

    def answer(self, request):
        if FakeClient.polish_error is not None and request.get("instructions") in (
            prompts.POLISH_INSTRUCTIONS,
            *DOCUMENT_INSTRUCTIONS,
        ):
            return FakeClient.polish_error
        if request.get("instructions") == prompts.POLISH_INSTRUCTIONS:
            body = request["text"].split("<本文>\n", 1)[1].rsplit("\n</本文>", 1)[0]
            return f"<本文>\n{FakeClient.polish(body)}\n</本文>"
        if request.get("instructions") in DOCUMENT_INSTRUCTIONS:
            edits = [{"id": pid, "text": text} for pid, text in FakeClient.document_edits.items()]
            return json.dumps({"edits": edits}, ensure_ascii=False)
        return f"翻訳テキスト {len(request['images'])}"

    def generate_batch(self, requests, on_result=None, return_exceptions=False):
        out = []
        for i, request in enumerate(requests):
            FakeClient.requests.append(request)
            FakeClient.models.append(self.model)
            out.append(self.answer(request))
            if on_result:
                on_result(i, out[-1])
        return out

    def generate(self, request):
        FakeClient.requests.append(request)
        FakeClient.models.append(self.model)
        if request.get("instructions") == prompts.GLOSSARY_INSTRUCTIONS:
            return json.dumps({"terms": FakeClient.glossary}, ensure_ascii=False)
        return "【背景】\n- 要約"


PARAGRAPH = "We study how to translate papers into Japanese with layout analysis."


def make_text_pdf(path: Path, paragraph: str = PARAGRAPH) -> Path:
    """One page with a real text layer: title, a paragraph (one line), a reference list."""
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((80, 60), "Real Title From The PDF", fontsize=16)
    page.insert_text((40, 140), paragraph, fontsize=9)
    page.insert_text((40, 300), "References", fontsize=11)
    page.insert_text((40, 330), "[1] A. Author. Exact reference text. 2020.", fontsize=9)
    page.insert_text((40, 345), "[2] B. Author. Another one. 2021.", fontsize=9)
    doc.save(path)
    return path


# boxes in the pixels of the page above rendered at 1000px (750 x 1000; 1.25 px per point)
TEXT_RECOGNITION = {
    "pages": [
        {
            "elements": [
                el("sec_0", [90, 50, 600, 85], 0, "Wrong OCR Titel"),
                el("para", [40, 160, 700, 185], 1),
                el("sec_1", [40, 360, 200, 385], 2, "Referenses"),
                el("reference", [40, 400, 700, 420], 3, "[1] wrong ocr"),
                el("reference", [40, 420, 700, 440], 4, "[2] wrong ocr"),
            ]
        }
    ]
}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.pdf = make_pdf(self.tmp / "paper.pdf")
        self.settings = Settings(page_resolution=1000, max_pages=5)
        FakeClient.requests = []
        FakeClient.models = []
        FakeClient.glossary = [{"en": "layout analysis", "ja": "レイアウト解析"}, {"en": "attention", "ja": "注意機構"}]
        FakeClient.polish = staticmethod(lambda body: body)
        FakeClient.document_edits = {}
        FakeClient.polish_error = None
        self.parse_pages = mock.Mock(return_value=RECOGNITION)
        for target, value in (
            ("parse_pages", self.parse_pages),
            ("OpenAIClient", FakeClient),
            (
                "upload_all_to_gyazo",
                mock.Mock(side_effect=lambda paths, token: [f"https://i.gyazo.com/{p.name}" for p in paths]),
            ),
        ):
            patcher = mock.patch.object(pipeline, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_outputs(self):
        result = pipeline.translate_paper(
            self.pdf, self.tmp / "work", openai_api_key="sk-test", gyazo_token="g", settings=self.settings
        )
        markdown = result.markdown_path.read_text()
        self.assertTrue(markdown.startswith("# Test Paper\n"))  # no text layer: Dolphin's text
        self.assertIn("翻訳テキスト 1", markdown)
        self.assertIn('<img src="figures/fig01.png" width=60% alt="fig01.png">', markdown)
        self.assertIn("## References\n\n- [1] A. B. Title. 2020.", markdown)
        self.assertLess(markdown.index("翻訳テキスト"), markdown.index("figures/fig01.png"))
        self.assertIn("https://i.gyazo.com/fig01.png", result.gyazo_markdown_path.read_text())
        self.assertEqual([p.name for p in result.figure_paths], ["fig01.png"])
        self.assertTrue(result.figure_paths[0].is_file())
        self.assertEqual((result.title, result.summary, result.failed_segments), ("Test Paper", "【背景】\n- 要約", 0))
        self.assertEqual(result.summary_path.read_text(), "【背景】\n- 要約")
        segments = [json.loads(line) for line in (self.tmp / "work/segments.jsonl").read_text().splitlines()]
        self.assertEqual([s["kind"] for s in segments], ["title", "text", "figure", "text", "heading", "reference"])
        self.assertEqual(self.parse_pages.call_args.kwargs["ocr_pages"], [0, 1])  # no text layer anywhere
        self.assertEqual((result.translation_model, result.translation_effort), ("gpt-6.1-terra", "high"))
        summary_request = FakeClient.requests[-1]
        self.assertNotIn("<img", summary_request["text"])
        self.assertNotIn("A. B. Title", summary_request["text"])  # the reference list is not summarized

    def test_the_glossary_has_a_model_of_its_own(self):
        settings = Settings(
            page_resolution=1000, max_pages=5, translation_model="gpt-6-luna", glossary_model="gpt-6.1-sol",
            glossary_effort="high", polish_model="gpt-6-astra", polish_document=False,
        )  # fmt: skip
        self.parse_pages.return_value = TEXT_RECOGNITION
        result = pipeline.translate_paper(
            make_text_pdf(self.tmp / "t.pdf"), self.tmp / "w", openai_api_key="sk", settings=settings
        )
        index = next(
            i for i, r in enumerate(FakeClient.requests) if r.get("instructions") == prompts.GLOSSARY_INSTRUCTIONS
        )
        self.assertEqual(FakeClient.models[index], "gpt-6.1-sol")
        self.assertEqual(FakeClient.requests[index]["gen_kwargs"]["reasoning_effort"], "high")
        translation = next(i for i, r in enumerate(FakeClient.requests) if r.get("images"))
        self.assertEqual(FakeClient.models[translation], "gpt-6-luna")
        self.assertEqual((result.glossary_model, result.glossary_effort), ("gpt-6.1-sol", "high"))

    def test_stopped_during_the_layout_analysis(self):
        token = CancelToken()

        def parse(pages, out_dir, **kwargs):
            kwargs["on_page_done"]()  # page 1 done; the user stops the job
            token.cancel()
            kwargs["on_page_done"]()  # page 2 done: the check stops the analysis here
            raise AssertionError("not reached")

        self.parse_pages.side_effect = parse
        with self.assertRaises(Cancelled):
            pipeline.translate_paper(
                self.pdf, self.tmp / "work", openai_api_key="sk", settings=self.settings, cancel=token
            )
        self.assertEqual(FakeClient.requests, [])  # nothing was sent to OpenAI

    def test_text_layer_is_copied(self):
        pdf = make_text_pdf(self.tmp / "text.pdf")
        self.parse_pages.return_value = TEXT_RECOGNITION
        result = pipeline.translate_paper(pdf, self.tmp / "work2", openai_api_key="sk", settings=self.settings)
        markdown = result.markdown_path.read_text()
        self.assertTrue(markdown.startswith("# Real Title From The PDF\n"))
        self.assertIn("## References\n\n- [1] A. Author. Exact reference text. 2020.\n- [2] B. Author.", markdown)
        self.assertNotIn("wrong ocr", markdown)
        self.assertEqual(self.parse_pages.call_args.kwargs["ocr_pages"], [])  # Dolphin reads no text
        translations = [r for r in FakeClient.requests if r.get("images")]
        self.assertEqual(len(translations), 1)  # only the paragraph
        self.assertIn("論文: Real Title From The PDF", translations[0]["text"])
        # the PDF's own text of the region and the glossary terms it uses go with the image
        self.assertIn(f"<PDFの文字>\n{PARAGRAPH}\n</PDFの文字>", translations[0]["text"])
        self.assertIn("<訳語表>\n- layout analysis: レイアウト解析\n</訳語表>", translations[0]["text"])
        glossary_request = next(
            r for r in FakeClient.requests if r.get("instructions") == prompts.GLOSSARY_INSTRUCTIONS
        )
        self.assertIn("We study how to translate papers", glossary_request["text"])
        self.assertNotIn("Exact reference text", glossary_request["text"])  # not the references
        self.assertNotIn("用語の一覧", glossary_request["text"])  # no term list
        self.assertEqual((result.glossary_terms, result.glossary_list_terms, result.term_list), (2, 0, None))
        self.assertEqual(json.loads((self.tmp / "work2/glossary.json").read_text()), FakeClient.glossary)

    def test_term_list(self):
        terms = self.tmp / "terms.tsv"
        terms.write_text(
            tsv([("文書処理", "レイアウト解析", "layout analysis", "", "", "日: 全3冊一致（A、B、C）")]),
            encoding="utf-8",
        )
        pdf = make_text_pdf(self.tmp / "text.pdf", "Layout analysis first: the layout analysis finds the blocks.")
        self.parse_pages.return_value = TEXT_RECOGNITION
        FakeClient.glossary = [{"en": "layout analysis", "ja": "レイアウト分析"}]
        settings = Settings(page_resolution=1000, max_pages=5, term_list=terms)
        with self.assertLogs("paper2md.terms", "INFO") as logs:
            result = pipeline.translate_paper(pdf, self.tmp / "work3", openai_api_key="sk", settings=settings)
        glossary_request = next(
            r for r in FakeClient.requests if r.get("instructions") == prompts.GLOSSARY_INSTRUCTIONS
        )
        self.assertIn(
            "<用語の一覧>\n- layout analysis: レイアウト解析［文書処理、3冊］\n</用語の一覧>", glossary_request["text"]
        )
        # the model chose another rendering: logged, and saved next to the list's
        logged = "\n".join(logs.output)
        self.assertIn("layout analysis -> レイアウト分析, but the term list has レイアウト解析", logged)
        self.assertEqual((result.glossary_terms, result.glossary_list_terms), (1, 0))
        self.assertTrue(result.term_list.startswith("terms.tsv ("))
        self.assertEqual(
            json.loads((self.tmp / "work3/glossary.json").read_text()),
            [{"en": "layout analysis", "ja": "レイアウト分析", "term_list": ["レイアウト解析"]}],
        )

    def test_default_polishes_the_whole_markdown_only(self):
        FakeClient.polish = staticmethod(lambda body: "使われない")
        FakeClient.document_edits = {"P1": "全体で推敲した訳文 1"}
        result = pipeline.translate_paper(self.pdf, self.tmp / "work", openai_api_key="sk", settings=self.settings)
        self.assertIn("全体で推敲した訳文 1", result.markdown_path.read_text())
        self.assertFalse([r for r in FakeClient.requests if r.get("instructions") == prompts.POLISH_INSTRUCTIONS])
        document = [r for r in FakeClient.requests if r.get("instructions") in DOCUMENT_INSTRUCTIONS]
        self.assertEqual([r["instructions"] for r in document], [prompts.POLISH_DOCUMENT_ONLY_INSTRUCTIONS])
        self.assertEqual((result.polished_blocks, result.polished_document), (False, True))
        self.assertEqual((result.polish_model, result.polish_effort), ("gpt-6.1-sol", "high"))

    def test_polishing_blocks_and_document(self):
        FakeClient.polish = staticmethod(lambda body: body.replace("翻訳テキスト", "推敲した訳文"))
        FakeClient.document_edits = {"P2": "全体で推敲した訳文 1"}
        settings = Settings(page_resolution=1000, polish_segments=True)
        result = pipeline.translate_paper(self.pdf, self.tmp / "work", openai_api_key="sk", settings=settings)
        markdown = result.markdown_path.read_text()
        self.assertIn("推敲した訳文 1", markdown)  # the paragraph (block pass)
        self.assertIn("全体で推敲した訳文 1", markdown)  # the caption (whole-Markdown pass)
        self.assertNotIn("翻訳テキスト", markdown)
        self.assertEqual((result.polished_blocks, result.polished_document), (True, True))
        self.assertEqual((result.polish_stats["rewritten"], result.polish_stats["document_edits"]), (2, 1))
        segments = [json.loads(line) for line in (self.tmp / "work/segments.jsonl").read_text().splitlines()]
        self.assertEqual((segments[1]["translation"], segments[1]["markdown"]), ("翻訳テキスト 1", "推敲した訳文 1"))
        document = [r for r in FakeClient.requests if r.get("instructions") in DOCUMENT_INSTRUCTIONS]
        self.assertEqual([r["instructions"] for r in document], [prompts.POLISH_DOCUMENT_INSTRUCTIONS])
        self.assertIn("[P1] 推敲した訳文 1", document[0]["text"])
        self.assertIn("（図表）", document[0]["text"])
        self.assertNotIn("A. B. Title", document[0]["text"])  # references are not sent

    def test_polishing_off_and_failures(self):
        settings = Settings(page_resolution=1000, polish_segments=False, polish_document=False, glossary=False)
        off = pipeline.translate_paper(self.pdf, self.tmp / "off", openai_api_key="sk", settings=settings)
        instructions = [r.get("instructions") for r in FakeClient.requests]
        self.assertNotIn(prompts.GLOSSARY_INSTRUCTIONS, instructions)
        self.assertFalse(set(instructions) & {prompts.POLISH_INSTRUCTIONS, *DOCUMENT_INSTRUCTIONS})
        self.assertEqual((off.polish_model, off.polish_stats, off.polish_error), (None, None, None))

        FakeClient.polish_error = ModelNotAvailableError("gpt-6.1-terra is not available")
        failed = pipeline.translate_paper(self.pdf, self.tmp / "failed", openai_api_key="sk", settings=self.settings)
        self.assertIn("翻訳テキスト 1", failed.markdown_path.read_text())  # the translation is used as it is
        self.assertIsNone(failed.polish_model)
        self.assertIn("ModelNotAvailableError", failed.polish_error)

    def test_without_gyazo(self):
        result = pipeline.translate_paper(self.pdf, self.tmp / "work", openai_api_key="sk-test", settings=self.settings)
        self.assertIsNone(result.gyazo_markdown_path)
        pipeline.upload_all_to_gyazo.assert_not_called()

    def test_limits(self):
        with self.assertRaises(PdfError):
            pipeline.translate_paper(
                self.pdf, self.tmp / "work", openai_api_key="sk", settings=Settings(page_resolution=1000, max_pages=1)
            )
        with self.assertRaises(PdfError):
            check_limits(self.pdf, max_pages=1, max_mb=10)
        not_pdf = self.tmp / "x.pdf"
        not_pdf.write_bytes(b"<html>login</html>")
        with self.assertRaises(PdfError):
            check_limits(not_pdf, max_pages=10, max_mb=10)


if __name__ == "__main__":
    unittest.main()
