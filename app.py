"""
Start the Slack bot.

* Socket Mode (recommended): set SLACK_APP_TOKEN (xapp-...).  The bot connects
  to Slack itself, so no public URL or ngrok is needed.
* HTTP mode: without SLACK_APP_TOKEN, Slack sends events to
  ``http://HOST:PORT/slack/events`` (exposed with ngrok, see scripts/run_ngrok.sh).

Usage: ``scripts/run_app.sh`` (loads .env) or ``uv run python app.py``.
"""

from __future__ import annotations

import asyncio
import logging
import os

from slack_bolt.async_app import AsyncApp

from bot import create_app


def create_http_app(slack_app: AsyncApp):
    from fastapi import FastAPI, Request
    from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler

    handler = AsyncSlackRequestHandler(slack_app)
    api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)  # exposed to the internet via ngrok

    @api.post("/slack/events")
    async def slack_events(request: Request):
        return await handler.handle(request)

    @api.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    return api


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.umask(0o077)  # files written by the bot (cache, keys, temp files) are private
    slack_app = create_app()

    if app_token := os.getenv("SLACK_APP_TOKEN"):
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

        asyncio.run(AsyncSocketModeHandler(slack_app, app_token).start_async())
    else:
        import uvicorn

        uvicorn.run(
            create_http_app(slack_app), host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8888"))
        )


if __name__ == "__main__":
    main()
