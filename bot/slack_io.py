"""Posting to and downloading from Slack."""

from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from paper2md.cache import CachedPaper
from paper2md.markdown import summary_for_slack
from paper2md.pdf import PdfError, ensure_pdf
from paper2md.pipeline import PaperResult

from . import messages

logger = logging.getLogger(__name__)

FILES_PER_UPLOAD = 10  # files_upload_v2 limit per call


async def post(
    client: AsyncWebClient, channel: str, thread_ts: str, text: str, blocks: list[dict] | None = None
) -> str | None:
    """Post to the thread; ``blocks`` (e.g. with a button) are shown instead of ``text`` where supported."""
    try:
        extra = {"blocks": blocks} if blocks else {}
        response = await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text, **extra)
        return response["ts"]
    except SlackApiError as e:
        logger.error("chat_postMessage failed: %s", e.response.get("error"))
        return None


async def edit(client: AsyncWebClient, channel: str, ts: str | None, text: str) -> None:
    """Replace a message with plain ``text`` (its blocks, such as a button, are removed)."""
    if not ts:
        return
    try:
        await client.chat_update(channel=channel, ts=ts, text=text, blocks=[])
    except SlackApiError as e:
        logger.warning("chat_update failed: %s", e.response.get("error"))


async def download_slack_file(url: str, dest: Path, token: str, max_bytes: int) -> None:
    """Download a file shared in Slack (needs the bot token)."""
    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {token}"}, timeout=timeout) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            size = 0
            with dest.open("wb") as f:
                async for chunk in response.content.iter_chunked(1 << 16):
                    size += len(chunk)
                    if size > max_bytes:
                        dest.unlink(missing_ok=True)
                        raise PdfError(f"PDF が大きすぎます（上限 {max_bytes // 2**20} MB）。")
                    f.write(chunk)
    try:
        ensure_pdf(dest)
    except PdfError:
        dest.unlink(missing_ok=True)
        raise


@dataclass
class Deliverable:
    """What is posted for a translated paper."""

    summary: str
    markdown_path: Path  # figures linked as figures/figNN.png
    figure_paths: list[Path]
    display_name: str
    gyazo_markdown_path: Path | None = None
    pdf_path: Path | None = None  # None: do not post the PDF (e.g. the user attached it)

    @classmethod
    def from_entry(cls, entry: CachedPaper, include_pdf: bool) -> Deliverable:
        return cls(
            summary=entry.summary,
            markdown_path=entry.markdown_path,
            figure_paths=entry.figure_paths,
            display_name=entry.display_name,
            gyazo_markdown_path=entry.gyazo_markdown_path,
            pdf_path=entry.pdf_path if include_pdf else None,
        )

    @classmethod
    def from_result(cls, result: PaperResult, pdf_path: Path, display_name: str, include_pdf: bool) -> Deliverable:
        return cls(
            summary=result.summary,
            markdown_path=result.markdown_path,
            figure_paths=result.figure_paths,
            display_name=display_name,
            gyazo_markdown_path=result.gyazo_markdown_path,
            pdf_path=pdf_path if include_pdf else None,
        )


def make_bundle(deliverable: Deliverable, dest_dir: Path) -> Path:
    """ZIP with ``<name>.md`` and ``figures/`` (opens with images in any Markdown viewer)."""
    path = dest_dir / f"{deliverable.display_name}.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.write(deliverable.markdown_path, f"{deliverable.display_name}/{deliverable.display_name}.md")
        for figure in deliverable.figure_paths:
            bundle.write(figure, f"{deliverable.display_name}/figures/{figure.name}")
    return path


async def _upload(
    client: AsyncWebClient, channel: str, thread_ts: str, files: list[tuple[Path, str]], comment: str
) -> None:
    for start in range(0, len(files), FILES_PER_UPLOAD):
        batch = files[start : start + FILES_PER_UPLOAD]
        try:
            await client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file_uploads=[{"file": str(path), "filename": name, "title": name} for path, name in batch],
                initial_comment=comment if start == 0 else None,
            )
        except SlackApiError as e:
            logger.error("Slack upload failed: %s", e.response.get("error"))


async def deliver(
    client: AsyncWebClient, channel: str, thread_ts: str, deliverable: Deliverable, scratch_dir: Path
) -> None:
    """Post the summary, the PDF/Markdown/ZIP and the figure images to the thread."""
    name = deliverable.display_name
    await post(client, channel, thread_ts, messages.SUMMARY_HEADER + summary_for_slack(deliverable.summary))

    files: list[tuple[Path, str]] = []
    if deliverable.pdf_path is not None:
        files.append((deliverable.pdf_path, f"{name}.pdf"))
    files.append((deliverable.gyazo_markdown_path or deliverable.markdown_path, f"{name}.md"))
    files.append((make_bundle(deliverable, scratch_dir), f"{name}.zip"))
    await _upload(client, channel, thread_ts, files, messages.RESULT_FILES)

    if deliverable.figure_paths:
        await _upload(client, channel, thread_ts, [(p, p.name) for p in deliverable.figure_paths], messages.FIGURES)
