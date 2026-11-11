"""Run the Slack request flow without Slack, arXiv, Dolphin or the OpenAI API."""

import asyncio
import functools
import os
import tempfile
import time
import zipfile
from pathlib import Path
from unittest import mock

import pymupdf

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")

from bot import request as request_module  # noqa: E402
from bot.request import RequestHandler  # noqa: E402
from paper2md.pipeline import PaperResult  # noqa: E402

KEYS = {"openai": "sk-test", "gyazo": None}


@functools.cache
def pdf_bytes(label: str) -> bytes:
    """A real one-page PDF whose bytes depend on ``label`` (the same bytes for the same label)."""
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), label)
    return doc.tobytes(no_new_id=True)


class FakeKeyStore:
    def __init__(self, keys, preferences=None):
        self.keys = dict(keys)
        self.saved_preferences = dict(preferences or {})

    async def get(self, user_id, provider):
        return self.keys.get(provider)

    async def preferences(self, user_id):
        return dict(self.saved_preferences)


class BotHarness:
    """Patches the I/O of bot.request; records what would be posted to Slack."""

    def __init__(self, testcase):
        root = Path(tempfile.mkdtemp(prefix="p2m-bot-test-"))
        self.cache_dir = root / "cache"
        self.shm = root / "shm"
        self.shm.mkdir()
        self.posted: list[str] = []
        self.uploads: list[str] = []  # file names posted to the thread
        self.zips: list[list[str]] = []  # members of each posted ZIP
        self.pipeline_calls = 0
        self.pipeline_delay = 0.0
        self.pipeline_error: Exception | None = None
        self.failed_segments = 0
        self.polish_error = None
        self.pipeline_settings = []  # the Settings each pipeline run got
        self.gyazo_tokens = []  # the Gyazo token each pipeline run got
        self.gyazo_uploads = []  # tokens of figures uploaded for a cached paper
        self.markdown_texts: list[str] = []  # the posted .md files
        self.stop_in_pipeline = False  # the requester presses 中断する while the pipeline runs
        self.tokens = []  # the CancelToken each pipeline run got
        self.blocks: list[list] = []  # the blocks of each posted message ([] without blocks)
        self.edits: list[dict] = []  # chat_update calls
        self.ephemeral: list[dict] = []  # chat_postEphemeral calls
        self.arxiv_latest = {"2408.03326": 3}
        self.resolve_fails = False
        self.slack_files: dict[str, bytes] = {}
        self.urls: dict[str, bytes] = {}

        self.client = mock.MagicMock()
        self.client.token = "xoxb-test"
        self.client.chat_postMessage = mock.AsyncMock(side_effect=self._post)
        self.client.chat_update = mock.AsyncMock(side_effect=self._update)
        self.client.chat_postEphemeral = mock.AsyncMock(side_effect=self._ephemeral)
        self.client.files_upload_v2 = mock.AsyncMock(side_effect=self._upload)
        patches = [
            mock.patch.object(request_module, "translate_paper", side_effect=self._pipeline),
            mock.patch.object(request_module, "resolve_latest_arxiv_version", side_effect=self._resolve),
            mock.patch.object(request_module, "download_arxiv_pdf", side_effect=self._download_arxiv),
            mock.patch.object(request_module, "fetch_pdf", side_effect=self._fetch),
            mock.patch.object(request_module, "download_slack_file", side_effect=self._download_slack),
            mock.patch.object(request_module, "write_gyazo_markdown", side_effect=self._gyazo_markdown),
            mock.patch.object(request_module, "usd_to_jpy", return_value=(150.0, "2026-11-10")),
            mock.patch.dict(os.environ, {"PAPER_CACHE_DIR": str(self.cache_dir), "NO_CACHE_WORKDIR": str(self.shm)}),
        ]
        for patcher in patches:
            patcher.start()
            testcase.addCleanup(patcher.stop)

    # ---- fakes ---------------------------------------------------------
    async def _post(self, **kwargs):
        self.posted.append(kwargs["text"])
        self.blocks.append(kwargs.get("blocks") or [])
        return {"ts": f"{len(self.posted)}.0"}

    async def _update(self, **kwargs):
        self.edits.append(kwargs)
        return {"ok": True}

    async def _ephemeral(self, **kwargs):
        self.ephemeral.append(kwargs)
        return {"ok": True}

    async def _upload(self, **kwargs):
        for item in kwargs["file_uploads"]:
            path = Path(item["file"])
            assert path.is_file(), path  # must still exist while uploading
            self.uploads.append(item["filename"])
            if path.suffix == ".md":
                self.markdown_texts.append(path.read_text())
            if path.suffix == ".zip":
                with zipfile.ZipFile(path) as bundle:
                    self.zips.append(sorted(bundle.namelist()))
        return {"ok": True}

    def _resolve(self, arxiv_id):
        return None if self.resolve_fails else self.arxiv_latest.get(arxiv_id)

    def _download_arxiv(self, arxiv_id, version, path, max_bytes=None):
        version = version or self.arxiv_latest[arxiv_id]
        Path(path).write_bytes(pdf_bytes(f"{arxiv_id}v{version}"))
        return version

    def _fetch(self, url, path, max_bytes=None, **kwargs):
        Path(path).write_bytes(self.urls[url])
        return {}

    async def _download_slack(self, url, dest, token, max_bytes):
        Path(dest).write_bytes(self.slack_files[url])

    def _gyazo_markdown(self, markdown_path, figure_paths, token, out_path):
        self.gyazo_uploads.append(token)
        out_path.write_text("# Paper Title (uploaded for a cached paper)\n")
        return out_path, 0

    def _pipeline(self, pdf_path, workdir, *, openai_api_key, gyazo_token, settings, progress, cancel, meter):
        self.pipeline_calls += 1
        self.pipeline_settings.append(settings)
        self.gyazo_tokens.append(gyazo_token)
        self.tokens.append(cancel)
        meter.add("translation", settings.translation_model, {"input_tokens": 100_000, "output_tokens": 10_000})
        if self.stop_in_pipeline:
            cancel.cancel()  # as the button handler does
            cancel.check()
        if self.pipeline_delay:
            time.sleep(self.pipeline_delay)
        out = Path(workdir) / "output"
        (out / "figures").mkdir(parents=True, exist_ok=True)
        (Path(workdir) / "dolphin").mkdir(exist_ok=True)
        (Path(workdir) / "dolphin" / "recognition.json").write_text("{}")
        figure = out / "figures" / "fig01.png"
        figure.write_bytes(b"png")
        markdown = out / "paper.md"
        markdown.write_text(f"# Paper Title\n\n翻訳 {self.pipeline_calls}\n")
        gyazo = None
        if gyazo_token:
            gyazo = out / "paper.gyazo.md"
            gyazo.write_text("# Paper Title (gyazo)\n")
        summary = out / "summary.md"
        summary.write_text(f"【背景】run {self.pipeline_calls}")
        if self.pipeline_error is not None:
            raise self.pipeline_error
        return PaperResult(
            summary=summary.read_text(),
            markdown_path=markdown,
            summary_path=summary,
            figure_paths=[figure],
            gyazo_markdown_path=gyazo,
            title="Paper Title",
            failed_segments=self.failed_segments,
            translation_model=settings.translation_model,
            translation_effort=settings.translation_effort,
            summary_model=settings.summary_model,
            summary_effort=settings.summary_effort,
            polish_model=settings.polish_model if settings.polish_segments or settings.polish_document else None,
            polish_effort=settings.polish_effort if settings.polish_segments or settings.polish_document else None,
            polished_blocks=settings.polish_segments,
            polished_document=settings.polish_document,
            polish_error=self.polish_error,
            glossary_model=settings.glossary_model if settings.glossary else None,
            glossary_effort=settings.glossary_effort if settings.glossary else None,
        )

    # ---- helpers -------------------------------------------------------
    def event(self, text, files=None, ts="100.0"):
        event = {"channel": "C1", "ts": ts, "text": text}
        if files:
            event["files"] = files
        return event

    def run(self, text, files=None, keys=KEYS, ts="100.0", preferences=None):
        handler = RequestHandler(FakeKeyStore(keys, preferences))
        asyncio.run(handler.handle(self.client, self.event(text, files, ts), "U1"))

    def entries(self):
        if not self.cache_dir.is_dir():
            return []
        return sorted(p.name for p in self.cache_dir.iterdir() if not p.name.startswith("_"))

    def staging_leftovers(self):
        staging = self.cache_dir / "_staging"
        return list(staging.iterdir()) if staging.is_dir() else []

    def slack_file(self, url, name, data):
        self.slack_files[url] = data
        return [{"mimetype": "application/pdf", "url_private_download": url, "name": name}]
