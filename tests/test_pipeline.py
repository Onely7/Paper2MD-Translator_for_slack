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
    def __init__(self, model, api_key, **kwargs):
        self.model = model

    def generate_batch(self, requests, on_result=None, return_exceptions=False):
        out = []
        for i, request in enumerate(requests):
            if "refine the given title" in request["text"]:
                out.append("# Test Paper")
            elif "reference entries" in request["text"]:
                out.append("## Reference\n- [1] A. B. Title. 2020.")
            else:
                out.append(f"翻訳テキスト {len(request['images'])}")
            if on_result:
                on_result(i, out[-1])
        return out

    def generate(self, request):
        return "【背景】\n- 要約"


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.pdf = make_pdf(self.tmp / "paper.pdf")
        self.settings = Settings(page_resolution=1000, max_pages=5)
        for target, value in (
            ("parse_pages", mock.Mock(return_value=RECOGNITION)),
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
        self.assertTrue(markdown.startswith("# Test Paper\n"))
        self.assertIn("翻訳テキスト 1", markdown)
        self.assertIn('<img src="figures/fig01.png" width=60% alt="fig01.png">', markdown)
        self.assertIn("## Reference", markdown)
        self.assertLess(markdown.index("翻訳テキスト"), markdown.index("figures/fig01.png"))
        self.assertIn("https://i.gyazo.com/fig01.png", result.gyazo_markdown_path.read_text())
        self.assertEqual([p.name for p in result.figure_paths], ["fig01.png"])
        self.assertTrue(result.figure_paths[0].is_file())
        self.assertEqual((result.title, result.summary, result.failed_segments), ("Test Paper", "【背景】\n- 要約", 0))
        self.assertEqual(result.summary_path.read_text(), "【背景】\n- 要約")
        segments = [json.loads(line) for line in (self.tmp / "work/segments.jsonl").read_text().splitlines()]
        self.assertEqual([s["kind"] for s in segments], ["title", "text", "figure", "text", "reference"])

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
