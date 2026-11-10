"""Text copied from the PDF: lines in a box, headings, reference entries (real PDFs made with PyMuPDF)."""

import tempfile
import unittest
from pathlib import Path

import pymupdf

from paper2md.textlayer import (
    LIGATURES,
    TextLayer,
    _RefLine,
    clean_title,
    join_lines,
    reference_entries,
    split_ocr_reference_entries,
    split_reference_entries,
)

SIZE = 1000  # pages rendered with the longest side = 1000 px
SCALE = SIZE / 792  # a 612 x 792 pt page


def px(*box_pt):
    return [round(v * SCALE) for v in box_pt]


def make_pdf(path: Path, filled: bool = True) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    if filled:  # enough text for a text layer worth using
        page.insert_text((50, 40), "This conference paper studies pre-training and other methods in depth.", fontsize=9)
    page.insert_text((50, 70), "3.1", fontsize=11)
    page.insert_text((90, 70), "Encoder and Decoder", fontsize=11)  # number and title split by a wide gap
    page.insert_text((50, 100), "References", fontsize=11)
    left = [
        (50, "[1] A. Author and B. Author. A study of con-"),
        (62, "ference papers. In Proc. of X, 2020."),
        (50, "[2] C. Author. Self-"),
        (62, "training at https://github.com/"),
        (62, "org/repo, 2021."),
        (50, "[3] D. Author. Another title. 2022."),  # outside Dolphin's (truncated) box
    ]
    for i, (x, text) in enumerate(left):
        page.insert_text((x, 120 + 12 * i), text, fontsize=9)
    right = [
        (320, "[4] E. Author. Right column entry. 2023."),
        (320, "[5] F. Author. Last entry with a long"),
        (332, "title. 2024."),
    ]
    for i, (x, text) in enumerate(right):
        page.insert_text((x, 120 + 12 * i), text, fontsize=9)
    page.insert_text((300, 770), "7", fontsize=9)  # page number
    doc.save(path)
    return path


class TextLayerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.layer = TextLayer(make_pdf(self.tmp / "paper.pdf"), SIZE)
        self.addCleanup(self.layer.close)

    def test_heading_pieces_are_one_line(self):
        self.assertTrue(self.layer.usable(0))
        self.assertEqual(self.layer.text(0, px(45, 58, 300, 74)), "3.1 Encoder and Decoder")

    def test_reference_entries_fill_dolphins_gaps(self):
        elements = [
            {"page": 0, "bbox": px(45, 110, 300, 160)},  # [1] and [2] only: [3] is missing from the box
            {"page": 0, "bbox": px(315, 110, 560, 150)},
        ]
        exclude = {0: [px(45, 88, 200, 104), px(290, 760, 310, 775)]}  # the heading and the page number
        self.assertEqual(
            reference_entries(self.layer, elements, exclude),
            [
                "[1] A. Author and B. Author. A study of conference papers. In Proc. of X, 2020.",
                "[2] C. Author. Self-training at https://github.com/org/repo, 2021.",
                "[3] D. Author. Another title. 2022.",
                "[4] E. Author. Right column entry. 2023.",
                "[5] F. Author. Last entry with a long title. 2024.",
            ],
        )

    def test_scanned_page_is_not_used(self):
        layer = TextLayer(make_pdf(self.tmp / "sparse.pdf", filled=False), SIZE)
        self.addCleanup(layer.close)
        self.assertTrue(layer.usable(0))  # the reference list alone has enough text
        empty = pymupdf.open()
        empty.new_page()
        empty.save(self.tmp / "scan.pdf")
        scan = TextLayer(self.tmp / "scan.pdf", SIZE)
        self.addCleanup(scan.close)
        self.assertFalse(scan.usable(0))
        self.assertEqual(scan.text(0, [0, 0, 100, 100]), "")
        self.assertIsNone(reference_entries(scan, [{"page": 0, "bbox": [0, 0, 500, 500]}]))


class SplittingTests(unittest.TestCase):
    def test_author_year_with_hanging_indent(self):
        lines = [
            _RefLine("Alan Akbik, Duncan Blythe, and Roland Vollgraf.", 0, True),
            _RefLine("2018. Contextual string embeddings for sequence", 7, False),  # a year, not a label
            _RefLine("labeling. In COLING.", 7, False),
            _RefLine("Rami Al-Rfou, Dokook Choe. 2018. Character-level lan-", 0, True),
            _RefLine("guage modeling. arXiv preprint.", 7, False),
            _RefLine("for natural language understanding. In EMNLP.", 7, True),  # rest of an entry, next column
        ]
        self.assertEqual(
            split_reference_entries(lines, frozenset({"language"})),
            [
                "Alan Akbik, Duncan Blythe, and Roland Vollgraf. 2018. Contextual string embeddings for sequence "
                "labeling. In COLING.",
                "Rami Al-Rfou, Dokook Choe. 2018. Character-level language modeling. arXiv preprint. "
                "for natural language understanding. In EMNLP.",
            ],
        )

    def test_numbered_labels_must_follow_each_other(self):
        lines = [_RefLine("1. A. Author. Title.", 0, True), _RefLine("12. Retrieved from the web.", 0, False)]
        lines.append(_RefLine("2. B. Author. Other title.", 0, True))
        self.assertEqual(
            split_reference_entries(lines),
            ["1. A. Author. Title. 12. Retrieved from the web.", "2. B. Author. Other title."],
        )

    def test_ocr_text_of_scanned_pages(self):
        texts = [
            "[1] A. Author. Title of the\npaper. 2020.\n[2] B. Author. Other. 2021.",
            "[3] C. Author. Third. 2022.",
        ]
        self.assertEqual(
            split_ocr_reference_entries(texts),
            ["[1] A. Author. Title of the paper. 2020.", "[2] B. Author. Other. 2021.", "[3] C. Author. Third. 2022."],
        )

    def test_helpers(self):
        self.assertEqual("difﬁcult".translate(LIGATURES), "difficult")
        self.assertEqual(
            join_lines(["Multi-", "head attention", "and lan-", "guage"]), "Multi-head attention and language"
        )
        self.assertEqual(clean_title("Llama 2: Open Foundation Models"), "Llama 2: Open Foundation Models")
        self.assertEqual(clean_title("Visual Instruction Tuning*"), "Visual Instruction Tuning")


if __name__ == "__main__":
    unittest.main()
