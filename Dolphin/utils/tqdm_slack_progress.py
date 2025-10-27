# slack_progress.py  修正版 (バー表示対応)
from __future__ import annotations

import time

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from tqdm import tqdm


class TqdmSlack(tqdm):
    # ===== ここでバーの見た目と長さを定義 ===============================
    BAR_LEN = 10  # バーの本数（10 ⇒ 10 %/本）
    BAR_DONE = "▓"  # 完了部分のブロック
    BAR_TODO = "░"  # 未完部分のブロック
    # ==================================================================

    def __init__(
        self,
        *args,
        client: WebClient,
        channel: str,
        thread_ts: str | None,
        update_interval: float = 5.0,
        **kwargs,
    ):
        # ------- tqdm より先に属性を用意 --------------------------------
        self.client = client
        self.channel = channel
        self.thread_ts = thread_ts
        self.update_interval = update_interval
        self._last_update = 0.0
        self.msg_ts: str | None = None
        # ----------------------------------------------------------------
        super().__init__(*args, **kwargs)  # tqdm 初期化

        # 進捗メッセージ初回投稿
        resp = self.client.chat_postMessage(
            channel=self.channel,
            thread_ts=self.thread_ts,
            text=self._render_text(),
        )
        self.msg_ts = resp["ts"]

    def _render_text(self) -> str:
        if not self.total:  # total=0 のとき保険
            return f"Progress: {self.n}/? (-)"

        pct_val = self.n / self.total
        pct_txt = f"{pct_val * 100:5.1f}%"

        filled = int(round(self.BAR_LEN * pct_val))
        bar = f"{self.BAR_DONE * filled}{self.BAR_TODO * (self.BAR_LEN - filled)}"

        return f"Progress: {self.n}/{self.total} ({pct_txt})  {bar}"

    # tqdm が定期的に呼ぶ
    def display(self, msg=None, **kwargs):  # type: ignore[override]
        now = time.time()
        if self.msg_ts and (
            now - self._last_update >= self.update_interval or self.n == self.total
        ):
            try:
                self.client.chat_update(
                    channel=self.channel,
                    ts=self.msg_ts,
                    text=self._render_text(),
                )
            except SlackApiError as e:
                tqdm.write(f"[TqdmSlack] {e.response['error']}")
            self._last_update = now
        super().display(msg, **kwargs)

    def close(self):
        super().close()
        if self.msg_ts:
            try:
                self.client.chat_update(
                    channel=self.channel,
                    ts=self.msg_ts,
                    text="✅ 完了しました！（100%）",
                )
            except SlackApiError:
                pass
