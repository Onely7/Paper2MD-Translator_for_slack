"""Layout grouping (Dolphin-v2 output -> segments), printed text and segment translation."""

import unittest

import openai

from paper2md import prompts
from paper2md.config import Settings
from paper2md.layout import attach_printed_text, build_segments, normalize_label
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
                el("para", [20, 1000, 80, 2800], 15, tags=["meta_num", "meta_pub_date"]),  # arXiv stamp as para
            ]
        },
        {
            "elements": [
                el("para", [500, 200, 2500, 400], 0),  # the introduction continues on page 2
                el("sec_1", [500, 500, 900, 560], 1, "References"),
                el("reference", [500, 600, 2500, 700], 2, "[1] A. Author. Paper one. 2020."),
                el("reference", [500, 720, 2500, 820], 3, "[2] B. Author. Paper two. 2021."),
                el("catalogue", [500, 900, 2500, 3500], 4, "[3] C. Author. Paper three.\n2022."),
            ]
        },
    ]
}
KINDS = [
    ("title", ["title"]),
    ("text", ["para"]),  # content above the title is kept, after it
    ("text", ["author"]),
    ("heading", ["sec"]),  # Abstract
    ("text", ["para"]),
    ("heading", ["sec"]),  # 1 Introduction
    ("text", ["para", "equ", "para", "para"]),  # around the figure and onto page 2
    ("figure", ["fig"]),
    ("text", ["cap"]),
    ("text", ["fnote"]),
    ("heading", ["sec"]),  # References
    ("reference", ["reference", "reference", "reference"]),  # the catalogue is part of the list
]


def segments_with_text():
    segments = build_segments(RECOGNITION, page_height=4000)
    attach_printed_text(segments, layer=None)  # no PDF: Dolphin's text is used
    return segments


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.segments = segments_with_text()

    def test_labels(self):
        self.assertEqual(normalize_label("sec_0"), "title")
        self.assertEqual(normalize_label("para", ["author"]), "author")
        self.assertIsNone(normalize_label("watermark"))
        self.assertIsNone(normalize_label("para", ["meta_num"]))
        self.assertEqual(normalize_label("something_new"), "para")

    def test_order_and_kinds(self):
        self.assertEqual([(s.kind, s.labels) for s in self.segments], KINDS)
        self.assertEqual(self.segments[6].pages, [0, 0, 1])

    def test_figure_includes_its_caption(self):
        self.assertGreaterEqual(self.segments[7].bboxes[0][3], 2960)

    def test_printed_text(self):
        self.assertEqual(
            [s.source_text for s in self.segments if s.kind in ("title", "heading")],
            ["Attention Is All You Need", "Abstract", "1 Introduction", "References"],
        )
        self.assertEqual(
            self.segments[11].entries,
            ["[1] A. Author. Paper one. 2020.", "[2] B. Author. Paper two. 2021.", "[3] C. Author. Paper three. 2022."],
        )
        # the reference segment knows the other boxes of its page (to skip them in the PDF text)
        self.assertIn([500, 500, 900, 560], self.segments[11].exclude[1])

    def test_paragraph_after_references_is_translated(self):
        pages = [{"elements": [*RECOGNITION["pages"][1]["elements"], el("para", [500, 3600, 2500, 3900], 5, "")]}]
        pages[0]["elements"][-1]["text"] = "We thank the reviewers for their helpful comments and suggestions."
        kinds = [s.kind for s in build_segments({"pages": pages}, page_height=4000)]
        self.assertEqual(kinds, ["text", "heading", "reference", "text"])

    def test_long_heading_is_translated(self):
        recognition = {"pages": [{"elements": [el("sec_1", [500, 500, 2500, 900], 0, "word " * 60)]}]}
        segments = build_segments(recognition)
        attach_printed_text(segments, layer=None)
        self.assertEqual((segments[0].kind, segments[0].source_text), ("text", ""))

    def test_title_on_two_elements(self):
        recognition = {
            "pages": [
                {
                    "elements": [
                        el("sec_0", [500, 100, 2500, 200], 0, "LLaVA:"),
                        el("sec_0", [500, 210, 2500, 300], 1, "## Visual Instruction Tuning∗"),
                    ]
                }
            ]
        }
        segments = build_segments(recognition)
        attach_printed_text(segments, layer=None)
        self.assertEqual([(s.kind, s.source_text) for s in segments], [("title", "LLaVA: Visual Instruction Tuning")])

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
                result = "```markdown\n翻訳\n```"
            if on_result:
                on_result(i, result)
            results.append(result)
        return results


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.segments = segments_with_text()
        for i, segment in enumerate(self.segments):
            segment.images = [f"/tmp/crop_{i}.png"]
        self.settings = Settings(translation_model="gpt-6.1-terra")

    def test_requests(self):
        fake = FakeClient()
        parts, failed = translate_segments(self.segments, fake, self.settings)
        self.assertEqual(failed, 0)
        self.assertEqual(len(fake.requests), 6)  # text only: title, headings and references are printed
        self.assertEqual(parts[0], "# Attention Is All You Need")
        self.assertEqual(parts[1], "翻訳")  # the ```markdown fence is removed
        self.assertEqual((parts[3], parts[5], parts[10]), ("## Abstract", "## 1 Introduction", "## References"))
        self.assertEqual(parts[11].splitlines()[0], "- [1] A. Author. Paper one. 2020.")
        self.assertNotIn(7, parts)  # the figure
        translation = fake.requests[-1]
        self.assertEqual(translation["instructions"], prompts.TRANSLATION_INSTRUCTIONS)
        self.assertEqual(translation["gen_kwargs"]["reasoning_effort"], "high")
        intro = fake.requests[3]["text"]  # the introduction: the paper title and its section are given
        self.assertIn("論文: Attention Is All You Need", intro)
        self.assertIn("節: 1 Introduction", intro)
        self.assertNotIn("節:", fake.requests[0]["text"])  # the notice above the first heading

    def test_a_repeated_section_heading_is_dropped(self):
        # the model wrote the section heading given in the request, which is a segment of its own
        def answer(request):
            return "## 1 Introduction\n\n翻訳" if "節: 1 Introduction" in request["text"] else None

        parts, _ = translate_segments(self.segments, FakeClient(answer), self.settings)
        self.assertEqual((parts[5], parts[6]), ("## 1 Introduction", "翻訳"))

    def test_failures_are_marked(self):
        error = openai.APIConnectionError(request=None)
        fake = FakeClient(lambda r: error)
        parts, failed = translate_segments(self.segments, fake, self.settings)
        self.assertEqual(failed, 6)
        self.assertIn("翻訳に失敗しました", parts[1])
        self.assertEqual(parts[0], "# Attention Is All You Need")

    def test_fatal_error_aborts(self):
        fake = FakeClient(lambda r: InsufficientQuotaError("quota"))
        with self.assertRaises(InsufficientQuotaError):
            translate_segments(self.segments, fake, self.settings)

    def test_empty_answer_is_dropped(self):
        fake = FakeClient(lambda r: f" {prompts.EMPTY_MARKER}\n")
        parts, failed = translate_segments(self.segments, fake, self.settings)
        self.assertEqual((failed, parts[1]), (0, ""))

    def test_unreadable_text_is_transcribed_not_translated(self):
        self.segments[0].source_text = ""
        self.segments[5].source_text = ""
        self.segments[11].entries = []
        answers = {
            prompts.TITLE_TRANSCRIPTION: "# Attention Is All You Need",
            prompts.HEADING_TRANSCRIPTION: "1 Introduction",
            prompts.REFERENCE_TRANSCRIPTION: "## References\n- [1] A. Author. Paper one. 2020.",
        }
        fake = FakeClient(lambda r: answers.get(r["text"]))
        parts, _ = translate_segments(self.segments, fake, self.settings)
        transcriptions = [r for r in fake.requests if r["text"] in answers]
        self.assertEqual(len(transcriptions), 3)
        self.assertTrue(all("instructions" not in r for r in transcriptions))  # not the translation prompt
        self.assertEqual({r["gen_kwargs"]["reasoning_effort"] for r in transcriptions}, {"none"})
        self.assertEqual(parts[0], "# Attention Is All You Need")
        self.assertEqual(parts[5], "## 1 Introduction")
        self.assertEqual(parts[11], "- [1] A. Author. Paper one. 2020.")  # its heading is a segment of its own


if __name__ == "__main__":
    unittest.main()
