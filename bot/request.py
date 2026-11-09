"""
One translation request from Slack.

1. Find the paper in the message and its cache key (arXiv: latest version).
2. Answer from the cache when the paper was translated before (no queue).
3. Otherwise translate it (one job per paper at a time, a limited number of
   jobs at once), store the result in the cache and post it.

``--no-cache``: everything happens in a private temporary directory that is
deleted at the end; the cache is read but never written.
``--refresh``: the cache is not read; the new result replaces the cached one.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import requests
from openai import AuthenticationError, PermissionDeniedError
from slack_sdk import WebClient
from slack_sdk.web.async_client import AsyncWebClient

from paper2md.cache import CachedPaper, PaperCache
from paper2md.config import Settings
from paper2md.llm import InsufficientQuotaError, ModelNotAvailableError
from paper2md.options import parse_request_options
from paper2md.pdf import PdfError, check_limits, fetch_pdf, file_sha256
from paper2md.pipeline import translate_paper
from paper2md.progress import slack_progress
from paper2md.sources import (
    PaperRef,
    arxiv_key,
    download_arxiv_pdf,
    find_paper_ref,
    resolve_latest_arxiv_version,
    sha256_key,
)
from paper2md.workspace import create_private_workdir, ephemeral_workdir, purge_stale_workdirs, remove_tree

from . import messages
from .jobs import JobQueue, PaperLocks
from .keystore import KeyStore
from .slack_io import Deliverable, deliver, download_slack_file, post

logger = logging.getLogger(__name__)


def describe_error(error: BaseException) -> str:
    """Message for users."""
    if isinstance(error, AuthenticationError):
        return "OpenAI の API Key が無効です。Home タブから登録し直してください。"
    if isinstance(error, InsufficientQuotaError):
        return "OpenAI API の利用枠（クレジット）が足りません。OpenAI の Billing 設定を確認してください。"
    if isinstance(error, ModelNotAvailableError):
        return f"この API Key では指定のモデルを使えません。({error})"
    if isinstance(error, PermissionDeniedError):
        return "OpenAI API へのアクセスが拒否されました。モデルの利用権限や組織の設定を確認してください。"
    if isinstance(error, PdfError):
        return str(error)
    if isinstance(error, (aiohttp.ClientResponseError, requests.HTTPError, requests.ConnectionError)):
        return f"PDF のダウンロードに失敗しました: {error}"
    return f"{type(error).__name__}: {error}"


@dataclass
class _Paper:
    ref: PaperRef
    keys: list[str] = field(default_factory=list)  # most specific first
    pdf_path: Path | None = None
    sha256: str | None = None
    arxiv_version: int | None = None
    cached: CachedPaper | None = None

    @property
    def display_name(self) -> str:
        return self.ref.display_name(self.arxiv_version)

    @property
    def is_latest_arxiv(self) -> bool:
        """A versionless arXiv link, resolved to its latest version."""
        return self.ref.kind == "arxiv" and self.ref.arxiv_version is None and self.arxiv_version is not None


class RequestHandler:
    def __init__(self, keystore: KeyStore) -> None:
        self.keystore = keystore
        self.jobs = JobQueue(Settings.from_env().max_concurrent_jobs)
        self.locks = PaperLocks()

    async def handle(self, client: AsyncWebClient, event: dict, user_id: str) -> None:
        channel = event["channel"]
        thread_ts = event.get("thread_ts", event["ts"])
        text = event.get("text") or ""
        options = parse_request_options(text)

        async def say(message: str) -> None:
            await post(client, channel, thread_ts, message)

        ref = find_paper_ref(event, text)
        if ref is None:
            await say(messages.NO_PAPER)
            return

        settings = Settings.from_env()
        cache = PaperCache(settings.cache_dir)
        include_pdf = ref.kind != "slack_file"  # do not post the user's own file back
        workdir: Path | None = None
        committed = False
        try:
            if options.no_cache:
                await say(messages.NO_CACHE_NOTICE)
                await asyncio.to_thread(purge_stale_workdirs)
                workdir = await asyncio.to_thread(create_private_workdir)
            else:
                await asyncio.to_thread(cache.purge_stale)
                workdir = await asyncio.to_thread(cache.new_staging_dir)
            if options.refresh:
                await say(messages.REFRESH_NOTICE)

            # 1. identify the paper; answer from the cache without queueing
            paper = await self._identify(client, ref, cache, workdir, settings, use_cache=not options.refresh)
            if paper.cached is not None:
                await self._deliver_cached(client, channel, thread_ts, paper, paper.cached, include_pdf, workdir)
                return
            await asyncio.to_thread(check_limits, paper.pdf_path, settings.max_pages, settings.max_pdf_mb)

            openai_key = await self.keystore.get(user_id, "openai")
            if not openai_key:
                await say(messages.NO_OPENAI_KEY)
                return
            gyazo_key = await self.keystore.get(user_id, "gyazo")
            if not options.no_cache:
                logger.info("Translating %s (%s)", paper.display_name, paper.keys[0])
            if paper.is_latest_arxiv:
                older = [
                    v
                    for v in await asyncio.to_thread(cache.cached_arxiv_versions, ref.arxiv_id)
                    if v < paper.arxiv_version
                ]
                if older:
                    await say(
                        messages.new_version(ref.arxiv_id, ", ".join(f"v{v}" for v in older), paper.arxiv_version)
                    )

            # 2. translate (one job per paper; a later request for it waits and uses the cache)
            entry: CachedPaper | None = None
            lock = (
                nullcontext()
                if options.no_cache
                else self.locks.hold(paper.keys[0], lambda: say(messages.SAME_PAPER_WAIT))
            )
            async with lock:
                if not options.refresh and not options.no_cache:
                    cached = await asyncio.to_thread(cache.find, paper.keys)
                    if cached is not None:
                        await self._deliver_cached(client, channel, thread_ts, paper, cached, include_pdf, workdir)
                        return
                async with self.jobs.slot(client, channel, thread_ts, f"{channel}:{event['ts']}"):
                    result = await asyncio.to_thread(
                        translate_paper,
                        paper.pdf_path,
                        workdir,
                        openai_api_key=openai_key,
                        gyazo_token=gyazo_key,
                        settings=settings,
                        progress=slack_progress(WebClient(token=client.token), channel, thread_ts),
                    )
                # a result with failed segments is posted but not cached
                if not options.no_cache and result.failed_segments == 0:
                    try:
                        entry = await asyncio.to_thread(
                            cache.commit,
                            workdir,
                            key=paper.keys[0],
                            aliases=paper.keys[1:],
                            result=result,
                            pdf_path=paper.pdf_path,
                            info=self._manifest_info(paper),
                        )
                        committed = True
                    except Exception:
                        logger.exception("Failed to store the result in the cache")

            # 3. post
            if entry is not None:
                with ephemeral_workdir() as scratch:
                    await deliver(client, channel, thread_ts, Deliverable.from_entry(entry, include_pdf), scratch)
            else:
                deliverable = Deliverable.from_result(result, paper.pdf_path, paper.display_name, include_pdf)
                await deliver(client, channel, thread_ts, deliverable, workdir)
            if result.failed_segments:
                await say(messages.failed_segments(result.failed_segments, options.no_cache))
            if result.gyazo_failures:
                await say(messages.gyazo_failures(result.gyazo_failures))
        except Exception as e:
            logger.exception("Processing error")
            await say(messages.error(describe_error(e)))
        finally:
            if workdir is not None and not committed:
                if options.no_cache:
                    removed = await asyncio.to_thread(remove_tree, workdir)
                    await say(messages.NO_CACHE_DONE if removed else messages.NO_CACHE_FAILED)
                else:
                    await asyncio.to_thread(cache.discard, workdir)

    async def _identify(
        self,
        client: AsyncWebClient,
        ref: PaperRef,
        cache: PaperCache,
        workdir: Path,
        settings: Settings,
        use_cache: bool,
    ) -> _Paper:
        """Work out the cache keys; download the PDF into ``workdir`` unless the cache answers first."""
        paper = _Paper(ref=ref)
        pdf_path = workdir / "paper.pdf"
        max_bytes = settings.max_pdf_mb * 2**20

        if ref.kind == "arxiv":
            # no version in the link: use the latest one (v2 -> v3 updates are picked up)
            version = ref.arxiv_version or await asyncio.to_thread(resolve_latest_arxiv_version, ref.arxiv_id)
            paper.arxiv_version = version
            if version and use_cache:
                paper.cached = await asyncio.to_thread(cache.get, arxiv_key(ref.arxiv_id, version))
                if paper.cached is not None:
                    return paper
            served = await asyncio.to_thread(download_arxiv_pdf, ref.arxiv_id, version, pdf_path, max_bytes)
            paper.arxiv_version = served or version
            if paper.arxiv_version:
                paper.keys.append(arxiv_key(ref.arxiv_id, paper.arxiv_version))
        elif ref.kind == "slack_file":
            await download_slack_file(ref.pdf_url, pdf_path, client.token, max_bytes)
        else:
            await asyncio.to_thread(fetch_pdf, ref.pdf_url, pdf_path, max_bytes=max_bytes)

        paper.pdf_path = pdf_path
        paper.sha256 = await asyncio.to_thread(file_sha256, pdf_path)
        paper.keys.append(sha256_key(paper.sha256))
        if use_cache:
            paper.cached = await asyncio.to_thread(cache.find, paper.keys)
        return paper

    @staticmethod
    async def _deliver_cached(
        client, channel, thread_ts, paper: _Paper, entry: CachedPaper, include_pdf: bool, scratch: Path
    ) -> None:
        if paper.is_latest_arxiv:
            subject = f"arXiv:{paper.ref.arxiv_id} の最新版（v{paper.arxiv_version}）は"
        elif paper.ref.kind == "arxiv":
            subject = f"arXiv:{entry.display_name} は"
        else:
            subject = "この PDF は"
        await post(client, channel, thread_ts, messages.cache_hit(subject, entry.created_date, entry.translation_model))
        await deliver(client, channel, thread_ts, Deliverable.from_entry(entry, include_pdf), scratch)

    @staticmethod
    def _manifest_info(paper: _Paper) -> dict:
        ref = paper.ref
        return {
            "display_name": paper.display_name,
            "source": {
                "kind": ref.kind,
                "url": None if ref.kind == "slack_file" else ref.url,
                "arxiv_id": ref.arxiv_id,
                "arxiv_version": paper.arxiv_version,
                "filename": ref.filename,
            },
            "sha256": paper.sha256,
            "document_parser": "Dolphin-v2",
        }
