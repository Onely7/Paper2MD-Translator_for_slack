"""Stopping a job: the token, a job waiting in the queue, and OpenAI requests in flight."""

import asyncio
import threading
import unittest
from unittest import mock

import httpx

from bot.jobs import JobQueue
from paper2md import llm
from paper2md.cancel import Cancelled, CancelToken, until_cancelled
from paper2md.llm import OpenAIClient
from paper2md.usage import UsageMeter
from tests.test_llm import MockOpenAI, ok


class TokenTests(unittest.TestCase):
    def test_check_and_wait(self):
        token = CancelToken()
        token.check()  # not stopped: nothing happens
        self.assertEqual(asyncio.run(until_cancelled(asyncio.sleep(0, "done"), token)), "done")
        token.cancel()
        with self.assertRaises(Cancelled):
            token.check()
        self.assertFalse(issubclass(Cancelled, Exception))  # "except Exception" lets it through


class QueueTests(unittest.TestCase):
    def test_a_waiting_job_can_be_stopped(self):
        client = mock.MagicMock(
            chat_postMessage=mock.AsyncMock(return_value={"ts": "1.0"}), chat_update=mock.AsyncMock()
        )
        queue = JobQueue(1)
        token = CancelToken()
        steps = []

        async def first():
            async with queue.slot(client, "C1", "1.0", "a"):
                steps.append("first runs")
                await asyncio.sleep(0.5)

        async def second():
            try:
                async with queue.slot(client, "C1", "1.0", "b", cancel=token):
                    steps.append("second runs")
            except Cancelled:
                steps.append("second stopped")

        async def main():
            running = asyncio.create_task(first())
            await asyncio.sleep(0.05)
            waiting = asyncio.create_task(second())
            await asyncio.sleep(0.05)
            token.cancel()
            await asyncio.gather(running, waiting)
            async with queue.slot(client, "C1", "1.0", "c"):  # the slot is free again
                steps.append("third runs")

        asyncio.run(main())
        self.assertEqual(steps, ["first runs", "second stopped", "third runs"])
        self.assertIn("順番待ちをやめました", str(client.chat_update.await_args))


def usage_ok(text="訳"):
    response = ok(text)
    body = response.json()
    body["usage"] = {
        "input_tokens": 1200,
        "output_tokens": 300,
        "input_tokens_details": {"cached_tokens": 1000},
        "output_tokens_details": {"reasoning_tokens": 100},
        "total_tokens": 1500,
    }
    return httpx.Response(200, json=body)


class OpenAIClientTests(unittest.TestCase):
    def test_usage_is_counted(self):
        meter = UsageMeter()
        with MockOpenAI(lambda n, b: usage_ok()):
            OpenAIClient("gpt-6.1-terra", "sk", task="summary", meter=meter).generate_batch(
                [{"text": "a"}, {"text": "b"}]
            )
        count = meter.counts[("summary", "gpt-6.1-terra")]
        self.assertEqual(
            (count.requests, count.input_tokens, count.cached_tokens, count.output_tokens), (2, 2400, 2000, 600)
        )
        self.assertAlmostEqual(meter.usd, 2 * (200 * 1 + 1000 * 0.05 + 300 * 5) / 1e6)

    def test_requests_in_flight_are_dropped(self):
        async def slow(request):
            await asyncio.sleep(5)
            return usage_ok()

        token = CancelToken()
        threading.Timer(0.3, token.cancel).start()
        transport = httpx.MockTransport(slow)
        with mock.patch.object(
            llm,
            "AsyncOpenAI",
            side_effect=lambda **kw: MockOpenAI.real(**kw, http_client=httpx.AsyncClient(transport=transport)),
        ):
            client = OpenAIClient("gpt-6.1-terra", "sk", cancel=token, meter=UsageMeter())
            with self.assertRaises(Cancelled):
                client.generate_batch([{"text": "a"}, {"text": "b"}])
        with self.assertRaises(Cancelled):  # stopped before: nothing is sent
            client.generate({"text": "c"})


if __name__ == "__main__":
    unittest.main()
