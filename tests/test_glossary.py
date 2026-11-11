"""The glossary of a paper's terms, and headings that Dolphin-v2 labelled as page headers."""

import json
import tempfile
import unittest
from pathlib import Path

import pymupdf

from paper2md import prompts
from paper2md.config import Settings
from paper2md.glossary import build_glossary, glossary_records, paper_text
from paper2md.layout import Segment, promote_header_headings
from paper2md.llm import ModelNotAvailableError
from paper2md.terms import parse_term_list
from paper2md.textlayer import TextLayer
from tests.test_terms import tsv


def segment(kind, label="para", source_text="", layer_text=""):
    return Segment(kind, label, [label], [], [], [], source_text=source_text, layer_text=layer_text)


class FakeClient:
    def __init__(self, answer):
        self.answer, self.requests = answer, []

    def generate(self, request):
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class GlossaryTests(unittest.TestCase):
    SEGMENTS = [
        segment("title", "title", "Attention Is All You Need"),
        segment("heading", "sec", "1 Introduction"),
        segment("text", layer_text="The encoder maps an input sequence."),
        segment("reference", "reference"),
    ]

    def test_paper_text(self):
        self.assertEqual(
            paper_text(self.SEGMENTS),
            "Attention Is All You Need\n\n1 Introduction\n\nThe encoder maps an input sequence.",
        )

    def test_build(self):
        client = FakeClient(json.dumps({"terms": [{"en": "encoder", "ja": "エンコーダ"}]}))
        glossary = build_glossary(self.SEGMENTS, client, Settings(), "Attention Is All You Need")
        self.assertEqual(glossary, [("encoder", "エンコーダ")])
        request = client.requests[0]
        self.assertEqual(request["instructions"], prompts.GLOSSARY_INSTRUCTIONS)
        self.assertTrue(request["text"].startswith("論文: Attention Is All You Need\n\n<本文>\n"))
        self.assertEqual(request["gen_kwargs"]["text"]["format"]["name"], "glossary")

    def test_term_list_rows_go_with_the_request(self):
        segments = [
            segment("title", "title", "Attention Is All You Need"),
            segment("text", layer_text="The encoder maps a sequence; the encoder is a stack. Explanatory variable."),
        ]
        terms = parse_term_list(tsv(), "terms.tsv", "abc")
        client = FakeClient(
            json.dumps({"terms": [{"en": "encoder", "ja": "エンコーダ"}, {"en": "stack", "ja": "スタック"}]})
        )
        glossary = build_glossary(segments, client, Settings(), "Attention Is All You Need", terms)
        text = client.requests[0]["text"]
        # every term of the list found in the paper is a candidate, most used first
        self.assertIn("</本文>\n\n<用語の一覧>\n- encoder: エンコーダ［深層学習、8冊］\n", text)
        self.assertIn("- explanatory variable: 説明変数［確率・統計、1冊］\n</用語の一覧>", text)  # used once
        self.assertEqual(
            glossary_records(glossary, terms),
            [{"en": "encoder", "ja": "エンコーダ", "term_list": ["エンコーダ"]}, {"en": "stack", "ja": "スタック"}],
        )
        build_glossary(segments, client, Settings(), "Attention Is All You Need")  # without the list
        self.assertNotIn("用語の一覧", client.requests[1]["text"])

    def test_failures(self):
        self.assertEqual(build_glossary(self.SEGMENTS, FakeClient("not json"), Settings()), [])
        self.assertEqual(build_glossary([segment("figure", "fig")], FakeClient("{}"), Settings()), [])  # no text
        with self.assertRaises(ModelNotAvailableError):
            build_glossary(self.SEGMENTS, FakeClient(ModelNotAvailableError("no")), Settings())


class HeaderHeadingTests(unittest.TestCase):
    def test_only_a_heading_set_as_a_header_is_promoted(self):
        path = Path(tempfile.mkdtemp()) / "headers.pdf"
        doc = pymupdf.open()
        headers = [
            ("Preprint under review", 8, "helv"),  # a running header (pages 1 and 2)
            ("Preprint under review", 8, "helv"),
            ("Attention Visualizations", 12, "tibo"),  # a heading at the top of the page (bold, larger)
            ("Under review at a conference", 8, "helv"),  # on one page only, but small
        ]
        for text, size, font in headers:
            page = doc.new_page(width=600, height=800)
            page.insert_text((40, 40), text, fontsize=size, fontname=font)
            for line in range(20):
                page.insert_text((40, 100 + 14 * line), "Body text of the paper, long enough to count.", fontsize=10)
        doc.save(path)
        # boxes in the pixels of pages rendered at 1000 px (1.25 px per point)
        recognition = {
            "pages": [
                {
                    "elements": [
                        {"label": "header", "bbox": [40, 25, 700, 70]},
                        {"label": "foot", "bbox": [0, 960, 750, 990]},
                    ]
                }
                for _ in headers
            ]
        }
        with TextLayer(path, 1000) as layer:
            self.assertEqual(layer.body_font_size, 10.0)
            promoted = promote_header_headings(recognition, layer)
        self.assertEqual(promoted, ["Attention Visualizations"])
        labels = [page["elements"][0]["label"] for page in recognition["pages"]]
        self.assertEqual(labels, ["header", "header", "sec_1", "header"])


if __name__ == "__main__":
    unittest.main()
