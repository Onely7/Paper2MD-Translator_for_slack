"""
bot/actions.py
チャット処理・待機キュー・同時実行制御
"""

import asyncio
import logging
import os
from collections import deque
from pathlib import Path
from typing import List

import aiohttp
import app as core_app  # ← BOT_USER_ID を動的参照

# ─── アプリ固有ユーティリティ（同期想定） ─────────────────────────
from main import url2md_translator
from slack_multi_image_uploader import upload_images_in_batches
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from tools.data_utils import download_pdf, extract_pdf_url
from tools.postprocess import formatting_summary_section

from . import app  # bot/__init__.py が生成
from .middleware import inject_openai_client  # noqa: F401  (副作用 import)

PDF_GUIDE_PATH = Path("./description.pdf")

# ─── Slack クライアント ─────────────────────────────────────────────
client_async = AsyncWebClient(token=os.environ["SLACK_BOT_TOKEN"])
client_sync = WebClient(token=os.environ["SLACK_BOT_TOKEN"])

# ─── 同時実行制御 ──────────────────────────────────────────────────
MAX_CONCURRENT = 2
task_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
queue_waiting: deque[str] = deque()
queue_lock = asyncio.Lock()
processed_events: set[str] = set()
processed_lock = asyncio.Lock()
# キュー管理のグローバル
waiting_message_ts: dict[str, str] = {}  # thread_ts → bot_message_ts


# ─── 内部ヘルパ ───────────────────────────────────────────────────
def _bot_id() -> str | None:
    """lifespan 後にセットされた BOT_USER_ID を取得"""
    return core_app.BOT_USER_ID


def _mentioned(text: str) -> bool:
    bid = _bot_id()
    return bool(bid and f"<@{bid}>" in text)


async def _download_slack_file(
    url: str,
    filename: str,
    output_dir: str,
) -> str:
    dest = Path(output_dir, filename)
    async with aiohttp.ClientSession(
        headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"}
    ) as sess:
        async with sess.get(url) as resp:
            resp.raise_for_status()
            dest.write_bytes(await resp.read())
    return str(dest)


async def _post_results_to_slack(
    *,
    channel: str,
    thread_ts: str,
    summary: str,
    pdf_path: str,
    md_path: str,
    image_paths: List[str],
) -> None:
    """PDF・Markdown・要約・画像を 1 スレッドに投稿"""
    try:
        # 1) 要約
        # 箇条書き体裁変更
        modified_summary = summary.replace("- ", "・")

        await client_async.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=(
                "*============================*\n"
                "*========　🐾　要約　=========*\n"
                "*============================*\n\n"
                f"{modified_summary}\n"
            ),
        )
        # PDF
        await client_async.files_upload_v2(
            channel=channel,
            thread_ts=thread_ts,
            file=pdf_path,
            filename=Path(pdf_path).name,
            title=Path(pdf_path).name,
        )
        # Markdown
        await client_async.files_upload_v2(
            channel=channel,
            thread_ts=thread_ts,
            file=md_path,
            filename=Path(md_path).name,
            title=Path(md_path).name,
        )
        # 図表画像（同期実装）
        await asyncio.to_thread(
            upload_images_in_batches,
            client_sync,
            channel,
            thread_ts,
            image_paths,
        )
    except SlackApiError as e:
        logging.error("Slack upload error: %s", e.response["error"])


# ─── メイン処理コア ───────────────────────────────────────────────
async def _handle_message_core(event: dict, context) -> None:
    channel = event["channel"]
    thread_ts = event.get("thread_ts", event["ts"])
    text = event.get("text", "")

    pdf_source_url: str | None = None
    pdf_filename: str | None = None
    pdf_save_dir = os.getenv(
        "PDF_WORKDIR",
        "/work00/paper2md_translator/resources",
    )
    result_save_dir = os.getenv(
        "RESULT_DIR",
        "/work00/paper2md_translator/results",
    )
    Path(pdf_save_dir).mkdir(parents=True, exist_ok=True)
    Path(result_save_dir).mkdir(parents=True, exist_ok=True)

    # 1) 添付ファイル優先でチェック
    for f in event.get("files", []):
        if f.get("mimetype") == "application/pdf":
            pdf_source_url = f["url_private_download"]
            pdf_filename = f["name"]
            break

    # 2) 添付が無ければ従来の URL 抽出
    if not pdf_source_url:
        pdf_source_url = extract_pdf_url(text)

    if not pdf_source_url:
        await client_async.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="PDF が見つかりませんでした。URL か PDF ファイルを送ってください。",
        )
        return

    try:
        # 3) ダウンロード
        if pdf_source_url.startswith("https://files.slack.com"):
            pdf_path = await _download_slack_file(
                pdf_source_url,
                pdf_filename or "paper.pdf",
                pdf_save_dir,
            )
        else:
            pdf_path = await asyncio.to_thread(
                download_pdf,
                pdf_source_url,
                pdf_save_dir,
            )
        logging.info("Downloaded: %s -> %s", pdf_source_url, pdf_path)

        # ③ 変換 + LLM 要約
        api_keys = {
            "openai": context.get("openai"),
            "gemini": context.get("gemini"),
            "claude": context.get("claude"),
            "gyazo": context.get("gyazo"),
        }
        summary, md_path, fig_paths = await asyncio.to_thread(
            url2md_translator,
            pdf_path,
            result_save_dir,
            True,
            4000,
            True,
            client_sync,
            channel,
            thread_ts,
            api_keys,
        )
        summary = formatting_summary_section(summary)
        # ④ Slack 投稿
        await _post_results_to_slack(
            channel=channel,
            thread_ts=thread_ts,
            summary=summary,
            pdf_path=pdf_path,
            md_path=md_path,
            image_paths=fig_paths,
        )
    except Exception as e:
        logging.exception("Processing error")
        await client_async.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"処理中にエラーが発生しました: {e}",
        )


# ─── キュー & セマフォ制御 ───────────────────────────────────────
async def queue_and_process(event: dict, context):
    """セマフォで同時実行を MAX_CONCURRENT 本に制限し、順位を通知"""
    channel = event["channel"]
    thread_ts = event.get("thread_ts", event["ts"])
    # イベント固有 ID（重複しなければ何でも可）
    event_id = f"{event['ts']}_{event['channel']}"

    # ① --- キューに登録し順位を計算 -----------------------------------
    async with queue_lock:
        queue_waiting.append(event_id)
        position = len(queue_waiting)
        running_now = MAX_CONCURRENT - task_semaphore._value
        total_jobs = running_now + position  # 実行中job数 + 待機job数

        if total_jobs > MAX_CONCURRENT:
            # すでに通知済みなら update だけ
            if thread_ts in waiting_message_ts:
                bot_msg_ts = waiting_message_ts[thread_ts]
                await client_async.chat_update(
                    channel=channel,
                    ts=bot_msg_ts,
                    text=(
                        "現在処理が混み合っています！"
                        f"（待機 {position} 件 / "
                        f"同時実行 {MAX_CONCURRENT} 件）\n"
                        "処理が開始されるまで少々お待ちください…🕒"
                    ),
                )
            else:
                resp = await client_async.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=(
                        "現在処理が混み合っています！"
                        f"（待機 {position} 件 / "
                        f"同時実行 {MAX_CONCURRENT} 件）\n"
                        "処理が開始されるまで少々お待ちください…🕒"
                    ),
                )
                waiting_message_ts[thread_ts] = resp["ts"]

    # ② --- 実行スロットが空くまで待機 -------------------------------
    async with task_semaphore:
        # 3-A) まず自分を「待機キュー」から確実に除外する
        async with queue_lock:
            try:
                queue_waiting.remove(event_id)  # deque 用
            except ValueError:
                pass  # 既に無ければ何もしない

        # 3-B) もし “待機中メッセージ” を出していたら
        #      「順番が来ました ✅」に書き換える（または消しても可）
        if thread_ts in waiting_message_ts:
            try:
                await client_async.chat_update(
                    channel=channel,
                    ts=waiting_message_ts[thread_ts],
                    text="✅ 順番が来たので処理を開始します！",
                )
            except SlackApiError as e:
                logging.warning("chat_update failed: %s", e.response["error"])

        # 3-C) --- 本体処理 --------------------------------------------------
        try:
            await _handle_message_core(event, context)
        finally:
            # 3-D) 終了後に待機メッセージを削除（残っている場合のみ）
            if thread_ts in waiting_message_ts:
                try:
                    await client_async.chat_delete(
                        channel=channel,
                        ts=waiting_message_ts.pop(thread_ts),
                    )
                except SlackApiError as e:
                    logging.warning("chat_delete failed: %s", e.response["error"])


# ─── Slack イベントハンドラ ───────────────────────────────────
@app.event("message")
async def handle_message_events(body, context, logger):
    event = body["event"]
    # BOT 自身の発言・メンション無しは無視
    if event.get("user") == _bot_id() or not _mentioned(event.get("text", "")):
        return

    event_id = f"{event['ts']}_{event['channel']}"
    async with processed_lock:
        if event_id in processed_events:
            return  # ❶ 二重実行を完全に防止
        processed_events.add(event_id)

    asyncio.create_task(queue_and_process(event, context))


# ─── Bot 招待時に PDF を送付 ──────────────────────────────────────
@app.event("member_joined_channel")
async def send_setup_pdf_when_bot_joined(body, context, client, logger):
    """
    ボット自身がチャンネルに招待（join）された瞬間に
    初期設定ガイド PDF をアップロードする。
    """
    event = body["event"]

    # ① 参加したのが BOT か確認
    if event.get("user") != _bot_id():
        return  # 人間メンバー join は無視

    channel_id = event["channel"]

    # ② PDF の存在チェック
    if not PDF_GUIDE_PATH.exists():
        logger.error("Guide PDF not found: %s", PDF_GUIDE_PATH)
        return

    # ③ Slack へアップロード
    try:
        await client.files_upload_v2(
            channel=channel_id,
            file=str(PDF_GUIDE_PATH),
            filename=PDF_GUIDE_PATH.name,
            title="Paper2MD-Translator 初期設定ガイド",
            initial_comment=(
                "ご招待ありがとうございます！🎉\n"
                "まずはこの PDF の説明に従って初回セットアップを完了してください。\n"
                "セットアップが完了したら、以下のような形式でメッセージを送信することで Paper2MD-Translator を利用できます。\n\n"
                "```\n"
                "@Paper2MD-Translator\n"
                "https://arxiv.org/abs/0000.00000\n"
                "```"
            ),
        )
        logger.info("Setup PDF sent to %s", channel_id)
    except SlackApiError as e:
        logger.error("Slack API error while uploading guide PDF: %s", e)
