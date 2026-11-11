"""
The 中断する button on the first message of a translation.

The job runs on: ``RunningJobs`` keeps its ``CancelToken`` until it ends.  Only
the user who asked for the translation can stop it; anyone else (and a click
after the job ended) gets a note only they can see.  The job stops at its next
check (``paper2md.cancel``); ``RequestHandler`` then removes its files and
posts that it stopped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from paper2md.cancel import CancelToken

from . import messages
from .slack_io import edit

logger = logging.getLogger(__name__)


@dataclass
class Job:
    token: CancelToken
    user_id: str  # who asked for the translation: the only one who can stop it
    channel: str
    message_ts: str | None  # the first message, which carries the button
    text: str  # its text (shown again without the button)


class RunningJobs:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def add(self, job_id: str, job: Job) -> None:
        self._jobs[job_id] = job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def remove(self, job_id: str) -> Job | None:
        return self._jobs.pop(job_id, None)


async def _note(client: AsyncWebClient, channel: str, user: str, thread_ts: str | None, text: str) -> None:
    """A message only ``user`` sees."""
    try:
        await client.chat_postEphemeral(channel=channel, user=user, thread_ts=thread_ts, text=text)
    except SlackApiError as e:
        logger.warning("chat_postEphemeral failed: %s", e.response.get("error"))


async def stop(client: AsyncWebClient, jobs: RunningJobs, job_id: str, user: str, channel: str, thread_ts: str | None):
    """Handle a click on the button: stop the job if ``user`` asked for it."""
    job = jobs.get(job_id)
    if job is None:
        await _note(client, channel, user, thread_ts, messages.CANCEL_TOO_LATE)
        return
    if user != job.user_id:
        await _note(client, channel, user, thread_ts, messages.CANCEL_NOT_ALLOWED)
        return
    if not job.token.cancelled:
        logger.info("Job %s stopped by its requester", job_id)
        job.token.cancel()
        await edit(client, job.channel, job.message_ts, f"{job.text}\n\n{messages.CANCELLING}")


def register(app: AsyncApp, jobs: RunningJobs) -> None:
    @app.action(messages.CANCEL_ACTION)
    async def on_cancel(ack, body, action, client):
        await ack()
        message = body.get("message") or {}
        channel = (body.get("channel") or {}).get("id") or ""
        await stop(
            client,
            jobs,
            action.get("value") or "",
            body["user"]["id"],
            channel,
            message.get("thread_ts") or message.get("ts"),
        )
