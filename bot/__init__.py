# """
# Slack AsyncApp を生成して他モジュールに公開するだけのファイル。
# 環境変数:
#     SLACK_BOT_TOKEN
#     SLACK_SIGNING_SECRET
# """

# import os

# from slack_bolt.async_app import AsyncApp

# app = AsyncApp(
#     token=os.environ["SLACK_BOT_TOKEN"],
#     signing_secret=os.environ["SLACK_SIGNING_SECRET"],
# )

# bot/__init__.py
import os

from slack_bolt.async_app import AsyncApp

app = AsyncApp(
    token=os.environ["SLACK_BOT_TOKEN"],
    signing_secret=os.environ["SLACK_SIGNING_SECRET"],
)

# ★ これを追加（順不同でOK・F401で無視）
from . import actions as _actions  # メッセージ等のハンドラ登録
from . import home as _home  # Homeタブ/ボタン/モーダル登録
from . import middleware as _middleware  # ミドルウェア登録
