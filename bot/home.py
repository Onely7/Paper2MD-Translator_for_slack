"""Home tab: the user's API keys and the models (with reasoning effort) used for their requests."""

from __future__ import annotations

import logging
import re

import aiohttp
from openai import AsyncOpenAI, AuthenticationError
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from paper2md.config import Settings
from paper2md.models import POLISH_SCOPES, ModelChoice, coerce_effort, model_spec, offered_models

from . import messages
from .keystore import PROVIDERS, KeyStore

logger = logging.getLogger(__name__)

PROVIDER_NAMES = {"openai": "OpenAI", "gyazo": "Gyazo"}
PROVIDER_NOTES = {
    "openai": "必須。翻訳、推敲、要約に使います。",
    "gyazo": "任意。登録すると、Markdown 内の図表が Gyazo の画像 URL になります。",
}
TASKS = {
    "glossary": ("訳語表", "翻訳の前に、論文の主な用語の訳語表を作るのに使います。"),
    "translation": ("翻訳", "本文の翻訳に使います。"),
    "polish": ("推敲", "yomiyasu で訳文の日本語を整えるのに使います。推敲するかどうかは、下のチェックで選べます。"),
    "summary": ("要約", "要約の作成に使います。"),
}
POLISH_SCOPE_OPTIONS = {
    "polish_segments": (
        "ブロックごとに推敲する",
        "翻訳した段落や図表の説明を、一つずつ推敲します。リクエストが多いので、料金が高くなります。",
    ),
    "polish_document": ("Markdown 全体を推敲する", "まとめた論文の全体を推敲します。"),
}
MODEL_NOTE = (
    "括弧内は 100 万トークンあたりの料金（入力 / 出力）です。"
    "選べる Reasoning Effort はモデルによって違います（GPT-6.1 Sol と GPT-6 Astra では none を選べません）。"
    "Reasoning Effort を上げると精度が上がることがありますが、時間と料金が増えます。"
    "推敲のリクエストには毎回 yomiyasu の指示（約 4 万トークン）が付くので、入力のトークンが多くなります。"
)
VALIDATION_TIMEOUT = 2.0  # a modal submission must be acknowledged within 3 seconds
_PREFERENCE_ACTION_RE = re.compile(r"^pref_(glossary|translation|polish|summary)_(model|effort)$")
POLISH_SCOPE_ACTION = "pref_polish_scope"


def _mask(key: str) -> str:
    return f"••••{key[-4:]}" if len(key) > 8 else "••••"


def _option(text: str, value: str) -> dict:
    return {"text": {"type": "plain_text", "text": text}, "value": value}


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


def _key_blocks(keys: dict[str, str | None]) -> list[dict]:
    blocks: list[dict] = [{"type": "header", "text": {"type": "plain_text", "text": "API Key"}}]
    for provider in PROVIDERS:
        key = keys.get(provider)
        status = f"✅ 登録済み（{_mask(key)}）" if key else "❌ 未登録"
        block = {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{PROVIDER_NAMES[provider]}*　{status}\n{PROVIDER_NOTES[provider]}"},
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
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "open_key_modal",
                    "text": {"type": "plain_text", "text": "API Key を登録 / 更新"},
                }
            ],
        }
    )
    return blocks


def _model_actions(task: str, model: str, effort: str | None, settings: Settings) -> dict:
    spec = model_spec(model)
    elements = [
        {
            "type": "static_select",
            "action_id": f"pref_{task}_model",
            "placeholder": {"type": "plain_text", "text": "モデル"},
            "initial_option": _option(spec.label, spec.id),
            "options": [_option(other.label, other.id) for other in offered_models(settings)],
        }
    ]
    if spec.efforts and effort:
        elements.append(
            {
                "type": "static_select",
                "action_id": f"pref_{task}_effort",
                "placeholder": {"type": "plain_text", "text": "Reasoning Effort"},
                "initial_option": _option(f"effort: {effort}", effort),
                "options": [_option(f"effort: {value}", value) for value in spec.efforts],
            }
        )
    # the block id changes with the model, so Slack redraws the effort menu with that model's values
    return {"type": "actions", "block_id": f"pref_{task}_{model}"[:255], "elements": elements}


def _polish_scope_actions(choice: ModelChoice) -> dict:
    """Checkboxes for the two yomiyasu passes (both unchecked: no polishing)."""
    options = {
        name: {
            "text": {"type": "mrkdwn", "text": f"*{label}*"},
            "description": {"type": "mrkdwn", "text": description},
            "value": name,
        }
        for name, (label, description) in POLISH_SCOPE_OPTIONS.items()
    }
    checkboxes = {"type": "checkboxes", "action_id": POLISH_SCOPE_ACTION, "options": list(options.values())}
    selected = [option for name, option in options.items() if getattr(choice, name)]
    if selected:  # Slack rejects an empty initial_options
        checkboxes["initial_options"] = selected
    return {"type": "actions", "block_id": POLISH_SCOPE_ACTION, "elements": [checkboxes]}


def _model_blocks(preferences: dict[str, str], settings: Settings, notice: str | None) -> list[dict]:
    choice = ModelChoice.resolve(preferences, settings)
    blocks: list[dict] = [{"type": "divider"}, {"type": "header", "text": {"type": "plain_text", "text": "モデル"}}]
    for task, (name, note) in TASKS.items():
        if task == "glossary" and not settings.glossary:
            continue  # the server makes no glossary
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{name}*　{note}"}})
        blocks.append(
            _model_actions(task, getattr(choice, f"{task}_model"), getattr(choice, f"{task}_effort"), settings)
        )
        if task == "polish":
            blocks.append(_polish_scope_actions(choice))
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": MODEL_NOTE}]})
    if notice:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"ℹ️ {notice}"}})
    if preferences:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "pref_reset",
                        "text": {"type": "plain_text", "text": "サーバーの既定値に戻す"},
                    }
                ],
            }
        )
    return blocks


def home_view(
    keys: dict[str, str | None],
    preferences: dict[str, str] | None = None,
    settings: Settings | None = None,
    notice: str | None = None,
) -> dict:
    settings = settings or Settings.from_env()
    blocks = _key_blocks(keys) + _model_blocks(preferences or {}, settings, notice)
    blocks += [{"type": "divider"}, {"type": "section", "text": {"type": "mrkdwn", "text": messages.USAGE}}]
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
                "initial_option": _option("OpenAI", "openai"),
                "options": [_option(PROVIDER_NAMES[p], p) for p in PROVIDERS],
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


async def change_preference(keystore: KeyStore, user_id: str, action_id: str, value: str) -> str | None:
    """Save a model or effort picked in the Home tab; return a notice for the user, if any."""
    match = _PREFERENCE_ACTION_RE.match(action_id)
    if not match or not value:
        return None
    task, field = match.groups()
    settings = Settings.from_env()
    choice = ModelChoice.resolve(await keystore.preferences(user_id), settings)
    if field == "model":
        if value not in {spec.id for spec in offered_models(settings)}:
            return None
        current = getattr(choice, f"{task}_effort")
        effort = coerce_effort(value, current or getattr(settings, f"{task}_effort"))
        await keystore.set_preferences(user_id, {f"{task}_model": value, f"{task}_effort": effort})
        if current and effort and effort != current:
            return messages.effort_changed(model_spec(value).name, current, effort)
        return None
    if value in model_spec(getattr(choice, f"{task}_model")).efforts:
        await keystore.set_preferences(user_id, {f"{task}_effort": value})
    return None


async def change_polish_scope(keystore: KeyStore, user_id: str, selected: list[str]) -> None:
    """Save which yomiyasu passes are checked in the Home tab (every pass is saved, checked or not)."""
    await keystore.set_preferences(user_id, {name: "on" if name in selected else "off" for name in POLISH_SCOPES})


def register(app: AsyncApp, keystore: KeyStore) -> None:
    async def publish(client: AsyncWebClient, user_id: str, notice: str | None = None) -> None:
        view = home_view(await keystore.keys_for(user_id), await keystore.preferences(user_id), notice=notice)
        await client.views_publish(user_id=user_id, view=view)

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

    @app.action(_PREFERENCE_ACTION_RE)
    async def pick_model(ack, body, action, client):
        await ack()
        user_id = body["user"]["id"]
        value = (action.get("selected_option") or {}).get("value", "")
        notice = await change_preference(keystore, user_id, action["action_id"], value)
        await publish(client, user_id, notice)

    @app.action(POLISH_SCOPE_ACTION)
    async def pick_polish_scope(ack, body, action, client):
        await ack()
        user_id = body["user"]["id"]
        await change_polish_scope(keystore, user_id, [o["value"] for o in action.get("selected_options") or []])
        await publish(client, user_id)

    @app.action("pref_reset")
    async def reset_models(ack, body, client):
        await ack()
        user_id = body["user"]["id"]
        await keystore.clear_preferences(user_id)
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
