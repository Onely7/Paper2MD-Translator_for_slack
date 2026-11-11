"""
PDF -> Japanese Markdown + summary.

    workdir/
        dolphin/recognition.json   Dolphin-v2 layout analysis
        crops/                     page regions sent to the translation model
        segments.jsonl             segments with their translation and polished text (for debugging)
        glossary.json              the glossary of the paper's terms (when one was made), with the
                                   term list's renderings of the terms the list has
        output/
            paper.md               Markdown; figures linked as figures/figNN.png
                                   (title, headings and references copied from the PDF)
            paper.gyazo.md         the same with Gyazo image URLs (with a Gyazo token, when a
                                   figure could be uploaded)
            figures/figNN.png
            summary.md

Steps: layout analysis (Dolphin-v2) -> glossary (``Settings.glossary_model``) -> translation of each block
-> [yomiyasu polishing of each block] -> one Markdown document -> [yomiyasu
polishing of the whole Markdown] -> section references and spaces between
Japanese and English (``normalize_text``) -> prettier -> summary.  The two polishing
passes are switched by ``Settings.polish_segments`` / ``polish_document``;
when polishing fails, the translation is used as it is.

``cancel`` (``cancel.CancelToken``) stops the run with ``Cancelled``: after the
page being analysed, before each stage, and at once during OpenAI requests.
``meter`` (``usage.UsageMeter``) collects the tokens and cost of every OpenAI
request, also of a run that stops early.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .cancel import CancelToken
from .config import Settings
from .document import parse_pages
from .figures import export_figures, img_tag, upload_all_to_gyazo
from .glossary import build_glossary, glossary_records
from .layout import TEXT_LABELS, attach_printed_text, build_segments, crop_segments, promote_header_headings
from .llm import OpenAIClient
from .markdown import finalize_markdown, first_heading, format_markdown, normalize_text, replace_figure_tags
from .pdf import PdfError, render_pages
from .polish import PolishStats, polish_document, polish_segments
from .progress import ProgressFactory, console_progress
from .terms import load_term_list
from .textlayer import TextLayer
from .translation import summarize, translate_segments
from .usage import UsageMeter

logger = logging.getLogger(__name__)


@dataclass
class PaperResult:
    summary: str
    markdown_path: Path
    summary_path: Path
    figure_paths: list[Path]
    gyazo_markdown_path: Path | None = None
    title: str | None = None
    failed_segments: int = 0  # translations that failed (the result is not cached)
    gyazo_failures: int = 0  # figures not uploaded to Gyazo (still in figures/)
    translation_model: str = ""
    translation_effort: str | None = None
    summary_model: str = ""
    summary_effort: str | None = None
    polish_model: str | None = None  # None: not polished
    polish_effort: str | None = None
    polished_blocks: bool = False  # the yomiyasu passes that ran
    polished_document: bool = False
    polish_stats: dict | None = None  # PolishStats.to_json()
    polish_error: str | None = None  # why polishing stopped (the translation is used as it is)
    glossary_terms: int = 0  # terms in the glossary used for the translation
    glossary_model: str | None = None  # the model that made the glossary (None: no glossary)
    glossary_effort: str | None = None
    glossary_list_terms: int = 0  # of which rendered as the term list renders them
    term_list: str | None = None  # the term list used for the glossary (file name and digest)


def _assemble(count: int, parts: dict[int, str], figures: dict[int, str]) -> str:
    """Markdown of all segments in order (figures as <img> tags)."""
    return finalize_markdown("\n\n".join(filter(None, (figures.get(i) or parts.get(i, "") for i in range(count)))))


def _polish_failure(error: Exception) -> str:
    logger.warning("Polishing stopped: %s: %s", type(error).__name__, error)
    return f"{type(error).__name__}: {error}"[:300]


def write_gyazo_markdown(
    markdown_path: Path, figure_paths: list[Path], token: str, out_path: Path
) -> tuple[Path | None, int]:
    """
    Upload the figures to Gyazo and write the Markdown with their URLs to ``out_path``.

    Returns (``out_path``, or None when no figure could be uploaded; the number of figures that failed).
    """
    urls = upload_all_to_gyazo(figure_paths, token)
    tags = {
        img_tag(f"figures/{path.name}", path.name): img_tag(url, path.name)
        for path, url in zip(figure_paths, urls, strict=True)
        if url
    }
    failures = sum(url is None for url in urls)
    if not tags:
        return None, failures
    out_path.write_text(replace_figure_tags(markdown_path.read_text(encoding="utf-8"), tags), encoding="utf-8")
    return out_path, failures


def translate_paper(
    pdf_path: str | Path,
    workdir: str | Path,
    *,
    openai_api_key: str | None,
    gyazo_token: str | None = None,
    settings: Settings | None = None,
    progress: ProgressFactory = console_progress,
    cancel: CancelToken | None = None,
    meter: UsageMeter | None = None,
) -> PaperResult:
    """Run the whole pipeline; every file is written under ``workdir``."""
    settings = settings or Settings.from_env()
    if not openai_api_key:
        raise ValueError("OpenAI API key is not set.")
    cancel = cancel or CancelToken()  # never set when nobody can stop the run

    def client(task: str, model: str, **kwargs) -> OpenAIClient:
        return OpenAIClient(model, openai_api_key, task=task, meter=meter, cancel=cancel, **kwargs)

    def page_done(bar) -> None:
        bar.update(1)
        cancel.check()  # the model is unloaded when the analysis stops here

    workdir = Path(workdir).resolve()
    output_dir = workdir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Layout analysis (pages are rendered once and shared by every stage).
    #    Dolphin reads text only on pages without a text layer (scans).
    pages = render_pages(pdf_path, settings.page_resolution)
    cancel.check()
    if len(pages) > settings.max_pages:
        raise PdfError(f"ページ数が多すぎます（{len(pages)} ページ、上限 {settings.max_pages} ページ）。")
    with TextLayer(pdf_path, settings.page_resolution) as layer:
        with progress(len(pages), "ページ内要素 解析中") as bar:
            recognition = parse_pages(
                pages,
                workdir / "dolphin",
                settings=settings,
                recognize_labels=TEXT_LABELS,
                ocr_pages=[i for i in range(len(pages)) if not layer.usable(i)],
                on_page_done=lambda: page_done(bar),
            )

        def text_of(element: dict) -> str:
            return layer.text(element["page"], element["bbox"]) or element["text"]

        if promoted := promote_header_headings(recognition, layer):
            logger.info("Page headers used as headings: %s", promoted if settings.log_paper_text else len(promoted))
        segments = build_segments(recognition, page_height=max(page.height for page in pages), text_of=text_of)
        attach_printed_text(segments, layer)
    crop_segments(segments, pages, workdir / "crops")
    del pages
    title = next((s.source_text for s in segments if s.kind == "title" and s.source_text), "")

    # 2. Glossary and translation
    cancel.check()
    translator = client("translation", settings.translation_model, max_concurrency=settings.openai_max_concurrency)
    glossary, glossary_list_terms = [], 0
    term_list = load_term_list(settings.term_list) if settings.glossary else None
    if settings.glossary:
        glossary_maker = client("glossary", settings.glossary_model)
        with progress(1, "訳語表 作成中") as bar:
            glossary = build_glossary(segments, glossary_maker, settings, title, term_list)
            bar.update(1)
        if glossary:
            records = glossary_records(glossary, term_list)
            (workdir / "glossary.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
            glossary_list_terms = term_list.check(glossary, log=settings.log_paper_text) if term_list else 0
    parts, failed = translate_segments(segments, translator, settings, progress, glossary)
    translations = dict(parts)

    # 3. yomiyasu polishing of each block
    polisher: OpenAIClient | None = None
    polish_stats, polish_error = PolishStats(), None
    polished_blocks = polished_document = False
    if settings.polish_segments or settings.polish_document:
        polisher = client("polish", settings.polish_model, max_concurrency=settings.openai_max_concurrency)
    if polisher is not None and settings.polish_segments:
        try:
            parts, polish_stats = polish_segments(segments, parts, polisher, settings, workdir, progress, glossary)
            polished_blocks = True
        except Exception as e:
            polisher, parts, polish_error = None, translations, _polish_failure(e)

    # 4. Markdown with figures, yomiyasu polishing of the whole Markdown, prettier
    figure_indices = [i for i, s in enumerate(segments) if s.kind == "figure" and s.images]
    figure_paths = export_figures([segments[i].images[0] for i in figure_indices], output_dir / "figures")
    local_figures = {i: img_tag(f"figures/{p.name}", p.name) for i, p in zip(figure_indices, figure_paths, strict=True)}
    markdown = _assemble(len(segments), parts, local_figures)
    if polisher is not None and settings.polish_document:
        try:
            markdown, polish_stats = polish_document(
                markdown,
                polisher,
                settings,
                workdir,
                first_heading(markdown) or title,
                progress,
                polish_stats,
                glossary,
                after_blocks=polished_blocks,
            )
            polished_document = True
        except Exception as e:
            polish_error = _polish_failure(e)
    markdown = format_markdown(normalize_text(markdown))
    markdown_path = output_dir / "paper.md"
    markdown_path.write_text(markdown, encoding="utf-8")

    gyazo_path, gyazo_failures = None, 0
    cancel.check()
    if gyazo_token and figure_paths:
        gyazo_path, gyazo_failures = write_gyazo_markdown(
            markdown_path, figure_paths, gyazo_token, output_dir / "paper.gyazo.md"
        )

    with (workdir / "segments.jsonl").open("w", encoding="utf-8") as f:
        for index, segment in enumerate(segments):
            record = {**segment.to_json(), "translation": translations.get(index), "markdown": parts.get(index)}
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    polished = polished_blocks or polished_document
    if settings.polish_segments or settings.polish_document:
        logger.info(
            "Glossary: %d terms (%d as in the term list); polishing: %s",
            len(glossary),
            glossary_list_terms,
            polish_stats.to_json(),
        )

    # 5. Summary
    summarizer = client("summary", settings.summary_model)
    summary = normalize_text(summarize(markdown, summarizer, settings))
    summary_path = output_dir / "summary.md"
    summary_path.write_text(summary, encoding="utf-8")

    return PaperResult(
        summary=summary,
        markdown_path=markdown_path,
        summary_path=summary_path,
        figure_paths=figure_paths,
        gyazo_markdown_path=gyazo_path,
        title=first_heading(markdown),
        failed_segments=failed,
        gyazo_failures=gyazo_failures,
        translation_model=settings.translation_model,
        translation_effort=settings.translation_effort,
        summary_model=settings.summary_model,
        summary_effort=settings.summary_effort,
        polish_model=settings.polish_model if polished else None,
        polish_effort=settings.polish_effort if polished else None,
        polished_blocks=polished_blocks,
        polished_document=polished_document,
        polish_stats=polish_stats.to_json() if polished else None,
        polish_error=polish_error,
        glossary_terms=len(glossary),
        glossary_model=settings.glossary_model if glossary else None,
        glossary_effort=settings.glossary_effort if glossary else None,
        glossary_list_terms=glossary_list_terms,
        term_list=term_list.label if term_list else None,
    )
