"""The Slack request flow: cache, arXiv versions, --refresh, --no-cache (all I/O faked)."""

import asyncio
import json
import unittest

from bot import messages
from bot.request import RequestHandler
from tests.bot_harness import KEYS, BotHarness, FakeKeyStore, pdf_bytes

ARXIV = "<@UBOT> <https://arxiv.org/abs/2408.03326>"
RESULT = ["2408.03326v3.pdf", "2408.03326v3.md", "2408.03326v3.zip", "fig01.png"]


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.h = BotHarness(self)

    def test_second_request_is_answered_from_cache(self):
        self.h.run(ARXIV)
        self.h.run("<@UBOT> <https://arxiv.org/pdf/2408.03326v3.pdf>")  # same paper, another link
        self.assertEqual(self.h.pipeline_calls, 1)
        self.assertEqual(self.h.entries(), ["arxiv-2408.03326v3"])
        self.assertTrue(any(t.startswith("♻️") for t in self.h.posted))
        self.assertEqual(self.h.uploads, RESULT * 2)
        self.assertEqual(self.h.zips[0], ["2408.03326v3/2408.03326v3.md", "2408.03326v3/figures/fig01.png"])
        self.assertEqual(self.h.staging_leftovers(), [])
        manifest = json.loads((self.h.cache_dir / "arxiv-2408.03326v3/manifest.json").read_text())
        self.assertEqual((manifest["source"]["arxiv_version"], manifest["title"]), (3, "Paper Title"))

    def test_new_arxiv_version_is_translated(self):
        self.h.arxiv_latest["2408.03326"] = 2
        self.h.run(ARXIV)
        self.h.arxiv_latest["2408.03326"] = 3  # v3 is published later
        self.h.run(ARXIV)
        self.assertEqual(self.h.pipeline_calls, 2)
        self.assertEqual(self.h.entries(), ["arxiv-2408.03326v2", "arxiv-2408.03326v3"])
        self.assertTrue(any("v2 を翻訳済み" in t and "最新版の v3" in t for t in self.h.posted))
        self.h.run("<@UBOT> <https://arxiv.org/abs/2408.03326v2>")  # an explicit old version
        self.assertEqual(self.h.pipeline_calls, 2)

    def test_cache_hit_needs_no_api_key(self):
        self.h.run(ARXIV)
        before = len(self.h.posted)
        self.h.run(ARXIV, keys={})
        self.assertEqual(self.h.pipeline_calls, 1)
        self.assertNotIn(messages.NO_OPENAI_KEY, self.h.posted[before:])

    def test_version_from_download_when_resolution_fails(self):
        self.h.run(ARXIV)
        self.h.resolve_fails = True
        self.h.run(ARXIV)
        self.assertEqual(self.h.pipeline_calls, 1)

    def test_same_file_hits_and_is_not_posted_back(self):
        data = pdf_bytes("uploaded paper")
        self.h.run("<@UBOT>", files=self.h.slack_file("https://files.slack.com/a", "a.pdf", data))
        self.h.run("<@UBOT>", files=self.h.slack_file("https://files.slack.com/b", "renamed.pdf", data))
        self.assertEqual(self.h.pipeline_calls, 1)
        self.assertEqual(self.h.uploads, ["a.md", "a.zip", "fig01.png"] * 2)  # no PDF: the user attached it
        self.h.run("<@UBOT>", files=self.h.slack_file("https://files.slack.com/c", "a.pdf", pdf_bytes("other")))
        self.assertEqual(self.h.pipeline_calls, 2)

    def test_same_url_and_changed_content(self):
        url = "https://example.com/papers/foo.pdf"
        self.h.urls[url] = pdf_bytes("foo")
        self.h.run(f"<@UBOT> <{url}>")
        self.h.run(f"<@UBOT> <{url}>")
        self.assertEqual(self.h.pipeline_calls, 1)
        self.h.urls[url] = pdf_bytes("foo, revised")
        self.h.run(f"<@UBOT> <{url}>")
        self.assertEqual(self.h.pipeline_calls, 2)

    def test_arxiv_pdf_uploaded_as_file_hits_by_hash(self):
        self.h.run(ARXIV)
        self.h.run("<@UBOT>", files=self.h.slack_file("https://files.slack.com/x", "x.pdf", pdf_bytes("2408.03326v3")))
        self.assertEqual(self.h.pipeline_calls, 1)

    def test_gyazo_markdown_is_posted_when_available(self):
        self.h.run(ARXIV, keys={"openai": "sk", "gyazo": "g"})
        entry = self.h.cache_dir / "arxiv-2408.03326v3"
        self.assertTrue((entry / "output/paper.gyazo.md").is_file())
        self.assertEqual(self.h.uploads, RESULT)

    def test_refresh_replaces(self):
        sol = {"translation_model": "gpt-6.1-sol"}
        self.h.run(ARXIV)
        self.h.run("<@UBOT> --refresh <https://arxiv.org/abs/2408.03326>", preferences=sol)
        self.assertEqual(self.h.pipeline_calls, 2)
        self.h.run(ARXIV, preferences=sol)
        self.assertEqual(self.h.pipeline_calls, 2)
        hit = [t for t in self.h.posted if t.startswith("♻️")][-1]
        self.assertIn("`gpt-6.1-sol`（effort: high）", hit)  # the stored result was made with Sol
        self.assertNotIn("今の設定", hit)  # ... which is also the user's choice

    def test_user_models_reach_the_pipeline(self):
        preferences = {
            "translation_model": "gpt-6.1-sol",
            "translation_effort": "none",  # Sol has no "none": the closest effort is used
            "summary_model": "gpt-6-luna",
            "summary_effort": "max",
        }
        self.h.run(ARXIV, preferences=preferences)
        settings = self.h.pipeline_settings[0]
        self.assertEqual(
            (settings.translation_model, settings.translation_effort, settings.summary_model, settings.summary_effort),
            ("gpt-6.1-sol", "low", "gpt-6-luna", "max"),
        )
        self.assertTrue(any(t.startswith("🤖") and "`gpt-6.1-sol`（effort: low）" in t for t in self.h.posted))
        manifest = json.loads((self.h.cache_dir / "arxiv-2408.03326v3/manifest.json").read_text())
        self.assertEqual((manifest["translation_model"], manifest["summary_effort"]), ("gpt-6.1-sol", "max"))

    def test_first_message_lists_the_models(self):
        self.h.run(ARXIV)
        started = next(t for t in self.h.posted if t.startswith("🤖"))
        self.assertEqual(
            started,
            "🤖\n\n• 訳語表 `gpt-6.1-terra`（effort: high）\n• 翻訳 `gpt-6.1-terra`（effort: high）\n"
            "• 推敲 `gpt-6.1-sol`（effort: high、Markdown 全体）\n• 要約 `gpt-6.1-sol`（effort: high）\n\nで処理します。",
        )
        manifest = json.loads((self.h.cache_dir / "arxiv-2408.03326v3/manifest.json").read_text())
        self.assertEqual((manifest["polish_model"], manifest["polish_effort"]), ("gpt-6.1-sol", "high"))
        self.assertEqual((manifest["polish_segments"], manifest["polish_document"]), (False, True))

    def test_polishing_switched_off(self):
        off = {"polish_segments": "off", "polish_document": "off"}
        self.h.run(ARXIV, preferences=off)
        self.assertTrue(any("• 推敲 なし" in t for t in self.h.posted))
        settings = self.h.pipeline_settings[0]
        self.assertEqual((settings.polish_segments, settings.polish_document), (False, False))
        self.h.run(ARXIV, preferences=off)  # the stored result (not polished) matches the choice
        self.assertNotIn("今の設定", [t for t in self.h.posted if t.startswith("♻️")][-1])
        self.h.run(ARXIV)  # the default polishes the whole Markdown: the bot says how to translate again
        self.assertIn("今の設定", [t for t in self.h.posted if t.startswith("♻️")][-1])

    def test_polish_error_is_reported(self):
        self.h.polish_error = "ModelNotAvailableError: no"
        self.h.run(ARXIV)
        self.assertTrue(any("推敲していない訳文" in t and "ModelNotAvailableError" in t for t in self.h.posted))

    def test_cache_hit_tells_the_models(self):
        self.h.run(ARXIV)
        self.h.run(ARXIV, preferences={"translation_model": "gpt-6-astra", "translation_effort": "high"})
        hit = [t for t in self.h.posted if t.startswith("♻️")][-1]
        self.assertIn("今の設定（訳語表 `gpt-6.1-terra`（effort: high）、翻訳 `gpt-6-astra`（effort: high）", hit)
        self.assertIn(
            "保存済みの結果を返します（訳語表 `gpt-6.1-terra`（effort: high）、翻訳 `gpt-6.1-terra`（effort: high）",
            hit,
        )
        self.assertEqual(self.h.pipeline_calls, 1)  # the stored result is still returned

    def test_partial_failure_is_not_cached(self):
        self.h.failed_segments = 1
        self.h.run(ARXIV)
        self.assertEqual((self.h.entries(), self.h.staging_leftovers()), ([], []))
        self.assertTrue(any("キャッシュしない" in t for t in self.h.posted))
        self.h.failed_segments = 0
        self.h.run(ARXIV)
        self.assertEqual((self.h.pipeline_calls, self.h.entries()), (2, ["arxiv-2408.03326v3"]))

    def test_error_leaves_no_staging(self):
        self.h.pipeline_error = RuntimeError("model crashed")
        self.h.run(ARXIV)
        self.assertEqual((self.h.entries(), self.h.staging_leftovers()), ([], []))
        self.assertTrue(any("処理中にエラーが発生しました" in t for t in self.h.posted))

    def test_missing_key_and_missing_paper(self):
        self.h.run(ARXIV, keys={})
        self.assertIn(messages.NO_OPENAI_KEY, self.h.posted)
        self.h.run("<@UBOT> hello")
        self.assertEqual(self.h.posted[-1], messages.NO_PAPER)
        self.assertEqual(self.h.pipeline_calls, 0)

    def test_concurrent_requests_translate_once(self):
        self.h.pipeline_delay = 0.3
        handler = RequestHandler(FakeKeyStore(KEYS))

        async def both():
            await asyncio.gather(
                handler.handle(self.h.client, self.h.event(ARXIV, ts="1.0"), "U1"),
                handler.handle(self.h.client, self.h.event(ARXIV, ts="2.0"), "U2"),
            )

        asyncio.run(both())
        self.assertEqual(self.h.pipeline_calls, 1)
        self.assertIn(messages.SAME_PAPER_WAIT, self.h.posted)
        self.assertEqual(self.h.uploads.count("2408.03326v3.md"), 2)


class NoCacheTests(unittest.TestCase):
    def setUp(self):
        self.h = BotHarness(self)

    def test_leaves_nothing(self):
        self.h.run("<@UBOT> --no-cache <https://arxiv.org/abs/2408.03326>")
        self.assertEqual(self.h.pipeline_calls, 1)
        self.assertEqual((list(self.h.shm.iterdir()), self.h.entries(), self.h.staging_leftovers()), ([], [], []))
        self.assertEqual((self.h.posted[0], self.h.posted[-1]), (messages.NO_CACHE_NOTICE, messages.NO_CACHE_DONE))
        self.assertEqual(self.h.uploads, RESULT)

    def test_cleans_up_on_error(self):
        self.h.pipeline_error = RuntimeError("model crashed")
        self.h.run("<@UBOT> キャッシュなし <https://arxiv.org/abs/2408.03326>")
        self.assertEqual((list(self.h.shm.iterdir()), self.h.entries()), ([], []))
        self.assertEqual(self.h.posted[-1], messages.NO_CACHE_DONE)

    def test_reads_the_cache_but_never_writes(self):
        self.h.run(ARXIV)
        entry = self.h.cache_dir / "arxiv-2408.03326v3"
        files = sorted(entry.rglob("*"))
        self.h.run("<@UBOT> --no-cache <https://arxiv.org/abs/2408.03326>")
        self.assertEqual(self.h.pipeline_calls, 1)
        self.assertEqual(sorted(entry.rglob("*")), files)
        self.h.run("<@UBOT> --no-cache --refresh <https://arxiv.org/abs/2408.03326>")
        self.assertEqual((self.h.pipeline_calls, self.h.entries()), (2, ["arxiv-2408.03326v3"]))
        self.assertEqual(list(self.h.shm.iterdir()), [])

    def test_upload_is_not_kept(self):
        files = self.h.slack_file("https://files.slack.com/secret", "draft.pdf", pdf_bytes("unpublished"))
        self.h.run("<@UBOT> --no-cache", files=files)
        self.assertEqual((self.h.entries(), list(self.h.shm.iterdir())), ([], []))
        self.assertFalse((self.h.cache_dir / "_index").exists())
        self.assertEqual(self.h.uploads, ["draft.md", "draft.zip", "fig01.png"])


if __name__ == "__main__":
    unittest.main()
