"""Slack events: mentions with a paper, and the bot joining a channel."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict

from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError

from paper2md.config import REPO_ROOT

from . import messages
from .request import RequestHandler

logger = logging.getLogger(__name__)

GUIDE_PDF = REPO_ROOT / "docs" / "description.pdf"
HANDLED_SUBTYPES = (None, "file_share")  # plain messages and messages with files


class RecentIds:
    """Remembers the last ``size`` ids (Slack may deliver an event more than once)."""

    def __init__(self, size: int = 2048) -> None:
        self.size = size
        self._ids: OrderedDict[str, None] = OrderedDict()

    def add(self, item: str) -> bool:
        """Return False if ``item`` was seen recently."""
        if item in self._ids:
            return False
        self._ids[item] = None
        if len(self._ids) > self.size:
            self._ids.popitem(last=False)
        return True


def register(app: AsyncApp, handler: RequestHandler) -> None:
    seen = RecentIds()
    tasks: set[asyncio.Task] = set()  # keep references so running tasks are not garbage-collected

    @app.event("message")
    async def on_message(event, context, client):
        if event.get("subtype") not in HANDLED_SUBTYPES or event.get("bot_id"):
            return
        bot_user_id = context.bot_user_id
        if not bot_user_id or event.get("user") == bot_user_id or f"<@{bot_user_id}>" not in (event.get("text") or ""):
            return
        if not seen.add(f"{event['channel']}:{event['ts']}"):
            return
        task = asyncio.create_task(handler.handle(client, event, event.get("user")))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    @app.event("member_joined_channel")
    async def on_member_joined(event, context, client):
        if event.get("user") != context.bot_user_id:
            return
        comment = messages.WELCOME + messages.USAGE
        try:
            if GUIDE_PDF.exists():
                await client.files_upload_v2(
                    channel=event["channel"],
                    file=str(GUIDE_PDF),
                    filename=GUIDE_PDF.name,
                    title="Paper2MD-Translator 初期設定ガイド",
                    initial_comment=comment,
                )
            else:
                await client.chat_postMessage(channel=event["channel"], text=comment)
        except SlackApiError as e:
            logger.error("Could not post the guide: %s", e.response.get("error"))
