"""
bot/home.py  –  OpenAI & Gemini API Key 対応版
"""

import logging

from slack_sdk.web.async_client import AsyncWebClient

from . import app
from .db import keystore

logger = logging.getLogger(__name__)


# ================= Home Tab =========================
async def _publish_home(client: AsyncWebClient, user_id: str) -> None:
    """ユーザーごとに OpenAI / Gemini の登録状況を表示"""
    has_openai = await keystore.has_key(f"{user_id}_openai")
    has_gemini = await keystore.has_key(f"{user_id}_gemini")
    has_claude = await keystore.has_key(f"{user_id}_claude")
    has_gyazo = await keystore.has_key(f"{user_id}_gyazo")

    status_openai = "✅ 登録済み" if has_openai else "❌ 未登録"
    status_gemini = "✅ 登録済み" if has_gemini else "❌ 未登録"
    status_claude = "✅ 登録済み" if has_claude else "❌ 未登録"
    status_gyazo = "✅ 登録済み" if has_gyazo else "❌ 未登録"

    resp = await client.views_publish(
        user_id=user_id,
        view={
            "type": "home",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*OpenAI API Key*　→　{status_openai}",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Gemini API Key*　→　{status_gemini}",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Claude API Key*　→　{status_claude}",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Gyazo API Key*　→　{status_gyazo}",
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        # どちらのキーも同じモーダルで登録
                        {
                            "type": "button",
                            "action_id": "open_key_modal",
                            "text": {"type": "plain_text", "text": "キーを登録 / 更新"},
                        }
                    ],
                },
            ],
        },
    )
    if not resp.get("ok"):
        logger.error("views.publish failed: %s", resp)


@app.event("app_home_opened")
async def handle_home_opened(event, client: AsyncWebClient):
    uid = event["user"]
    await _publish_home(client, uid)
    logger.debug("Home published for %s", uid)


# =============== モーダル表示 / 保存 =================
@app.action("open_key_modal")
async def show_key_modal(ack, body, client: AsyncWebClient):
    await ack()
    await client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "save_key",
            "title": {"type": "plain_text", "text": "API Key 登録"},
            "submit": {"type": "plain_text", "text": "保存"},
            "close": {"type": "plain_text", "text": "キャンセル"},
            "blocks": [
                {  # ① プロバイダ選択
                    "type": "input",
                    "block_id": "provider",
                    "label": {"type": "plain_text", "text": "プロバイダ"},
                    "element": {
                        "type": "static_select",
                        "action_id": "provider_select",
                        "initial_option": {
                            "text": {"type": "plain_text", "text": "OpenAI"},
                            "value": "openai",
                        },
                        "options": [
                            {
                                "text": {"type": "plain_text", "text": "OpenAI"},
                                "value": "openai",
                            },
                            {
                                "text": {"type": "plain_text", "text": "Gemini"},
                                "value": "gemini",
                            },
                            {
                                "text": {"type": "plain_text", "text": "Claude"},
                                "value": "claude",
                            },
                            {
                                "text": {"type": "plain_text", "text": "Gyazo"},
                                "value": "gyazo",
                            },
                        ],
                    },
                },
                {  # ② API Key 入力
                    "type": "input",
                    "block_id": "keyblock",
                    "label": {"type": "plain_text", "text": "API Key"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "key_input",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "sk-... / xyz-...",
                        },
                    },
                },
            ],
        },
    )


@app.view("save_key")
async def save_key(ack, body, view, client: AsyncWebClient):
    await ack()
    uid = body["user"]["id"]

    provider = view["state"]["values"]["provider"]["provider_select"][
        "selected_option"
    ]["value"]
    key_value = view["state"]["values"]["keyblock"]["key_input"]["value"].strip()

    # 省略チェック (OpenAI は sk- / Gemini は AI... が一般的だが厳密に弾かない)
    if not key_value:
        await client.chat_postEphemeral(
            channel=uid, user=uid, text="❌ 空のキーは保存できません。"
        )
        return

    key_name = f"{uid}_{provider}"
    await keystore.save_key(key_name, key_value)
    logger.info("%s key saved for %s", provider, uid)

    await _publish_home(client, uid)
