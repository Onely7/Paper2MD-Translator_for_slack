# app.py
import asyncio
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
from slack_sdk.web.async_client import AsyncWebClient

from bot import actions as _actions  # noqa: F401

# =========================
# Slack Bolt アプリの取り込み（1本化）
# =========================
# bot/__init__.py で生成された AsyncApp を使う
from bot import app as slack_app  # ← これが唯一の AsyncApp
from bot import home as _home  # noqa: F401

# デコレータ（@app.event / @app.action / @app.view / @app.middleware）は
# import 時にリスナー登録されるため、副作用 import が必須
from bot import middleware as _mw  # noqa: F401

# FastAPI 用ハンドラは「bot 側の AsyncApp」を渡す
handler = AsyncSlackRequestHandler(app=slack_app)

logging.basicConfig(level=logging.INFO)

# =========================
# 既存コード互換のための公開変数
# （bot/actions.py が core_app.BOT_USER_ID を参照）
# =========================
# 起動時に一度だけ auth.test して BOT_USER_ID を解決
# ※ 既存構成に合わせて import 時に解決しています。
#   （もしイベントループ衝突が起きる環境なら、FastAPI の startup イベントで解決してください）
try:
    _loop = asyncio.get_event_loop()
    _client_async = AsyncWebClient(token=os.getenv("SLACK_BOT_TOKEN"))
    BOT_USER_ID: str = _loop.run_until_complete(_client_async.auth_test())["user_id"]
except Exception as e:
    logging.warning("Failed to resolve BOT_USER_ID at import time: %s", e)
    BOT_USER_ID = ""  # 起動後に bot/actions 側の _bot_id() で None/空チェック

# =========================
# FastAPI アプリ
# =========================
api = FastAPI()


@api.post("/slack/events")
async def events(request: Request):
    """Slack Events API エンドポイント（URL Verification 対応）"""
    # URL Verification は自前対応しても良いし、handler 側に任せても OK。
    # ここでは後方互換のため自前対応を残す。
    try:
        body = await request.json()
    except Exception:
        body = {}

    if body.get("type") == "url_verification":
        return PlainTextResponse(body["challenge"], status_code=200)

    # それ以外は Bolt 側に委譲
    return await handler.handle(request)


@api.get("/slack/test")
async def test_endpoint():
    return {"message": "Working!"}


# =========================
# エントリーポイント
# =========================
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(api, host="0.0.0.0", port=int(os.getenv("PORT", "8888")))
