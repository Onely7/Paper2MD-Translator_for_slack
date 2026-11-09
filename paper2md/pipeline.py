"""
PDF -> Japanese Markdown + summary.

    workdir/
        dolphin/recognition.json   Dolphin-v2 layout analysis
        crops/                     page regions sent to the translation model
        segments.jsonl             segments and their translations (for debugging)
        output/
            paper.md               Markdown; figures linked as figures/figNN.png
            paper.gyazo.md         the same with Gyazo image URLs (with a Gyazo token)
            figures/figNN.png
            summary.md
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .document import parse_pages
from .figures import export_figures, img_tag, upload_all_to_gyazo
from .layout import TEXT_LABELS, build_segments, crop_segments
from .llm import OpenAIClient
from .markdown import finalize_markdown, first_heading
from .pdf import PdfError, render_pages
from .progress import ProgressFactory, console_progress
from .translation import summarize, translate_segments

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
    summary_model: str = ""


def _assemble(count: int, parts: dict[int, str], figures: dict[int, str]) -> str:
    """Markdown of all segments in order (figures as <img> tags)."""
    return finalize_markdown("\n\n".join(filter(None, (figures.get(i) or parts.get(i, "") for i in range(count)))))


def translate_paper(
    pdf_path: str | Path,
    workdir: str | Path,
    *,
    openai_api_key: str | None,
    gyazo_token: str | None = None,
    settings: Settings | None = None,
    progress: ProgressFactory = console_progress,
) -> PaperResult:
    """Run the whole pipeline; every file is written under ``workdir``."""
    settings = settings or Settings.from_env()
    if not openai_api_key:
        raise ValueError("OpenAI API key is not set.")
    workdir = Path(workdir).resolve()
    output_dir = workdir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Layout analysis (pages are rendered once and shared by every stage)
    pages = render_pages(pdf_path, settings.page_resolution)
    if len(pages) > settings.max_pages:
        raise PdfError(f"ページ数が多すぎます（{len(pages)} ページ、上限 {settings.max_pages} ページ）。")
    with progress(len(pages), "ページ内要素 解析中") as bar:
        recognition = parse_pages(
            pages,
            workdir / "dolphin",
            settings=settings,
            recognize_labels=TEXT_LABELS,
            on_page_done=lambda: bar.update(1),
        )
    segments = build_segments(recognition, page_height=max(page.height for page in pages))
    crop_segments(segments, pages, workdir / "crops")
    del pages

    # 2. Translation
    translator = OpenAIClient(
        settings.translation_model, openai_api_key, max_concurrency=settings.openai_max_concurrency
    )
    parts, failed = translate_segments(segments, translator, settings, progress)

    # 3. Markdown with figures
    figure_indices = [i for i, s in enumerate(segments) if s.kind == "figure" and s.images]
    figure_paths = export_figures([segments[i].images[0] for i in figure_indices], output_dir / "figures")
    local_figures = {i: img_tag(f"figures/{p.name}", p.name) for i, p in zip(figure_indices, figure_paths, strict=True)}
    markdown = _assemble(len(segments), parts, local_figures)
    markdown_path = output_dir / "paper.md"
    markdown_path.write_text(markdown, encoding="utf-8")

    gyazo_path, gyazo_failures = None, 0
    if gyazo_token and figure_paths:
        urls = upload_all_to_gyazo(figure_paths, gyazo_token)
        gyazo_failures = sum(url is None for url in urls)
        gyazo_figures = {
            i: img_tag(url, p.name) if url else local_figures[i]
            for i, p, url in zip(figure_indices, figure_paths, urls, strict=True)
        }
        gyazo_path = output_dir / "paper.gyazo.md"
        gyazo_path.write_text(_assemble(len(segments), parts, gyazo_figures), encoding="utf-8")

    with (workdir / "segments.jsonl").open("w", encoding="utf-8") as f:
        for index, segment in enumerate(segments):
            f.write(json.dumps({**segment.to_json(), "markdown": parts.get(index)}, ensure_ascii=False) + "\n")

    # 4. Summary
    summarizer = OpenAIClient(settings.summary_model, openai_api_key)
    summary = summarize(markdown, summarizer, settings)
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
        summary_model=settings.summary_model,
    )
