"""Slack bot (Slack Bolt): ``create_app()`` builds the app with every handler registered."""

from __future__ import annotations

import os

from slack_bolt.async_app import AsyncApp


def create_app() -> AsyncApp:
    from . import cancel, events, home
    from .keystore import KeyStore
    from .request import RequestHandler

    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"], signing_secret=os.environ.get("SLACK_SIGNING_SECRET"))
    keystore = KeyStore()
    handler = RequestHandler(keystore)
    events.register(app, handler)
    home.register(app, keystore)
    cancel.register(app, handler.running)
    return app
