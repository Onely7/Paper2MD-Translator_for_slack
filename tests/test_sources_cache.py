"""Paper identification (arXiv versions, other sites), request options, workspaces and the cache store."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from paper2md import sources, workspace
from paper2md.cache import PaperCache
from paper2md.options import parse_request_options
from paper2md.pipeline import PaperResult
from paper2md.sources import find_paper_ref, resolve_latest_arxiv_version


class FindPaperRefTests(unittest.TestCase):
    def test_arxiv_forms(self):
        cases = {
            "<https://arxiv.org/abs/2408.03326>": ("2408.03326", None),
            "<https://arxiv.org/abs/2408.03326v2>": ("2408.03326", 2),
            "<https://arxiv.org/pdf/2408.03326v3.pdf>": ("2408.03326", 3),
            "<https://www.arxiv.org/html/2408.03326v1>": ("2408.03326", 1),
            "<https://export.arxiv.org/abs/1706.03762|arxiv.org/abs/1706.03762>": ("1706.03762", None),
            "<https://arxiv.org/abs/hep-th/9901001v2>": ("hep-th/9901001", 2),
            "<https://arxiv.org/pdf/math.GT/0309136>": ("math.GT/0309136", None),
            "<https://huggingface.co/papers/2408.03326>": ("2408.03326", None),
            "https://www.alphaxiv.org/abs/2408.03326v2": ("2408.03326", 2),
        }
        for text, (arxiv_id, version) in cases.items():
            ref = find_paper_ref({}, f"<@UBOT> {text}")
            self.assertEqual((ref.kind, ref.arxiv_id, ref.arxiv_version), ("arxiv", arxiv_id, version), text)

    def test_other_sites(self):
        cases = {
            "<https://openreview.net/forum?id=AbC-12>": ("https://openreview.net/pdf?id=AbC-12", "openreview-AbC-12"),
            "<https://aclanthology.org/2024.acl-long.1/>": (
                "https://aclanthology.org/2024.acl-long.1.pdf",
                "2024.acl-long.1",
            ),
            "<https://example.com/x/paper.pdf?dl=1|paper.pdf>": ("https://example.com/x/paper.pdf?dl=1", "paper"),
            "`https://example.com/x/paper.pdf`": ("https://example.com/x/paper.pdf", "paper"),
        }
        for text, (pdf_url, name) in cases.items():
            ref = find_paper_ref({}, text)
            self.assertEqual((ref.kind, ref.pdf_url, ref.display_name()), ("url", pdf_url, name), text)
        self.assertIsNone(find_paper_ref({}, "<https://example.com/paper.pdf.html>"))
        self.assertIsNone(find_paper_ref({}, "<https://aclanthology.org/events/acl-2024/>"))

    def test_attachment_wins(self):
        files = [{"mimetype": "application/pdf", "url_private_download": "https://files.slack.com/f", "name": "x.pdf"}]
        ref = find_paper_ref({"files": files}, "<https://arxiv.org/abs/2408.03326>")
        self.assertEqual((ref.kind, ref.display_name()), ("slack_file", "x"))


class ResolveVersionTests(unittest.TestCase):
    def test_head_content_disposition(self):
        head = mock.Mock(ok=True, headers={"content-disposition": 'inline; filename="2408.03326v3.pdf"'})
        with (
            mock.patch.object(sources.requests, "head", return_value=head),
            mock.patch.object(sources.requests, "get") as get,
        ):
            self.assertEqual(resolve_latest_arxiv_version("2408.03326"), 3)
        get.assert_not_called()

    def test_api_fallback_and_failure(self):
        api = mock.Mock(text="<entry><id>http://arxiv.org/abs/2408.03326v4</id></entry>", raise_for_status=mock.Mock())
        with (
            mock.patch.object(sources.requests, "head", side_effect=requests.ConnectionError()),
            mock.patch.object(sources.requests, "get", return_value=api),
        ):
            self.assertEqual(resolve_latest_arxiv_version("2408.03326"), 4)
        with (
            mock.patch.object(sources.requests, "head", side_effect=requests.Timeout()),
            mock.patch.object(sources.requests, "get", side_effect=requests.Timeout()),
        ):
            self.assertIsNone(resolve_latest_arxiv_version("2408.03326"))


class RequestOptionTests(unittest.TestCase):
    def test_no_cache(self):
        for text in (
            "--no-cache",
            "-no-cache",
            "nocache",
            "no_cache",
            "NO-CACHE",
            "—no-cache",
            "－－ｎｏ－ｃａｃｈｅ",
            "`--no-cache`",
            "キャッシュなしで",
            "保存しない",
        ):
            self.assertTrue(parse_request_options(f"<@U> {text} <https://arxiv.org/abs/1>").no_cache, text)
        for text in (
            "<https://example.com/nocache/paper.pdf>",
            "<https://example.com/a.pdf?nocache=1|a.pdf>",
            "nocaches",
            "",
        ):
            self.assertFalse(parse_request_options(f"<@U> {text}").no_cache, text)

    def test_refresh(self):
        self.assertTrue(parse_request_options("<@U> --refresh").refresh)
        self.assertTrue(parse_request_options("<@U> 再翻訳して").refresh)
        self.assertFalse(parse_request_options("<@U> please refresh my memory").refresh)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ, {"NO_CACHE_WORKDIR": self.root})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_private_and_removed_on_error(self):
        with self.assertRaises(RuntimeError), workspace.ephemeral_workdir() as path:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            (path / "paper.pdf").write_bytes(b"%PDF-1.7")
            raise RuntimeError("boom")
        self.assertFalse(path.exists())

    def test_purge_only_dead_processes(self):
        mine = workspace.create_private_workdir()
        dead = Path(self.root, f"{workspace.NO_CACHE_PREFIX}999999999-abc")
        dead.mkdir()
        self.assertEqual(workspace.purge_stale_workdirs(), 1)
        self.assertFalse(dead.exists())
        self.assertTrue(mine.exists())


class PaperCacheTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "cache"
        self.cache = PaperCache(self.root)

    def commit(self, key="arxiv:2408.03326v3", aliases=("sha256:abc",), text="翻訳", gyazo=True):
        staging = self.cache.new_staging_dir()
        (staging / "paper.pdf").write_bytes(b"%PDF-1.7")
        out = staging / "output"
        (out / "figures").mkdir(parents=True)
        (out / "paper.md").write_text(text)
        (out / "summary.md").write_text("要約")
        (out / "figures" / "fig01.png").write_bytes(b"png")
        if gyazo:
            (out / "paper.gyazo.md").write_text(text + " (gyazo)")
        result = PaperResult(
            summary="要約",
            markdown_path=out / "paper.md",
            summary_path=out / "summary.md",
            figure_paths=[out / "figures" / "fig01.png"],
            gyazo_markdown_path=out / "paper.gyazo.md" if gyazo else None,
            title="Title",
            translation_model="gpt-6.1-terra",
        )
        return self.cache.commit(
            staging,
            key=key,
            aliases=aliases,
            result=result,
            pdf_path=staging / "paper.pdf",
            info={"display_name": "2408.03326v3"},
        )

    def test_commit_and_lookup(self):
        entry = self.commit()
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(entry.path.name, "arxiv-2408.03326v3")
        got = self.cache.get("arxiv:2408.03326v3")
        self.assertEqual((got.summary, got.title, got.translation_model), ("要約", "Title", "gpt-6.1-terra"))
        self.assertEqual(got.gyazo_markdown_path.read_text(), "翻訳 (gyazo)")
        self.assertEqual(self.cache.get("sha256:abc").path, entry.path)  # alias
        self.assertIsNone(self.cache.get("arxiv:2408.03326v2"))
        self.assertEqual(self.cache.cached_arxiv_versions("2408.03326"), [3])
        self.assertEqual([e.key for e in self.cache.entries()], ["arxiv:2408.03326v3"])
        self.assertEqual(list((self.root / "_staging").iterdir()), [])
        moved = Path(tempfile.mkdtemp()) / "moved"
        os.rename(self.root, moved)  # paths in the manifest are relative
        self.assertTrue(PaperCache(moved).get("arxiv:2408.03326v3").figure_paths[0].is_file())

    def test_replace_and_remove(self):
        self.commit(text="old")
        entry = self.commit(text="new", aliases=("sha256:def",), gyazo=False)
        self.assertEqual(entry.markdown_path.read_text(), "new")
        self.assertIsNone(entry.gyazo_markdown_path)
        self.assertIsNone(self.cache.get("sha256:abc"))  # alias of the replaced entry is not trusted
        self.assertEqual(list((self.root / "_trash").iterdir()), [])
        self.assertTrue(self.cache.remove("sha256:def"))
        self.assertIsNone(self.cache.get("arxiv:2408.03326v3"))
        self.assertFalse(self.cache.remove("arxiv:2408.03326v3"))

    def test_incomplete_or_foreign_entries_are_ignored(self):
        entry = self.commit()
        entry.figure_paths[0].unlink()
        self.assertIsNone(self.cache.get("arxiv:2408.03326v3"))
        (self.root / "_index").mkdir(exist_ok=True)
        (self.root / "_index" / "sha256-zzz").write_text("../../etc")
        self.assertIsNone(self.cache.get("sha256:zzz"))
        manifest = json.loads((entry.path / "manifest.json").read_text())
        manifest["format_version"] = 1
        (entry.path / "manifest.json").write_text(json.dumps(manifest))
        self.assertEqual(list(self.cache.entries()), [])

    def test_purge_stale(self):
        dead = self.root / "_staging" / "999999999-abc"
        dead.mkdir(parents=True)
        mine = self.cache.new_staging_dir()
        self.assertEqual(self.cache.purge_stale(), 1)
        self.assertTrue(mine.exists())


if __name__ == "__main__":
    unittest.main()
