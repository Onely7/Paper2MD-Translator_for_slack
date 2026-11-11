"""Texts the bot posts to Slack (Japanese)."""

from __future__ import annotations

from paper2md.models import TASK_NAMES, TASKS
from paper2md.usage import UsageMeter

USAGE = (
    "*使い方*\n"
    "Bot を招待したチャンネルで、メンション付きで論文を送ってください。PDF の添付、PDF の URL、"
    "arXiv・OpenReview・ACL Anthology・Hugging Face Papers のリンクに対応しています。\n"
    "```@Paper2MD-Translator\nhttps://arxiv.org/abs/0000.00000```\n"
    "*翻訳済みの論文*\n"
    "一度翻訳した論文（arXiv は同じバージョン、PDF は同じファイル）は、保存済みの結果をすぐに返します。"
    "バージョンを指定しない arXiv のリンクは、その時点の最新版を翻訳します。"
    "翻訳し直したいときは `--refresh` を付けてください。\n"
    "*サーバーにデータを残したくない場合*\n"
    "`--no-cache`（または `キャッシュなし`）を付けると、送った PDF・途中のファイル・結果を一時領域だけで扱い、"
    "結果を投稿したあとすべて削除します。\n"
    "```@Paper2MD-Translator --no-cache\nhttps://arxiv.org/abs/0000.00000```\n"
    "*図表の Gyazo へのアップロード*\n"
    "Home タブで Gyazo のアクセストークンを登録すると、図表を Gyazo にアップロードし、"
    "投稿する Markdown の図表を Gyazo の画像 URL にします。アップロードするかどうかは、Home タブのチェックで選べます。\n"
    "*モデル*\n"
    "訳語表、翻訳、推敲（yomiyasu）、要約に使うモデルと Reasoning Effort は、"
    "Paper2MD-Translator の Home タブの「モデル」で選べます。推敲をしないこともできます。\n"
    "*処理の中断*\n"
    "処理を始めるときに投稿するメッセージの「中断する」ボタンで、処理を止められます。押せるのは依頼した人だけです。\n"
    "*API の料金*\n"
    "処理が終わると、スレッドの最後に、かかった OpenAI API の料金を円で投稿します。"
)
WELCOME = (
    "ご招待ありがとうございます！🎉\n"
    "まず添付の PDF に沿って、Paper2MD-Translator の Home タブから OpenAI の API Key を登録してください。\n\n"
)
NO_PAPER = "論文が見つかりませんでした。PDF を添付するか、論文の URL を送ってください。"
NO_OPENAI_KEY = (
    "OpenAI の API Key が未登録です。Paper2MD-Translator の Home タブから登録してから、もう一度送ってください。"
)
NO_CACHE_NOTICE = (
    "🔒 *キャッシュ無効モード（no-cache）* で処理します。"
    "送られた PDF・途中のファイル・結果はサーバーの一時領域だけで扱い、終わったらすべて削除します。"
)
NO_CACHE_DONE = "🧹 サーバー上の一時データ（PDF・途中のファイル・結果）をすべて削除しました。"
NO_CACHE_FAILED = "⚠️ 一時データを削除できたか確認できませんでした。管理者に連絡してください。"
REFRESH_NOTICE = "🔁 `--refresh` が指定されたため、保存済みの結果を使わずに翻訳し直します。"
SAME_PAPER_WAIT = "⏳ 同じ論文をほかのリクエストで翻訳しています。終わりしだい、その結果を返します。"
QUEUE_WAIT = "現在混み合っています（待機 {position} 件 / 同時実行 {limit} 件）。順番が来るまで少しお待ちください…🕒"
QUEUE_START = "✅ 順番が来たので処理を始めます！"
QUEUE_CANCELLED = "⏹ 順番待ちをやめました。"
CANCEL_ACTION = "cancel_job"
CANCELLING = "⏹ 中断しています…"
CANCELLED = "⏹ 処理を中断しました。結果は保存していません。"
CANCEL_NOT_ALLOWED = "中断できるのは、この論文の翻訳を依頼した人だけです。"
CANCEL_TOO_LATE = "この処理はもう終わっているので、中断できません。"
NO_API_COST = "💴 *OpenAI API の料金*: 0 円（保存済みの結果を返したので、OpenAI の API は使っていません）"
NO_API_USED = "💴 *OpenAI API の料金*: 0 円（OpenAI の API を使う前に止まりました）"
SUMMARY_HEADER = "*============================*\n*========　🐾　要約　=========*\n*============================*\n\n"
RESULT_FILES = "*翻訳結果*（ZIP には Markdown と図表画像が入っています）"
FIGURES = "*図表*"


def cache_hit(subject: str, date: str, cached_models: str, current_models: str | None = None) -> str:
    """``current_models`` is given when the user's models differ from the ones of the stored result."""
    text = f"♻️ {subject} {date} に翻訳済みなので、保存済みの結果を返します（{cached_models}）。\n"
    if current_models:
        return text + f"今の設定（{current_models}）で翻訳し直すときは、`--refresh` を付けて送ってください。"
    return text + "翻訳し直すときは、`--refresh` を付けて送ってください。"


def translating_with(lines: list[str]) -> str:
    """
    The models used, one task per line (``ModelChoice.lines()``).

    >>> print(translating_with(["翻訳 `gpt-6-luna`（effort: medium）", "要約 `gpt-6.1-sol`（effort: medium）"]))
    🤖
    <BLANKLINE>
    • 翻訳 `gpt-6-luna`（effort: medium）
    • 要約 `gpt-6.1-sol`（effort: medium）
    <BLANKLINE>
    で処理します。
    """
    items = "\n".join(f"• {line}" for line in lines)
    return f"🤖\n\n{items}\n\nで処理します。"


def with_cancel_button(text: str, job_id: str) -> list[dict]:
    """The first message of a translation, with the 中断する button (``job_id`` names the job)."""
    button = {
        "type": "button",
        "action_id": CANCEL_ACTION,
        "style": "danger",
        "text": {"type": "plain_text", "text": "中断する"},
        "value": job_id,
        "confirm": {
            "title": {"type": "plain_text", "text": "処理の中断"},
            "text": {
                "type": "plain_text",
                "text": "この論文の処理を中断しますか？ ここまでにかかった OpenAI の料金は戻りません。",
            },
            "confirm": {"type": "plain_text", "text": "中断する"},
            "deny": {"type": "plain_text", "text": "続ける"},
        },
    }
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions", "block_id": CANCEL_ACTION, "elements": [button]},
    ]


def _yen(value: float) -> str:
    """
    >>> [_yen(v) for v in (0.042, 3.21, 1234.5)]
    ['0.04', '3.2', '1,234']
    """
    if value < 1:
        return f"{value:.2f}"
    if value < 100:
        return f"{value:.1f}"
    return f"{value:,.0f}"


def _usd(value: float) -> str:
    return f"${value:.4f}" if value < 1 else f"${value:,.2f}"


def api_cost(meter: UsageMeter, rate: float, rate_source: str, complete: bool = True) -> str:
    """
    The OpenAI API cost of a translation, by task and model, in yen.

    ``rate_source``: the date of the reference rate, "USD_JPY_RATE" for a fixed rate, "" for the fallback.

    >>> meter = UsageMeter()
    >>> meter.add("translation", "gpt-6.1-terra", {"input_tokens": 100000, "output_tokens": 10000,
    ...     "input_tokens_details": {"cached_tokens": 40000}})
    >>> print(api_cost(meter, 150.0, "2026-11-10"))
    💴 *OpenAI API の料金*: 約 16.8 円（$0.1120）
    • 翻訳 `gpt-6.1-terra`: 約 16.8 円（1 回、入力 100,000 トークン（うちキャッシュ 40,000）、出力 10,000 トークン）
    1 ドル = 150 円（2026-11-10 の欧州中央銀行の参照レート）で換算しました。
    """
    lines = [f"💴 *OpenAI API の料金*: 約 {_yen(meter.usd * rate)} 円（{_usd(meter.usd)}）"]
    for task in TASKS:
        for (counted_task, model), count in meter.counts.items():
            if counted_task != task:
                continue
            cached = f"（うちキャッシュ {count.cached_tokens:,}）" if count.cached_tokens else ""
            tokens = f"{count.requests} 回、入力 {count.input_tokens:,} トークン{cached}、出力 {count.output_tokens:,} トークン"
            price = f"約 {_yen(count.usd * rate)} 円" if count.priced else "単価不明"
            lines.append(f"• {TASK_NAMES.get(task, task)} `{model}`: {price}（{tokens}）")
    if not complete:
        lines.append(
            "処理が途中で止まったので、止まるまでにかかった料金です。止まったときに処理中だったリクエストの分は含みません。"
        )
    if meter.unpriced_models:
        names = "、".join(f"`{model}`" for model in meter.unpriced_models)
        lines.append(f"{names} は単価が分からないため、料金に含めていません。")
    if rate_source == "USD_JPY_RATE":
        source = "設定した値"
    elif rate_source:
        source = f"{rate_source} の欧州中央銀行の参照レート"
    else:
        source = "為替レートを取得できなかったため、仮の値"
    lines.append(f"1 ドル = {rate:g} 円（{source}）で換算しました。")
    return "\n".join(lines)


def polish_skipped(reason: str) -> str:
    return f"⚠️ yomiyasu による推敲を途中でやめ、推敲していない訳文を使いました（{reason}）。"


def effort_changed(model_name: str, before: str, after: str) -> str:
    return f"{model_name} では Reasoning Effort に {before} を選べないため、{after} にしました。"


def new_version(arxiv_id: str, done: str, latest: int) -> str:
    return f"🆕 arXiv:{arxiv_id} は {done} を翻訳済みですが、最新版の v{latest} が出ているので v{latest} を翻訳します。"


def failed_segments(count: int, no_cache: bool) -> str:
    text = f"⚠️ {count} か所の翻訳に失敗しました。該当箇所には Markdown 内に ⚠️ の注記があります。"
    return text + ("" if no_cache else "この結果はキャッシュしないので、次回は翻訳し直します。")


def gyazo_line(upload: bool) -> str:
    """The line of the first message that says what happens to the figures (for a user with a Gyazo token)."""
    return "図表 Gyazo にアップロードする" if upload else "図表 Gyazo にアップロードしない"


def gyazo_failures(count: int) -> str:
    return f"⚠️ 図表 {count} 枚を Gyazo にアップロードできませんでした（ZIP には入っています）。"


def error(description: str) -> str:
    return f"処理中にエラーが発生しました: {description}"
