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
from paper2md.config import Settings  # noqa: E402
from paper2md.models import ModelChoice  # noqa: E402


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
        keystore = mock.MagicMock(
            set=mock.AsyncMock(), keys_for=mock.AsyncMock(return_value={}), preferences=mock.AsyncMock(return_value={})
        )
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


def select_options(view, action_id):
    for block in view["blocks"]:
        for element in block.get("elements", []):
            if element.get("action_id") == action_id:
                return [o["value"] for o in element["options"]], element["initial_option"]["value"]
    return None


def polish_checkboxes(view):
    """(the values of the polishing checkboxes, the checked ones)."""
    for block in view["blocks"]:
        for element in block.get("elements", []):
            if element.get("action_id") == "pref_polish_scope":
                return [o["value"] for o in element["options"]], [
                    o["value"] for o in element.get("initial_options", [])
                ]
    return None


def gyazo_checkbox(view):
    """(the value of the Gyazo checkbox, whether it is checked), or None when it is not shown."""
    for block in view["blocks"]:
        for element in block.get("elements", []):
            if element.get("action_id") == "pref_gyazo":
                return element["options"][0]["value"], bool(element.get("initial_options"))
    return None


class GyazoSettingTests(unittest.TestCase):
    def setUp(self):
        self.store = KeyStore(Path(tempfile.mkdtemp()) / "keys.db")
        self.app = FakeApp()
        home.register(self.app, self.store)
        self.client = mock.MagicMock(views_publish=mock.AsyncMock())

    def check(self, checked):
        action = {"selected_options": [{"value": "upload"}] if checked else []}
        asyncio.run(
            self.app.listeners["action:pref_gyazo"](mock.AsyncMock(), {"user": {"id": "U1"}}, action, self.client)
        )

    def test_checkbox_under_the_gyazo_token(self):
        self.assertIsNone(gyazo_checkbox(home.home_view({"openai": "sk", "gyazo": None})))  # no token: no choice
        self.assertEqual(gyazo_checkbox(home.home_view({"openai": "sk", "gyazo": "g"})), ("upload", True))  # default
        view = home.home_view({"gyazo": "g"}, {"gyazo": "off"})
        self.assertEqual(gyazo_checkbox(view), ("upload", False))
        self.assertNotIn("pref_reset", str(view))  # the reset button is for the models

    def test_checkbox_texts_fit_slack(self):
        options = [home.GYAZO_OPTION, *home.POLISH_SCOPE_OPTIONS.values()]
        for label, description in options:
            self.assertLessEqual(len(f"*{label}*"), home.OPTION_TEXT_LIMIT)
            self.assertLessEqual(len(description), home.OPTION_TEXT_LIMIT)

    def test_listener_saves_and_the_model_reset_keeps_it(self):
        self.check(False)
        self.assertEqual(asyncio.run(self.store.preferences("U1")), {"gyazo": "off"})
        self.client.views_publish.assert_awaited()
        asyncio.run(home.change_preference(self.store, "U1", "pref_summary_effort", "high"))
        asyncio.run(self.app.listeners["action:pref_reset"](mock.AsyncMock(), {"user": {"id": "U1"}}, self.client))
        self.assertEqual(asyncio.run(self.store.preferences("U1")), {"gyazo": "off"})
        self.check(True)
        self.assertEqual(asyncio.run(self.store.preferences("U1")), {"gyazo": "on"})


class ModelMenuTests(unittest.TestCase):
    def setUp(self):
        self.store = KeyStore(Path(tempfile.mkdtemp()) / "keys.db")

    def pick(self, action_id, value):
        return asyncio.run(home.change_preference(self.store, "U1", action_id, value))

    def saved(self):
        return asyncio.run(self.store.preferences("U1"))

    def test_menus_offer_only_accepted_efforts(self):
        view = home.home_view({}, {"translation_model": "gpt-6.1-sol", "translation_effort": "high"})
        models, current = select_options(view, "pref_translation_model")
        self.assertEqual(models, ["gpt-6-luna", "gpt-6.1-terra", "gpt-6.1-sol", "gpt-6-astra"])
        self.assertEqual(current, "gpt-6.1-sol")
        efforts, effort = select_options(view, "pref_translation_effort")
        self.assertEqual((efforts, effort), (["low", "medium", "high", "xhigh", "max"], "high"))
        self.assertIn("pref_reset", str(view))
        defaults = home.home_view({})
        self.assertEqual(select_options(defaults, "pref_translation_effort")[0][0], "none")  # Terra accepts none
        self.assertNotIn("pref_reset", str(defaults))

    def test_switching_model_keeps_an_accepted_effort(self):
        self.assertIsNone(self.pick("pref_translation_effort", "none"))  # Terra (default) accepts none
        notice = self.pick("pref_translation_model", "gpt-6-astra")
        self.assertEqual(self.saved(), {"translation_model": "gpt-6-astra", "translation_effort": "low"})
        self.assertIn("none を選べないため、low にしました", notice)
        self.pick("pref_translation_effort", "none")  # not offered for Astra: ignored
        self.assertEqual(self.saved()["translation_effort"], "low")
        self.pick("pref_summary_model", "gpt-unknown")  # not offered: ignored
        self.assertNotIn("summary_model", self.saved())

    def test_polish_menu_and_passes(self):
        view = home.home_view({})
        models, current = select_options(view, "pref_polish_model")
        self.assertEqual(
            (models, current), (["gpt-6-luna", "gpt-6.1-terra", "gpt-6.1-sol", "gpt-6-astra"], "gpt-6.1-sol")
        )
        self.assertEqual(select_options(view, "pref_polish_effort")[1], "high")
        options, checked = polish_checkboxes(view)
        self.assertEqual((options, checked), (["polish_segments", "polish_document"], ["polish_document"]))  # default

        app = FakeApp()
        home.register(app, self.store)
        client = mock.MagicMock(views_publish=mock.AsyncMock())

        def check(*names):
            action = {"selected_options": [{"value": name} for name in names]}
            asyncio.run(
                app.listeners["action:pref_polish_scope"](mock.AsyncMock(), {"user": {"id": "U1"}}, action, client)
            )

        check("polish_segments", "polish_document")
        self.assertEqual(self.saved(), {"polish_segments": "on", "polish_document": "on"})
        choice = ModelChoice.resolve(self.saved(), Settings())
        self.assertEqual(choice.lines()[2], "推敲 `gpt-6.1-sol`（effort: high、ブロックごと＋Markdown 全体）")
        check()  # nothing checked: no polishing
        self.assertEqual(ModelChoice.resolve(self.saved(), Settings()).lines()[2], "推敲 なし")
        self.assertEqual(polish_checkboxes(home.home_view({}, self.saved()))[1], [])
        client.views_publish.assert_awaited()

    def test_glossary_menu(self):
        models, current = select_options(home.home_view({}), "pref_glossary_model")
        self.assertEqual((models[0], current), ("gpt-6-luna", "gpt-6.1-terra"))
        self.assertEqual(select_options(home.home_view({}), "pref_glossary_effort")[1], "high")
        self.pick("pref_glossary_model", "gpt-6.1-sol")
        self.assertEqual(self.saved(), {"glossary_model": "gpt-6.1-sol", "glossary_effort": "high"})
        choice = ModelChoice.resolve(self.saved(), Settings())
        self.assertEqual(choice.lines()[0], "訳語表 `gpt-6.1-sol`（effort: high）")
        self.assertEqual(choice.apply(Settings()).glossary_model, "gpt-6.1-sol")
        # a server without glossaries offers no menu for it
        self.assertIsNone(select_options(home.home_view({}, settings=Settings(glossary=False)), "pref_glossary_model"))
        self.assertNotIn("訳語表", ModelChoice.resolve(self.saved(), Settings(glossary=False)).describe())

    def test_reset_listener(self):
        app = FakeApp()
        home.register(app, self.store)
        self.pick("pref_summary_effort", "high")
        client = mock.MagicMock(views_publish=mock.AsyncMock())
        asyncio.run(app.listeners["action:pref_reset"](mock.AsyncMock(), {"user": {"id": "U1"}}, client))
        self.assertEqual(self.saved(), {})
        client.views_publish.assert_awaited_once()


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
            await store.set_preferences("U1", {"translation_model": "gpt-6-astra", "translation_effort": "max"})
            await store.set_preferences("U1", {"translation_effort": None})
            self.assertEqual(await store.preferences("U1"), {"translation_model": "gpt-6-astra"})
            self.assertEqual(await store.preferences("U2"), {})
            with self.assertRaises(ValueError):
                await store.set_preferences("U1", {"api_key": "x"})
            await store.clear_preferences("U1")
            self.assertEqual(await store.preferences("U1"), {})

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
