import logging
from itertools import islice
from pathlib import Path
from typing import Iterable, List

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


def _chunked(iterable: Iterable, size: int) -> Iterable[List]:
    """iterable を size 件ずつに分割して返すジェネレータ"""
    it = iter(iterable)
    while chunk := list(islice(it, size)):
        yield chunk


def upload_images_in_batches(
    client: WebClient,
    channel: str,
    thread_ts: str | None,
    image_paths: list[str | Path],
    initial_comment: str | None = "*図表:*",
) -> None:
    """
    画像を 10 枚ずつまとめてスレッドに投稿する

    Parameters
    ----------
    client : slack_sdk.WebClient
        認証済み WebClient
    channel : str
        投稿先チャンネル ID
    thread_ts : str | None
        スレッド TS。None なら新規メッセージ
    image_paths : list[str | Path]
        送信したい画像ファイルのパス
    initial_comment : str | None, default コメント
        最初のバッチにだけ付けるメッセージ。不要なら None
    """
    if not image_paths:
        logging.info("[upload_images_in_batches] image_paths is empty – skipping.")
        return

    first_batch = True
    for batch in _chunked(image_paths, 10):  # 10 枚 / API Call が上限
        file_uploads = [
            {
                "file": str(p),
                "filename": Path(p).name,
                "title": Path(p).name,
            }
            for p in batch
        ]
        try:
            client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file_uploads=file_uploads,
                initial_comment=initial_comment
                if first_batch and initial_comment
                else None,
            )
            first_batch = False
        except SlackApiError as e:
            logging.error(
                "Slack upload error: %s (needed=%s / provided=%s)",
                e.response["error"],
                e.response.get("needed"),
                e.response.get("provided"),
            )
            # 失敗しても次のバッチは試す
            continue
