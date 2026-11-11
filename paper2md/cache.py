"""
Cache of finished translations, one directory per paper.

    $PAPER_CACHE_DIR/                   (default: data/cache)
        arxiv-2408.03326v3/             key "arxiv:2408.03326v3"
        sha256-<64 hex>/                key "sha256:<hex>" (other PDFs)
            manifest.json               written last: entries without it are ignored
            paper.pdf
            output/paper.md, output/paper.gyazo.md, output/summary.md, output/figures/
            dolphin/, crops/, segments.jsonl (intermediate files)
        _index/sha256-<hex>             alias: PDF hash -> entry directory name
        _staging/                       jobs in progress, renamed into place on success
        _trash/                         replaced entries, deleted right after

A job writes everything into a staging directory; ``commit`` renames it to the
entry directory in one step, so readers never see a half-written entry.  The
cache root is created with mode 0700.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .config import Settings
from .workspace import process_alive, remove_tree

if TYPE_CHECKING:  # avoid importing torch for cache-only use (e.g. the CLI)
    from .pipeline import PaperResult

logger = logging.getLogger(__name__)

# 3: models with their reasoning efforts; references and headings as printed
# 4: yomiyasu polishing (polish_model / polish_effort / polish_segments / polish_document / polish), glossary
#    (with the term list: glossary_list_terms, term_list),
#    PDF text sent with the images, headings set as page headers, underscores of names in math, prettier
CACHE_FORMAT_VERSION = 4
_ENTRY_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def entry_name(key: str) -> str:
    """
    >>> entry_name("arxiv:2408.03326v3")
    'arxiv-2408.03326v3'
    >>> entry_name("arxiv:hep-th/9901001v1")
    'arxiv-hep-th_9901001v1'
    """
    kind, _, value = key.partition(":")
    return f"{kind}-{re.sub(r'[^A-Za-z0-9._-]', '_', value)}"


@dataclass(frozen=True)
class CachedPaper:
    path: Path
    manifest: dict

    def _file(self, name: str) -> Path | None:
        relative = self.manifest["files"].get(name)
        return self.path / relative if relative else None

    @property
    def key(self) -> str:
        return self.manifest["key"]

    @property
    def keys(self) -> list[str]:
        return [self.manifest["key"], *self.manifest.get("aliases", [])]

    @property
    def pdf_path(self) -> Path:
        return self._file("pdf")

    @property
    def markdown_path(self) -> Path:
        return self._file("markdown")

    @property
    def gyazo_markdown_path(self) -> Path | None:
        return self._file("markdown_gyazo")

    @property
    def summary_path(self) -> Path:
        return self._file("summary")

    @property
    def figure_paths(self) -> list[Path]:
        return [self.path / p for p in self.manifest["files"].get("figures", [])]

    @property
    def summary(self) -> str:
        return self.summary_path.read_text(encoding="utf-8")

    @property
    def display_name(self) -> str:
        return self.manifest.get("display_name") or self.path.name

    @property
    def title(self) -> str | None:
        return self.manifest.get("title")

    @property
    def created_at(self) -> str:
        return self.manifest.get("created_at") or ""

    @property
    def created_date(self) -> str:
        return self.created_at[:10]

    @property
    def translation_model(self) -> str:
        return self.manifest.get("translation_model") or "?"

    @property
    def translation_effort(self) -> str | None:
        return self.manifest.get("translation_effort")

    @property
    def summary_model(self) -> str:
        return self.manifest.get("summary_model") or "?"

    @property
    def summary_effort(self) -> str | None:
        return self.manifest.get("summary_effort")

    @property
    def polish_model(self) -> str | None:
        """None: the result was not polished with yomiyasu."""
        return self.manifest.get("polish_model")

    @property
    def polish_effort(self) -> str | None:
        return self.manifest.get("polish_effort")

    @property
    def glossary_model(self) -> str:
        """ "": no glossary, or a result stored before the glossary model was recorded."""
        return self.manifest.get("glossary_model") or ""

    @property
    def glossary_effort(self) -> str | None:
        return self.manifest.get("glossary_effort")

    @property
    def polish_segments(self) -> bool:
        """The result was polished block by block."""
        return bool(self.manifest.get("polish_segments"))

    @property
    def polish_document(self) -> bool:
        """The result was polished as a whole."""
        return bool(self.manifest.get("polish_document"))


class PaperCache:
    def __init__(self, root: str | os.PathLike | None = None) -> None:
        self.root = Path(root) if root else Settings.from_env().cache_dir
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    def _load(self, path: Path) -> CachedPaper | None:
        try:
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if manifest.get("format_version") != CACHE_FORMAT_VERSION:
            return None
        try:
            entry = CachedPaper(path, manifest)
            required = [entry.pdf_path, entry.markdown_path, entry.summary_path, *entry.figure_paths]
            if entry.gyazo_markdown_path is not None:
                required.append(entry.gyazo_markdown_path)
        except (KeyError, TypeError):
            return None
        if not all(p is not None and p.is_file() for p in required):
            logger.warning("Ignoring incomplete cache entry %s", path.name)
            return None
        return entry

    def get(self, key: str) -> CachedPaper | None:
        entry = self._load(self.root / entry_name(key))
        if entry is not None:
            return entry
        try:  # alias, e.g. the PDF hash of an arXiv entry
            target = (self.root / "_index" / entry_name(key)).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not _ENTRY_NAME_RE.fullmatch(target):
            return None
        entry = self._load(self.root / target)
        # the alias is only trusted when the entry itself lists the key
        return entry if entry is not None and key in entry.keys else None

    def find(self, keys) -> CachedPaper | None:
        for key in keys:
            if key and (entry := self.get(key)) is not None:
                return entry
        return None

    def entries(self) -> Iterator[CachedPaper]:
        if not self.root.is_dir():
            return
        for path in sorted(self.root.iterdir()):
            if not path.name.startswith("_") and (entry := self._load(path)) is not None:
                yield entry

    def cached_arxiv_versions(self, arxiv_id: str) -> list[int]:
        prefix = entry_name(f"arxiv:{arxiv_id}v")
        versions = []
        for path in self.root.glob(f"{prefix}*"):
            match = re.fullmatch(re.escape(prefix) + r"(\d+)", path.name)
            if match and self._load(path) is not None:
                versions.append(int(match.group(1)))
        return sorted(versions)

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------
    def new_staging_dir(self) -> Path:
        if not self.root.exists():
            # other users of the server should not see which papers were translated
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging = self.root / "_staging"
        staging.mkdir(exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=f"{os.getpid()}-", dir=staging))

    def discard(self, staging_dir: str | os.PathLike) -> None:
        remove_tree(staging_dir)

    def commit(
        self,
        staging_dir: str | os.PathLike,
        *,
        key: str,
        aliases=(),
        result: PaperResult,
        pdf_path: str | os.PathLike,
        info: dict | None = None,
    ) -> CachedPaper:
        """Turn a finished staging directory into the entry for ``key`` (replacing an old one)."""
        staging_dir = Path(staging_dir).resolve()

        def relative(path) -> str:
            return str(Path(path).resolve().relative_to(staging_dir))

        files = {
            "pdf": relative(pdf_path),
            "markdown": relative(result.markdown_path),
            "summary": relative(result.summary_path),
            "figures": [relative(p) for p in result.figure_paths],
        }
        if result.gyazo_markdown_path is not None:
            files["markdown_gyazo"] = relative(result.gyazo_markdown_path)
        manifest = {
            "format_version": CACHE_FORMAT_VERSION,
            "key": key,
            "aliases": sorted({a for a in aliases if a and a != key}),
            "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "title": result.title,
            "translation_model": result.translation_model,
            "translation_effort": result.translation_effort,
            "summary_model": result.summary_model,
            "summary_effort": result.summary_effort,
            "polish_model": result.polish_model,
            "polish_effort": result.polish_effort,
            "polish_segments": result.polished_blocks,
            "polish_document": result.polished_document,
            "polish": result.polish_stats,
            "glossary_terms": result.glossary_terms,
            "glossary_model": result.glossary_model,
            "glossary_effort": result.glossary_effort,
            "glossary_list_terms": result.glossary_list_terms,
            "term_list": result.term_list,
            **(info or {}),
            "files": files,
        }
        _write_text_atomic(staging_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

        target = self.root / entry_name(key)
        trash = None
        with self._lock:
            if target.exists():
                trash = self.root / "_trash" / f"{target.name}-{uuid.uuid4().hex[:8]}"
                trash.parent.mkdir(parents=True, exist_ok=True)
                os.rename(target, trash)
            os.rename(staging_dir, target)
            for alias in manifest["aliases"]:
                _write_text_atomic(self.root / "_index" / entry_name(alias), target.name)
        if trash is not None:
            remove_tree(trash)

        entry = self._load(target)
        if entry is None:
            raise RuntimeError(f"Cache entry {target} could not be read back")
        logger.info("Cached %s", target.name)
        return entry

    def remove(self, key: str) -> bool:
        """Delete the entry of ``key`` (or of the entry an alias points to)."""
        entry = self.get(key)
        if entry is None:
            return False
        for alias in entry.keys:
            (self.root / "_index" / entry_name(alias)).unlink(missing_ok=True)
        return remove_tree(entry.path)

    def purge_stale(self, max_age_seconds: float = 24 * 3600) -> int:
        """Remove staging directories of dead processes and leftover trash."""
        removed = 0
        now = time.time()
        staging = self.root / "_staging"
        for path in staging.iterdir() if staging.is_dir() else ():
            try:
                age = now - path.lstat().st_mtime
            except FileNotFoundError:
                continue
            pid_text = path.name.split("-", 1)[0]
            pid = int(pid_text) if pid_text.isdigit() else None
            if pid == os.getpid():
                stale = age > max_age_seconds  # a job of this process may still use it
            else:
                stale = pid is None or not process_alive(pid) or age > max_age_seconds
            if stale and remove_tree(path):
                removed += 1
        trash = self.root / "_trash"
        for path in trash.iterdir() if trash.is_dir() else ():
            if remove_tree(path):
                removed += 1
        return removed
