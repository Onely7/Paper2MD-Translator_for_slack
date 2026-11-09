"""Progress bars for the console (tqdm) and for a Slack thread."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Protocol

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from tqdm import tqdm

logger = logging.getLogger(__name__)


class ProgressBar(Protocol):
    def update(self, n: int = 1) -> object: ...


ProgressFactory = Callable[[int, str], AbstractContextManager[ProgressBar]]


@contextmanager
def console_progress(total: int, desc: str) -> Iterator[ProgressBar]:
    with tqdm(total=total, desc=desc) as bar:
        yield bar


def slack_progress(client: WebClient, channel: str, thread_ts: str) -> ProgressFactory:
    """Progress bars posted (and updated) as messages in a Slack thread."""

    @contextmanager
    def factory(total: int, desc: str) -> Iterator[ProgressBar]:
        with TqdmSlack(total=total, desc=desc, client=client, channel=channel, thread_ts=thread_ts) as bar:
            yield bar

    return factory


class TqdmSlack(tqdm):
    """tqdm that mirrors its progress into a Slack thread message."""

    BAR_LEN = 10  # 10 blocks = 10 % each
    BAR_DONE = "▓"
    BAR_TODO = "░"

    def __init__(
        self, *args, client: WebClient, channel: str, thread_ts: str | None, update_interval: float = 5.0, **kwargs
    ):
        # attributes used by display() must exist before tqdm.__init__ calls it
        self.client = client
        self.channel = channel
        self.thread_ts = thread_ts
        self.update_interval = update_interval
        self._last_update = 0.0
        self._final_reported = False
        self.msg_ts: str | None = None
        super().__init__(*args, **kwargs)
        try:
            resp = self.client.chat_postMessage(
                channel=self.channel, thread_ts=self.thread_ts, text=self._render_text()
            )
            self.msg_ts = resp["ts"]
        except SlackApiError as e:  # the job continues without a progress message
            logger.warning("[TqdmSlack] chat_postMessage failed: %s", e.response.get("error"))

    def _render_text(self) -> str:
        label = f"{self.desc}: " if self.desc else ""
        if not self.total:
            return f"{label}Progress: {self.n}/? (-)"
        ratio = min(self.n / self.total, 1.0)
        filled = int(round(self.BAR_LEN * ratio))
        bar = f"{self.BAR_DONE * filled}{self.BAR_TODO * (self.BAR_LEN - filled)}"
        return f"{label}Progress: {self.n}/{self.total} ({ratio * 100:5.1f}%)  {bar}"

    def _update_message(self, text: str) -> None:
        if not self.msg_ts:
            return
        try:
            self.client.chat_update(channel=self.channel, ts=self.msg_ts, text=text)
        except SlackApiError as e:
            logger.warning("[TqdmSlack] chat_update failed: %s", e.response.get("error"))

    def display(self, msg=None, **kwargs):  # called by tqdm on updates
        now = time.time()
        if self.msg_ts and (now - self._last_update >= self.update_interval or self.n == self.total):
            self._update_message(self._render_text())
            self._last_update = now
        return super().display(msg, **kwargs)

    def close(self):
        finished = bool(self.total) and self.n >= self.total
        super().close()
        if self._final_reported:
            return
        self._final_reported = True
        # "ページ内要素 解析中" -> "ページ内要素 解析: "
        label = f"{self.desc.removesuffix('中').strip()}: " if self.desc else ""
        if finished:
            self._update_message(f"✅ {label}完了しました！（100%）")
        else:
            self._update_message(f"⚠️ {label}中断しました（{self.n}/{self.total or '?'}）")
