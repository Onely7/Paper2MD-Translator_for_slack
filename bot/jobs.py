"""Limits on concurrent translations and on translating the same paper twice."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from paper2md.cancel import Cancelled, CancelToken, until_cancelled

from . import messages

logger = logging.getLogger(__name__)


class JobQueue:
    """At most ``limit`` translations run at once; waiting requests see their position."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._semaphore = asyncio.Semaphore(limit)
        self._waiting: deque[str] = deque()
        self._running = 0

    async def _acquire(self, cancel: CancelToken | None) -> None:
        """Wait for a free slot, or raise ``Cancelled`` when the job is stopped while it waits."""
        acquire = asyncio.ensure_future(self._semaphore.acquire())
        try:
            await until_cancelled(acquire, cancel)
        except Cancelled:
            if acquire.done() and not acquire.cancelled() and acquire.exception() is None:
                self._semaphore.release()  # the slot came free at the same moment: give it back
            raise

    @asynccontextmanager
    async def slot(
        self, client: AsyncWebClient, channel: str, thread_ts: str, job_id: str, cancel: CancelToken | None = None
    ) -> AsyncIterator[None]:
        self._waiting.append(job_id)
        notice_ts = None
        if self._running + len(self._waiting) > self.limit:
            text = messages.QUEUE_WAIT.format(position=len(self._waiting), limit=self.limit)
            try:
                notice_ts = (await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text))["ts"]
            except SlackApiError as e:
                logger.warning("chat_postMessage failed: %s", e.response.get("error"))
        try:
            try:
                await self._acquire(cancel)
            except Cancelled:
                if notice_ts:
                    await self._edit(client, channel, notice_ts, messages.QUEUE_CANCELLED)
                raise
            self._waiting.remove(job_id)
            self._running += 1
            try:
                if notice_ts:
                    await self._edit(client, channel, notice_ts, messages.QUEUE_START)
                yield
            finally:
                self._running -= 1
                self._semaphore.release()
        finally:
            if job_id in self._waiting:  # cancelled while waiting
                self._waiting.remove(job_id)

    @staticmethod
    async def _edit(client: AsyncWebClient, channel: str, ts: str, text: str) -> None:
        try:
            await client.chat_update(channel=channel, ts=ts, text=text)
        except SlackApiError as e:
            logger.warning("chat_update failed: %s", e.response.get("error"))


class PaperLocks:
    """One translation per paper at a time; later requests wait and then use the cache."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._users: dict[str, int] = {}

    @asynccontextmanager
    async def hold(self, key: str, on_wait: Callable[[], Awaitable[object]] | None = None) -> AsyncIterator[None]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._users[key] = self._users.get(key, 0) + 1
        try:
            if lock.locked() and on_wait is not None:
                await on_wait()
            async with lock:
                yield
        finally:
            self._users[key] -= 1
            if self._users[key] == 0:
                del self._users[key]
                self._locks.pop(key, None)
