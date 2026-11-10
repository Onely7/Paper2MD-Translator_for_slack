"""Texts the bot posts to Slack (Japanese)."""

from __future__ import annotations

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
    "*モデル*\n"
    "翻訳と要約に使うモデルと Reasoning Effort は、Paper2MD-Translator の Home タブの「モデル」で選べます。"
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
SUMMARY_HEADER = "*============================*\n*========　🐾　要約　=========*\n*============================*\n\n"
RESULT_FILES = "*翻訳結果*（ZIP には Markdown と図表画像が入っています）"
FIGURES = "*図表*"


def cache_hit(subject: str, date: str, cached_models: str, current_models: str | None = None) -> str:
    """``current_models`` is given when the user's models differ from the ones of the stored result."""
    text = f"♻️ {subject} {date} に翻訳済みなので、保存済みの結果を返します（{cached_models}）。\n"
    if current_models:
        return text + f"今の設定（{current_models}）で翻訳し直すときは、`--refresh` を付けて送ってください。"
    return text + "翻訳し直すときは、`--refresh` を付けて送ってください。"


def translating_with(models: str) -> str:
    return f"🤖 {models} で翻訳します。"


def effort_changed(model_name: str, before: str, after: str) -> str:
    return f"{model_name} では Reasoning Effort に {before} を選べないため、{after} にしました。"


def new_version(arxiv_id: str, done: str, latest: int) -> str:
    return f"🆕 arXiv:{arxiv_id} は {done} を翻訳済みですが、最新版の v{latest} が出ているので v{latest} を翻訳します。"


def failed_segments(count: int, no_cache: bool) -> str:
    text = f"⚠️ {count} か所の翻訳に失敗しました。該当箇所には Markdown 内に ⚠️ の注記があります。"
    return text + ("" if no_cache else "この結果はキャッシュしないので、次回は翻訳し直します。")


def gyazo_failures(count: int) -> str:
    return f"⚠️ 図表 {count} 枚を Gyazo にアップロードできませんでした（ZIP には入っています）。"


def error(description: str) -> str:
    return f"処理中にエラーが発生しました: {description}"
