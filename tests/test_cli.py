"""`python -m paper2md cache ...` commands."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paper2md.cache import CACHE_FORMAT_VERSION, PaperCache
from paper2md.cli import main


def add_entry(cache: PaperCache, key: str, created_at: str) -> None:
    staging = cache.new_staging_dir()
    (staging / "paper.pdf").write_bytes(b"%PDF-1.7")
    (staging / "paper.md").write_text("# T")
    (staging / "summary.md").write_text("s")
    manifest = {
        "format_version": CACHE_FORMAT_VERSION,
        "key": key,
        "aliases": [],
        "created_at": created_at,
        "title": "T",
        "translation_model": "gpt-6.1-terra",
        "translation_effort": "none",
        "files": {"pdf": "paper.pdf", "markdown": "paper.md", "summary": "summary.md", "figures": []},
    }
    (staging / "manifest.json").write_text(json.dumps(manifest))
    os.rename(staging, cache.root / key.replace(":", "-"))


class CacheCommandTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "cache"
        patcher = mock.patch.dict(os.environ, {"PAPER_CACHE_DIR": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cache = PaperCache()
        add_entry(self.cache, "arxiv:1111.11111v1", "2020-01-01T00:00:00+09:00")
        add_entry(self.cache, "arxiv:2222.22222v1", "2099-01-01T00:00:00+09:00")

    def run_cli(self, *args) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(list(args))
        return out.getvalue()

    def test_list_show_remove_prune(self):
        listing = self.run_cli("cache", "list")
        self.assertIn("arxiv:1111.11111v1\t2020-01-01\tgpt-6.1-terra/none\tno polish\tT", listing)
        self.assertIn('"key": "arxiv:2222.22222v1"', self.run_cli("cache", "show", "arxiv:2222.22222v1"))
        self.assertIn("removed arxiv:1111.11111v1", self.run_cli("cache", "prune", "--older-than", "30"))
        self.assertEqual([e.key for e in self.cache.entries()], ["arxiv:2222.22222v1"])
        self.assertIn("removed", self.run_cli("cache", "remove", "arxiv:2222.22222v1"))
        self.assertIn("not found", self.run_cli("cache", "remove", "arxiv:2222.22222v1"))
        self.assertEqual(list(self.cache.entries()), [])


if __name__ == "__main__":
    unittest.main()
