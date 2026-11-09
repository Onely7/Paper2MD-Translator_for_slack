"""Layout grouping (Dolphin-v2 output -> segments) and segment translation."""

import unittest

import openai

from paper2md import prompts
from paper2md.config import Settings
from paper2md.layout import build_segments, normalize_label
from paper2md.llm import InsufficientQuotaError
from paper2md.translation import translate_segments


def el(label, bbox, order, text="", tags=None):
    return {"label": label, "bbox": bbox, "reading_order": order, "text": text, "tags": tags or []}


# Two pages shaped like Dolphin-v2 output (pixel boxes of a 4000px-high page).
RECOGNITION = {
    "pages": [
        {
            "elements": [
                el("para", [600, 300, 2400, 500], 0),  # a notice above the title
                el("sec_0", [1000, 700, 2000, 800], 1, "Attention Is All You Need"),
                el("para", [500, 900, 2500, 1300], 2, tags=["author", "author_affili"]),
                el("sec_1", [1400, 1400, 1700, 1460], 3, "Abstract", ["paper_abstract"]),
                el("para", [700, 1500, 2400, 2200], 4, tags=["paper_abstract"]),
                # reading order 5 was skipped by Dolphin (tiny crop): later elements must survive
                el("sec_1", [500, 2300, 900, 2360], 6, "1 Introduction"),
                el("para", [500, 2380, 2500, 2600], 7),
                el("fig", [500, 2650, 2500, 2900], 8),
                el("cap", [500, 2910, 2500, 2960], 9),
                el("equ", [900, 3000, 2100, 3070], 10),
                el("para", [500, 3100, 2500, 3500], 11),
                el("fnote", [500, 3700, 2500, 3750], 12),
                el("foot", [1500, 3800, 1600, 3850], 13, tags=["page_num"]),
                el("watermark", [50, 1000, 150, 2800], 14, tags=["meta_num"]),
            ]
        },
        {
            "elements": [
                el("para", [500, 200, 2500, 400], 0),  # the introduction continues on page 2
                el("sec_1", [500, 500, 900, 560], 1, "References"),
                el("reference", [500, 600, 2500, 700], 2, "[1] A. Author. Paper one. 2020."),
                el("reference", [500, 720, 2500, 820], 3, "[2] B. Author. Paper two. 2021."),
                el("catalogue", [500, 900, 2500, 3500], 4, "[3] C. Author. Paper three. 2022."),
            ]
        },
    ]
}


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.segments = build_segments(RECOGNITION, page_height=4000)

    def test_labels(self):
        self.assertEqual(normalize_label("sec_0"), "title")
        self.assertEqual(normalize_label("para", ["author"]), "author")
        self.assertIsNone(normalize_label("watermark"))
        self.assertEqual(normalize_label("something_new"), "para")

    def test_order_and_kinds(self):
        kinds = [(s.kind, s.labels) for s in self.segments]
        self.assertEqual(kinds[0], ("title", ["title"]))
        self.assertEqual(kinds[1], ("text", ["para"]))  # content above the title is kept, after it
        self.assertEqual(kinds[2], ("text", ["author"]))
        self.assertEqual(kinds[3], ("text", ["sec", "para"]))  # Abstract heading + text
        # the introduction is one segment around its figure and onto page 2; the figure follows it
        intro = kinds[4]
        self.assertEqual(intro, ("text", ["sec", "para", "equ", "para", "para"]))
        self.assertEqual(self.segments[4].pages, [0, 0, 1])
        self.assertEqual([k[0] for k in kinds[5:8]], ["figure", "text", "text"])  # figure, caption, footnote
        self.assertEqual(kinds[8][0], "reference")
        self.assertEqual(len(kinds), 9)

    def test_figure_includes_its_caption(self):
        figure = self.segments[5]
        self.assertGreaterEqual(figure.bboxes[0][3], 2960)

    def test_reference_text(self):
        reference = self.segments[8]
        self.assertTrue(reference.has_all_texts)
        self.assertIn("[3] C. Author", reference.text)

    def test_thresholds_scale_with_resolution(self):
        half = {
            "pages": [
                {"elements": [{**e, "bbox": [v // 2 for v in e["bbox"]]} for e in p["elements"]]}
                for p in RECOGNITION["pages"]
            ]
        }
        self.assertEqual([s.labels for s in build_segments(half, page_height=2000)], [s.labels for s in self.segments])


class FakeClient:
    def __init__(self, answer=None):
        self.requests = []
        self.answer = answer

    def generate_batch(self, requests, on_result=None, return_exceptions=False):
        results = []
        for i, request in enumerate(requests):
            self.requests.append(request)
            result = self.answer(request) if self.answer else None
            if result is None:
                if "refine the given title" in request["text"]:
                    result = "# Attention Is All You Need"
                elif "reference entries" in request["text"]:
                    result = "## Reference\n- [1]"
                else:
                    result = "```markdown\n翻訳\n```"
            if on_result:
                on_result(i, result)
            results.append(result)
        return results


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.segments = build_segments(RECOGNITION, page_height=4000)
        for i, segment in enumerate(self.segments):
            segment.images = [f"/tmp/crop_{i}.png"]
        self.settings = Settings(translation_model="gpt-6.1-terra")

    def test_requests(self):
        fake = FakeClient()
        parts, failed = translate_segments(self.segments, fake, self.settings)
        self.assertEqual(failed, 0)
        self.assertEqual(len(fake.requests), 8)  # everything but the figure
        self.assertEqual(parts[0], "# Attention Is All You Need")
        self.assertEqual(parts[1], "翻訳")  # the ```markdown fence is removed
        self.assertNotIn(5, parts)  # the figure
        translation = next(r for r in fake.requests if r.get("instructions"))
        self.assertEqual(translation["instructions"], prompts.TRANSLATION_INSTRUCTIONS)
        self.assertEqual(translation["gen_kwargs"]["reasoning_effort"], "none")

    def test_failures_are_marked(self):
        error = openai.APIConnectionError(request=None)
        fake = FakeClient(lambda r: error if r.get("instructions") else None)
        parts, failed = translate_segments(self.segments, fake, self.settings)
        self.assertEqual(failed, 6)
        self.assertIn("翻訳に失敗しました", parts[1])

    def test_fatal_error_aborts(self):
        fake = FakeClient(lambda r: InsufficientQuotaError("quota"))
        with self.assertRaises(InsufficientQuotaError):
            translate_segments(self.segments, fake, self.settings)

    def test_untranslatable_answer_is_dropped(self):
        sentence = f"申し訳ありません。{prompts.UNTRANSLATABLE_SENTINEL}。"
        fake = FakeClient(lambda r: sentence if r.get("instructions") else None)
        parts, failed = translate_segments(self.segments, fake, self.settings)
        self.assertEqual((failed, parts[1]), (0, ""))

    def test_reference_without_text_is_read_from_images(self):
        self.segments[8].texts[-1] = ""
        fake = FakeClient()
        translate_segments(self.segments, fake, self.settings)
        self.assertTrue(any("show the reference list" in r["text"] for r in fake.requests))


if __name__ == "__main__":
    unittest.main()
