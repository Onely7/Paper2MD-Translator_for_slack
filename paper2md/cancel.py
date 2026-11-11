"""
Stopping a translation that is under way (the 中断する button in Slack).

``CancelToken`` is set from the Slack bot's event loop and checked by the
pipeline in its worker thread: after each page of the layout analysis, before
each stage, and while OpenAI requests are running (``OpenAIClient`` drops the
requests in flight).  ``Cancelled`` is a ``BaseException``, so the handlers
that catch ``Exception`` to keep going after a failed step (a polishing pass,
the glossary) let it through, while ``finally`` blocks still run (the GPU
model is unloaded and the work directory removed).
"""

from __future__ import annotations

import asyncio
import threading


class Cancelled(BaseException):
    """The user stopped the job."""


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        """Raise ``Cancelled`` once the job has been stopped."""
        if self._event.is_set():
            raise Cancelled()

    async def wait(self, interval: float = 0.2) -> None:
        """Return when the job is stopped (works in any event loop and thread)."""
        while not self._event.is_set():
            await asyncio.sleep(interval)


async def until_cancelled(awaitable, token: CancelToken | None):
    """
    Await ``awaitable``, or raise ``Cancelled`` (and cancel it) when ``token`` is set first.

    >>> async def demo():
    ...     token = CancelToken()
    ...     token.cancel()
    ...     try:
    ...         await until_cancelled(asyncio.sleep(10), token)
    ...     except Cancelled:
    ...         return "stopped"
    >>> asyncio.run(demo())
    'stopped'
    """
    work = asyncio.ensure_future(awaitable)
    if token is None:
        return await work
    watcher = asyncio.ensure_future(token.wait())
    try:
        done, _ = await asyncio.wait({work, watcher}, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        work.cancel()
        raise
    finally:
        watcher.cancel()
    if work in done:
        return work.result()
    work.cancel()
    try:
        await work
    except (asyncio.CancelledError, Exception):
        pass
    raise Cancelled()
