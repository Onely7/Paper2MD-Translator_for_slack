"""Tests for the Slack progress bar."""

import unittest
from unittest import mock

from paper2md.progress import TqdmSlack


def make_client():
    client = mock.MagicMock()
    client.chat_postMessage.return_value = {"ts": "1.0"}
    return client


class TqdmSlackTests(unittest.TestCase):
    def test_completed(self):
        client = make_client()
        with TqdmSlack(total=2, client=client, channel="C", thread_ts="1", desc="ページ内要素 解析中") as bar:
            bar.update(1)
            bar.update(1)
        last = client.chat_update.call_args.kwargs["text"]
        self.assertEqual(last, "✅ ページ内要素 解析: 完了しました！（100%）")

    def test_interrupted_is_not_reported_as_done(self):
        client = make_client()
        with self.assertRaises(RuntimeError):
            with TqdmSlack(total=5, client=client, channel="C", thread_ts="1", desc="画像 → テキスト 変換中") as bar:
                bar.update(2)
                raise RuntimeError("boom")
        last = client.chat_update.call_args.kwargs["text"]
        self.assertEqual(last, "⚠️ 画像 → テキスト 変換: 中断しました（2/5）")

    def test_slack_failure_does_not_break_processing(self):
        from slack_sdk.errors import SlackApiError

        client = mock.MagicMock()
        client.chat_postMessage.side_effect = SlackApiError("ratelimited", {"error": "ratelimited"})
        with TqdmSlack(total=1, client=client, channel="C", thread_ts="1") as bar:
            bar.update(1)
        client.chat_update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
