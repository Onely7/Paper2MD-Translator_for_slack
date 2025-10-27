"""
ユーザー鍵を取り出して context["openai"] に OpenAI API KEY を注入するミドルウェア

bot/middleware.py
────────────────────────────────────────
・Home タブ / インタラクティブ操作は必ず next_() を通す
・API Key 未登録時は何も送らず、そのまま next_() で 200 OK を返す
・登録済みユーザーには context["openai"] に OpenAI API KEY を注入
"""
import logging

from openai import OpenAIError
from slack_bolt.async_app import AsyncBoltContext

from . import app
from .db import keystore

logger = logging.getLogger(__name__)


@app.middleware
async def inject_openai_client(context: AsyncBoltContext, body: dict, next_):
    """ユーザー単位で OpenAI クライアントを注入するミドルウェア"""

    # 1️⃣ Home タブ表示イベントはチェックだけで通過
    if (
        body.get("type") == "event_callback"
        and body.get("event", {}).get("type") == "app_home_opened"
    ):
        await next_()
        return

    user_id: str | None = context.user_id
    if user_id is None:  # system イベントなど
        await next_()
        return

    # 2️⃣ API Key を取得
    openai_api_key = await keystore.get_key(f"{user_id}_openai")
    gemini_api_key = await keystore.get_key(f"{user_id}_gemini")
    claude_api_key = await keystore.get_key(f"{user_id}_claude")
    gyazo_api_key = await keystore.get_key(f"{user_id}_gyazo")
    if not openai_api_key:
        # 未登録時は Home タブで❌表示する方針なので何も送らない
        await next_()
        return

    # 3️⃣ OpenAI API KEY を注入
    context["openai"] = openai_api_key
    context["gemini"] = gemini_api_key
    context["claude"] = claude_api_key
    context["gyazo"] = gyazo_api_key

    # 4️⃣ 後続リスナーへ
    try:
        await next_()
    except OpenAIError as e:
        logger.exception("OpenAI error for user %s: %s", user_id, e)
        # エラー時も 200 を返す
        await next_()
