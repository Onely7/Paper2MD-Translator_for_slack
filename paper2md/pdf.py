"""PDF files: download, validation, hashing and rendering pages to images."""

from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path

import pymupdf
import requests
from PIL import Image

USER_AGENT = "Paper2MD-Translator/1.0"
HTTP_HEADERS = {"User-Agent": USER_AGENT}


class PdfError(ValueError):
    """The file is not a usable PDF (message is shown to users)."""


def safe_pdf_filename(name: str | None, default: str = "paper.pdf") -> str:
    """
    Strip directories and control characters and make sure the name ends in .pdf.

    >>> safe_pdf_filename("../../etc/x.pdf")
    'x.pdf'
    >>> safe_pdf_filename("2304.08485")
    '2304.08485.pdf'
    >>> safe_pdf_filename("")
    'paper.pdf'
    """
    name = Path((name or "").replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().lstrip(".")
    if not name:
        return default
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def ensure_pdf(path: str | Path) -> None:
    with open(path, "rb") as f:
        head = f.read(1024)
    if b"%PDF-" not in head:
        raise PdfError(
            "ダウンロードしたファイルが PDF ではありませんでした（ログインが必要なページなどの可能性があります）。"
        )


def fetch_pdf(
    url: str,
    path: str | Path,
    *,
    headers: dict[str, str] | None = None,
    timeout: tuple[float, float] = (15.0, 180.0),
    max_bytes: int | None = None,
) -> dict[str, str]:
    """Download a PDF to ``path`` and return the response headers (lower-case keys).

    The file is removed again when it is not a PDF or larger than ``max_bytes``.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(url, stream=True, timeout=timeout, headers=headers or HTTP_HEADERS) as response:
            response.raise_for_status()
            size = 0
            with path.open("wb") as f:
                for chunk in response.iter_content(chunk_size=1 << 16):
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise PdfError(f"PDF が大きすぎます（上限 {max_bytes // 2**20} MB）。")
                    f.write(chunk)
            response_headers = {k.lower(): v for k, v in response.headers.items()}
        ensure_pdf(path)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return response_headers


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def page_count(path: str | Path) -> int:
    try:
        with pymupdf.open(path, filetype="pdf") as doc:
            return len(doc)
    except (pymupdf.FileDataError, RuntimeError, ValueError) as e:
        raise PdfError("PDF を開けませんでした（壊れているか、暗号化されている可能性があります）。") from e


def check_limits(path: str | Path, max_pages: int, max_mb: int) -> int:
    """Raise PdfError when the PDF exceeds the configured limits; return the page count."""
    ensure_pdf(path)
    size_mb = Path(path).stat().st_size / 2**20
    if size_mb > max_mb:
        raise PdfError(f"PDF が大きすぎます（{size_mb:.0f} MB、上限 {max_mb} MB）。")
    pages = page_count(path)
    if pages == 0:
        raise PdfError("PDF にページがありません。")
    if pages > max_pages:
        raise PdfError(f"ページ数が多すぎます（{pages} ページ、上限 {max_pages} ページ）。")
    return pages


def render_pages(path: str | Path, target_size: int) -> list[Image.Image]:
    """Render every page so that its longest side is ``target_size`` pixels.

    Dolphin's bounding boxes and the crops sent to the translation model both
    refer to these images, so they are rendered once and shared.
    """
    images = []
    with pymupdf.open(path, filetype="pdf") as doc:
        for page in doc:
            scale = target_size / max(page.rect.width, page.rect.height)
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale))
            images.append(Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB"))
    if not images:
        raise PdfError("PDF にページがありません。")
    return images
