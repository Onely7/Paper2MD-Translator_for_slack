"""OpenAI client: request parameters per model, retries and fallbacks (httpx.MockTransport, no network)."""

import json
import unittest
from unittest import mock

import httpx
import openai

from paper2md import llm
from paper2md.llm import (
    InsufficientQuotaError,
    ModelNotAvailableError,
    OpenAIClient,
    is_reasoning_model,
    normalize_reasoning_effort,
)

RealAsyncOpenAI = openai.AsyncOpenAI


def ok(text="翻訳結果", status="completed", reason=None):
    body = {
        "id": "resp_1",
        "object": "response",
        "created_at": 1,
        "model": "m",
        "status": status,
        "output": [
            {"type": "reasoning", "id": "rs_1", "summary": []},
            {"type": "some_future_item", "id": "x_1"},
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            },
        ],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }
    if reason:
        body["incomplete_details"] = {"reason": reason}
    return httpx.Response(200, json=body)


def error(status, message, param=None, code=None, headers=None):
    body = {"error": {"message": message, "type": "invalid_request_error", "param": param, "code": code}}
    return httpx.Response(status, json=body, headers=headers or {})


class MockOpenAI:
    """Answer the requests of OpenAIClient with ``responder(n, body)``."""

    def __init__(self, responder):
        self.requests = []
        self.responder = responder

    def _handle(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        return self.responder(len(self.requests), body)

    def __enter__(self):
        transport = httpx.MockTransport(self._handle)
        factory = lambda **kw: RealAsyncOpenAI(**kw, http_client=httpx.AsyncClient(transport=transport))  # noqa: E731
        self._patch = mock.patch.object(llm, "AsyncOpenAI", side_effect=factory)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


def client(model):
    return OpenAIClient(model, "sk-test", first_retry_wait=0.01)


class ModelFamilyTests(unittest.TestCase):
    def test_reasoning_detection(self):
        for name in ("gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-astra", "gpt-6-luna", "gpt-5.4-mini", "gpt-7", "o3"):
            self.assertTrue(is_reasoning_model(name), name)
        for name in ("gpt-4.1", "gpt-4o", "gpt-5-chat-latest"):
            self.assertFalse(is_reasoning_model(name), name)

    def test_effort_normalization(self):
        self.assertEqual(normalize_reasoning_effort("gpt-6.1-sol", "none"), "low")
        self.assertEqual(normalize_reasoning_effort("gpt-6-astra", "none"), "low")
        self.assertEqual(normalize_reasoning_effort("gpt-6.1-terra", "none"), "none")
        self.assertEqual(normalize_reasoning_effort("gpt-5-mini", "none"), "minimal")


class BuildKwargsTests(unittest.TestCase):
    def test_gpt61_terra(self):
        kw, detail = client("gpt-6.1-terra").build_kwargs(
            {"reasoning_effort": "none", "verbosity": "high", "image_detail": "high"}
        )
        self.assertEqual((kw["reasoning"], kw["text"], detail), ({"effort": "none"}, {"verbosity": "high"}, "high"))
        self.assertIs(kw["store"], False)
        self.assertEqual(kw["max_output_tokens"], 32768)

    def test_gpt61_sol_has_no_sampling_params(self):
        kw, _ = client("gpt-6.1-sol").build_kwargs({"reasoning_effort": "none", "temperature": 0.0})
        self.assertEqual(kw["reasoning"], {"effort": "low"})
        self.assertNotIn("temperature", kw)

    def test_classic_model(self):
        kw, _ = client("gpt-4.1").build_kwargs({"reasoning_effort": "medium", "verbosity": "high"})
        self.assertNotIn("reasoning", kw)
        self.assertNotIn("text", kw)
        self.assertEqual((kw["temperature"], kw["max_output_tokens"]), (0.0, 16384))


class RequestTests(unittest.TestCase):
    def setUp(self):
        OpenAIClient._learned.clear()

    def test_request_body(self):
        with MockOpenAI(lambda n, b: ok("こんにちは")) as api:
            out = client("gpt-6.1-terra").generate(
                {"text": "translate", "instructions": "rules", "gen_kwargs": {"reasoning_effort": "none"}}
            )
        self.assertEqual(out, "こんにちは")
        body = api.requests[0]
        self.assertEqual(
            (body["model"], body["instructions"], body["reasoning"]), ("gpt-6.1-terra", "rules", {"effort": "none"})
        )
        self.assertEqual(body["input"][0]["content"], [{"type": "input_text", "text": "translate"}])
        self.assertIs(body["store"], False)

    def test_unsupported_effort_is_replaced_and_learned(self):
        def responder(n, body):
            if body["reasoning"]["effort"] == "none":
                return error(
                    400,
                    "Unsupported value: 'reasoning.effort' does not support 'none' with this model. "
                    "Supported values are: 'low', 'medium', 'high', 'xhigh', and 'max'.",
                    param="reasoning.effort",
                )
            return ok()

        model = client("gpt-6.2-nova")  # an unknown future model
        with MockOpenAI(responder) as api:
            model.generate({"text": "x", "gen_kwargs": {"reasoning_effort": "none"}})
            model.generate({"text": "y", "gen_kwargs": {"reasoning_effort": "none"}})
        self.assertEqual([b["reasoning"]["effort"] for b in api.requests], ["none", "low", "low"])

    def test_unsupported_parameter_is_dropped(self):
        def responder(n, body):
            return (
                error(400, "Unsupported parameter: 'text.verbosity'.", param="text.verbosity")
                if "text" in body
                else ok()
            )

        with MockOpenAI(responder) as api:
            client("gpt-6.2-nova").generate({"text": "x", "gen_kwargs": {"verbosity": "high"}})
        self.assertNotIn("text", api.requests[1])

    def test_rate_limit_is_retried(self):
        def responder(n, body):
            return (
                error(429, "Rate limit", code="rate_limit_exceeded", headers={"retry-after-ms": "10"})
                if n == 1
                else ok("ok")
            )

        with MockOpenAI(responder) as api:
            self.assertEqual(client("gpt-6.1-terra").generate({"text": "x"}), "ok")
        self.assertEqual(len(api.requests), 2)

    def test_non_retryable_errors(self):
        cases = [
            (error(429, "You exceeded your current quota", code="insufficient_quota"), InsufficientQuotaError),
            (error(400, "Invalid image", param="input"), openai.BadRequestError),
            (error(401, "Incorrect API key provided"), openai.AuthenticationError),
            (error(404, "The model does not exist", code="model_not_found"), ModelNotAvailableError),
        ]
        for response, exception in cases:
            with MockOpenAI(lambda n, b, r=response: r) as api, self.subTest(exception=exception):
                with self.assertRaises(exception):
                    client("gpt-6.1-terra").generate({"text": "x"})
                self.assertEqual(len(api.requests), 1)

    def test_truncated_answer_gets_more_tokens(self):
        def responder(n, body):
            return ok("", status="incomplete", reason="max_output_tokens") if n == 1 else ok("full")

        with MockOpenAI(responder) as api:
            out = client("gpt-6.1-terra").generate({"text": "x", "gen_kwargs": {"max_output_tokens": 1000}})
        self.assertEqual(out, "full")
        self.assertEqual([b["max_output_tokens"] for b in api.requests], [1000, 2000])

    def test_batch_with_partial_failure(self):
        def responder(n, body):
            text = body["input"][0]["content"][0]["text"]
            return error(400, "Invalid image", param="input") if text == "bad" else ok(text.upper())

        seen = []
        with MockOpenAI(responder):
            results = client("gpt-6.1-terra").generate_batch(
                [{"text": "a"}, {"text": "bad"}, {"text": "c"}],
                on_result=lambda i, r: seen.append(i),
                return_exceptions=True,
            )
        self.assertEqual((results[0], results[2]), ("A", "C"))
        self.assertIsInstance(results[1], openai.BadRequestError)
        self.assertEqual(sorted(seen), [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
