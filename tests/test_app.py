"""Starting the bot (app.py) in Socket Mode, without connecting to Slack."""

import os
import unittest
from unittest import mock

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler  # noqa: E402
from slack_bolt.async_app import AsyncApp  # noqa: E402

import app  # noqa: E402


class SocketModeStartTest(unittest.TestCase):
    def test_main_starts_the_socket_mode_handler(self):
        # The real handler opens an aiohttp session in its constructor, which fails outside a running event loop.
        started = []

        async def fake_start(handler):
            started.append(handler.app_token)
            await handler.client.aiohttp_client_session.close()

        slack_app = AsyncApp(token="xoxb-test", signing_secret="secret")
        with (
            mock.patch.dict(os.environ, {"SLACK_APP_TOKEN": "xapp-test"}),
            mock.patch.object(app, "create_app", return_value=slack_app),
            mock.patch.object(app.logging, "basicConfig"),
            mock.patch.object(app.os, "umask"),
            mock.patch.object(AsyncSocketModeHandler, "start_async", fake_start),
        ):
            app.main()
        self.assertEqual(started, ["xapp-test"])


if __name__ == "__main__":
    unittest.main()
