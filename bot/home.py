"""Home tab: register, check and delete the user's API keys."""

from __future__ import annotations

import logging
import re

import aiohttp
from openai import AsyncOpenAI, AuthenticationError
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from paper2md.config import Settings

from . import messages
from .keystore import PROVIDERS, KeyStore

logger = logging.getLogger(__name__)

PROVIDER_NAMES = {"openai": "OpenAI", "gyazo": "Gyazo"}
PROVIDER_NOTES = {
    "openai": "必須。翻訳と要約に使います。",
    "gyazo": "任意。登録すると、Markdown 内の図表が Gyazo の画像 URL になります。",
}
VALIDATION_TIMEOUT = 2.0  # a modal submission must be acknowledged within 3 seconds


def _mask(key: str) -> str:
    return f"••••{key[-4:]}" if len(key) > 8 else "••••"


async def validate_key(provider: str, key: str) -> str | None:
    """Return an error message when the service rejects the key (other failures are ignored)."""
    try:
        if provider == "openai":
            async with AsyncOpenAI(api_key=key, max_retries=0, timeout=VALIDATION_TIMEOUT) as client:
                await client.models.list()
        elif provider == "gyazo":
            timeout = aiohttp.ClientTimeout(total=VALIDATION_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    "https://api.gyazo.com/api/users/me", headers={"Authorization": f"Bearer {key}"}
                ) as response:
                    if response.status in (401, 403):
                        return "Gyazo に拒否されました。アクセストークンを確認してください。"
    except AuthenticationError:
        return "OpenAI に拒否されました。API Key を確認してください。"
    except Exception as e:  # timeouts and network errors: accept the key
        logger.info("Could not validate the %s key: %s", provider, e)
    return None


def home_view(keys: dict[str, str | None]) -> dict:
    settings = Settings.from_env()
    blocks: list[dict] = [{"type": "header", "text": {"type": "plain_text", "text": "API Key"}}]
    for provider in PROVIDERS:
        key = keys.get(provider)
        status = f"✅ 登録済み（{_mask(key)}）" if key else "❌ 未登録"
        block = {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{PROVIDER_NAMES[provider]}*　{status}\n{PROVIDER_NOTES[provider]}",
            },
        }
        if key:
            block["accessory"] = {
                "type": "button",
                "action_id": f"delete_key_{provider}",
                "text": {"type": "plain_text", "text": "削除"},
                "style": "danger",
                "confirm": {
                    "title": {"type": "plain_text", "text": "API Key の削除"},
                    "text": {"type": "plain_text", "text": f"{PROVIDER_NAMES[provider]} の API Key を削除しますか？"},
                    "confirm": {"type": "plain_text", "text": "削除"},
                    "deny": {"type": "plain_text", "text": "キャンセル"},
                },
            }
        blocks.append(block)
    blocks += [
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "open_key_modal",
                    "text": {"type": "plain_text", "text": "API Key を登録 / 更新"},
                }
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"翻訳モデル: `{settings.translation_model}`　要約モデル: `{settings.summary_model}`",
                }
            ],
        },
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": messages.USAGE}},
    ]
    return {"type": "home", "blocks": blocks}


KEY_MODAL = {
    "type": "modal",
    "callback_id": "save_key",
    "title": {"type": "plain_text", "text": "API Key 登録"},
    "submit": {"type": "plain_text", "text": "保存"},
    "close": {"type": "plain_text", "text": "キャンセル"},
    "blocks": [
        {
            "type": "input",
            "block_id": "provider",
            "label": {"type": "plain_text", "text": "サービス"},
            "element": {
                "type": "static_select",
                "action_id": "provider_select",
                "initial_option": {"text": {"type": "plain_text", "text": "OpenAI"}, "value": "openai"},
                "options": [{"text": {"type": "plain_text", "text": PROVIDER_NAMES[p]}, "value": p} for p in PROVIDERS],
            },
        },
        {
            "type": "input",
            "block_id": "keyblock",
            "label": {"type": "plain_text", "text": "API Key"},
            "element": {
                "type": "plain_text_input",
                "action_id": "key_input",
                "placeholder": {"type": "plain_text", "text": "sk-..."},
            },
        },
    ],
}


def register(app: AsyncApp, keystore: KeyStore) -> None:
    async def publish(client: AsyncWebClient, user_id: str) -> None:
        await client.views_publish(user_id=user_id, view=home_view(await keystore.keys_for(user_id)))

    @app.event("app_home_opened")
    async def on_home_opened(event, client):
        if event.get("tab", "home") == "home":
            await publish(client, event["user"])

    @app.action("open_key_modal")
    async def open_key_modal(ack, body, client):
        await ack()
        await client.views_open(trigger_id=body["trigger_id"], view=KEY_MODAL)

    @app.action(re.compile(r"^delete_key_(openai|gyazo)$"))
    async def delete_key(ack, body, action, client):
        await ack()
        user_id = body["user"]["id"]
        await keystore.delete(user_id, action["action_id"].removeprefix("delete_key_"))
        await publish(client, user_id)

    @app.view("save_key")
    async def save_key(ack, body, view, client):
        values = view["state"]["values"]
        provider = values["provider"]["provider_select"]["selected_option"]["value"]
        key = (values["keyblock"]["key_input"]["value"] or "").strip()
        if not key:
            await ack(response_action="errors", errors={"keyblock": "API Key を入力してください。"})
            return
        if problem := await validate_key(provider, key):
            await ack(response_action="errors", errors={"keyblock": problem})
            return
        await ack()
        user_id = body["user"]["id"]
        await keystore.set(user_id, provider, key)
        logger.info("%s key saved for a user", provider)
        await publish(client, user_id)
