"""Slack event filtering, the Home tab and the key store."""

import asyncio
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")

from bot import events, home  # noqa: E402
from bot.keystore import KeyStore  # noqa: E402


class FakeApp:
    """Collects the listeners that register() attaches."""

    def __init__(self):
        self.listeners = {}

    def _decorator(self, name):
        def wrap(func):
            self.listeners[name] = func
            return func

        return wrap

    def event(self, name):
        return self._decorator(f"event:{name}")

    def action(self, name):
        return self._decorator(f"action:{getattr(name, 'pattern', name)}")

    def view(self, name):
        return self._decorator(f"view:{name}")


class MessageFilterTests(unittest.TestCase):
    def setUp(self):
        self.app = FakeApp()
        self.handler = SimpleNamespace(handle=mock.AsyncMock())
        events.register(self.app, self.handler)
        self.context = SimpleNamespace(bot_user_id="UBOT")

    def send(self, **event):
        event = {"channel": "C1", "ts": "1.0", "user": "U1", "text": "<@UBOT> <https://arxiv.org/abs/1>", **event}

        async def run():
            await self.app.listeners["event:message"](event, self.context, mock.MagicMock())
            await asyncio.sleep(0)

        asyncio.run(run())

    def test_filters(self):
        self.send(text="no mention")
        self.send(subtype="message_changed")
        self.send(bot_id="B1")
        self.send(user="UBOT")
        self.assertEqual(self.handler.handle.await_count, 0)
        self.send(subtype="file_share", ts="2.0")
        self.send(ts="3.0")
        self.send(ts="3.0")  # Slack retried the same event
        self.assertEqual(self.handler.handle.await_count, 2)

    def test_recent_ids_are_bounded(self):
        ids = events.RecentIds(size=2)
        self.assertTrue(ids.add("a") and ids.add("b") and ids.add("c"))
        self.assertTrue(ids.add("a"))  # forgotten
        self.assertFalse(ids.add("c"))


class HomeTests(unittest.TestCase):
    def test_view(self):
        view = home.home_view({"openai": "sk-abcdefghijkl1234", "gyazo": None})
        texts = str(view["blocks"])
        self.assertIn("••••1234", texts)
        self.assertNotIn("abcdefghijkl", texts)
        self.assertIn("delete_key_openai", texts)
        self.assertNotIn("delete_key_gyazo", texts)
        self.assertIn("gpt-6.1-terra", texts)

    def test_save_key(self):
        app = FakeApp()
        keystore = mock.MagicMock(set=mock.AsyncMock(), keys_for=mock.AsyncMock(return_value={}))
        home.register(app, keystore)
        save = app.listeners["view:save_key"]

        def submit(key, problem=None):
            ack = mock.AsyncMock()
            view = {
                "state": {
                    "values": {
                        "provider": {"provider_select": {"selected_option": {"value": "openai"}}},
                        "keyblock": {"key_input": {"value": key}},
                    }
                }
            }
            client = mock.MagicMock(views_publish=mock.AsyncMock())
            with mock.patch.object(home, "validate_key", mock.AsyncMock(return_value=problem)):
                asyncio.run(save(ack, {"user": {"id": "U1"}}, view, client))
            return ack

        ack = submit("  ")
        self.assertEqual(ack.await_args.kwargs["response_action"], "errors")
        ack = submit("sk-bad", problem="rejected")
        self.assertEqual(ack.await_args.kwargs["errors"], {"keyblock": "rejected"})
        keystore.set.assert_not_awaited()
        submit("sk-good")
        keystore.set.assert_awaited_once_with("U1", "openai", "sk-good")


class KeyStoreTests(unittest.TestCase):
    def test_crud_and_permissions(self):
        path = Path(tempfile.mkdtemp()) / "keys.db"
        store = KeyStore(path)

        async def run():
            self.assertIsNone(await store.get("U1", "openai"))
            await store.set("U1", "openai", "sk-1")
            await store.set("U1", "openai", "sk-2")
            await store.set("U1", "gyazo", "g-1")
            self.assertEqual(await store.keys_for("U1"), {"openai": "sk-2", "gyazo": "g-1"})
            await store.delete("U1", "gyazo")
            self.assertEqual(await store.keys_for("U1"), {"openai": "sk-2", "gyazo": None})
            with self.assertRaises(ValueError):
                await store.get("U1", "gemini")

        asyncio.run(run())
        for candidate in path.parent.iterdir():
            self.assertEqual(stat.S_IMODE(candidate.stat().st_mode), 0o600, candidate.name)

    def test_reads_keys_saved_by_the_previous_version(self):
        import sqlite3

        path = Path(tempfile.mkdtemp()) / "keys.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE user_keys (key_name TEXT PRIMARY KEY, api_key TEXT NOT NULL)")
        conn.execute("INSERT INTO user_keys VALUES ('U1_openai', 'sk-old')")
        conn.commit()
        conn.close()
        self.assertEqual(asyncio.run(KeyStore(path).get("U1", "openai")), "sk-old")


if __name__ == "__main__":
    unittest.main()
