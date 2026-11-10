"""translate_paper end to end on a real PDF, with Dolphin and the OpenAI API faked."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pymupdf

from paper2md import pipeline
from paper2md.config import Settings
from paper2md.pdf import PdfError, check_limits


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


class FakeClient:
    requests: list = []

    def __init__(self, model, api_key, **kwargs):
        self.model = model

    def generate_batch(self, requests, on_result=None, return_exceptions=False):
        out = []
        for i, request in enumerate(requests):
            FakeClient.requests.append(request)
            out.append(f"翻訳テキスト {len(request['images'])}")
            if on_result:
                on_result(i, out[-1])
        return out

    def generate(self, request):
        FakeClient.requests.append(request)
        return "【背景】\n- 要約"


def make_text_pdf(path: Path) -> Path:
    """One page with a real text layer: title, a paragraph, a reference list."""
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((80, 60), "Real Title From The PDF", fontsize=16)
    page.insert_text((40, 140), "We study how to translate papers into Japanese with layout analysis.", fontsize=9)
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
        self.assertEqual((result.translation_model, result.translation_effort), ("gpt-6.1-terra", "none"))
        summary_request = FakeClient.requests[-1]
        self.assertNotIn("<img", summary_request["text"])
        self.assertNotIn("A. B. Title", summary_request["text"])  # the reference list is not summarized

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
