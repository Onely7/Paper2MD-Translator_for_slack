"""
OpenAI Responses API client (text + images).

Works with GPT-4.x, the o-series and the reasoning families GPT-5.x / GPT-6.x
(e.g. ``gpt-6.1-sol``, ``gpt-6.1-terra``, ``gpt-6-luna``) and later; the
efforts of the models in ``models.CATALOG`` come from there.  Request
parameters are adapted per model:

* reasoning models (o-series, GPT-5 and later) get ``reasoning.effort``;
  ``temperature`` / ``top_p`` are only sent to classic models,
* effort values a model does not accept (e.g. ``none`` on ``gpt-6.1-sol``)
  are mapped to a supported one, using the "Supported values" list returned by
  the API when needed; what was learned is reused for later requests,
* other parameters rejected with ``400 Unsupported parameter`` are dropped and
  the request is retried without them.

Every response's ``usage`` goes to the ``UsageMeter`` given to the client
(``usage.py`` prices it), and a ``CancelToken`` stops a batch at once: the
requests in flight are dropped and ``Cancelled`` is raised.

Transient errors (connection, 408/409/429/5xx) are retried with exponential
backoff that honours ``Retry-After``.  Authentication errors, missing models
and exhausted quota are raised at once.  ``store`` is always ``False`` so
OpenAI does not keep the responses for later retrieval.

A request is a dict ``{"text": str, "images": [path or URL], "instructions":
str | None, "gen_kwargs": dict | None}``.
"""

from __future__ import annotations

import asyncio
import base64
import email.utils
import logging
import mimetypes
import random
import re
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse

from openai import (
    APIConnectionError,
    APIStatusError,
    AsyncOpenAI,
    BadRequestError,
    NotFoundError,
    RateLimitError,
)

from .cancel import CancelToken, until_cancelled
from .models import CATALOG, coerce_effort, is_reasoning_model, parse_gpt_version
from .usage import UsageMeter

T = TypeVar("T")
logger = logging.getLogger(__name__)

REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}
MAX_OUTPUT_TOKENS_CAP = 128_000


class ModelNotAvailableError(ValueError):
    """The model does not exist or the API key has no access to it."""


class InsufficientQuotaError(RuntimeError):
    """The API key has no remaining credit / quota."""


# ---------------------------------------------------------------------
# Model families
# ---------------------------------------------------------------------
def supports_verbosity(model: str) -> bool:
    """``text.verbosity`` exists since GPT-5."""
    version = parse_gpt_version(model)
    return version is not None and version[0] >= 5 and "chat-latest" not in model.lower()


def normalize_reasoning_effort(model: str, effort: str | None) -> str | None:
    """
    Map an effort value to one the model is documented to accept.

    >>> normalize_reasoning_effort("gpt-6.1-sol", "none")      # Sol/Astra have no "none"
    'low'
    >>> normalize_reasoning_effort("gpt-6.1-terra", "none")
    'none'
    >>> normalize_reasoning_effort("gpt-6-luna", "minimal")     # GPT-6: "minimal" -> "low"
    'low'
    >>> normalize_reasoning_effort("gpt-5-mini", "none")        # GPT-5.0 has "minimal", not "none"
    'minimal'
    """
    if effort is None:
        return None
    if any(spec.id == model for spec in CATALOG):
        return coerce_effort(model, effort)
    name = model.lower()
    version = parse_gpt_version(name)
    if effort == "none":
        if version is None:
            return "low"  # o-series
        if version == (5, 0):
            return "minimal"
        if version[0] >= 6 and ("astra" in name or re.search(r"6\.[1-9]\d*-sol", name)):
            return "low"
    if effort == "minimal" and version is not None and version[0] >= 6:
        return "low"
    return effort


def _closest_effort(desired: str, supported: list[str]) -> str | None:
    known = [s for s in supported if s in REASONING_EFFORTS]
    if not known:
        return None
    if desired not in REASONING_EFFORTS:
        return known[0]
    rank = REASONING_EFFORTS.index(desired)
    # nearest rank; ties go to the cheaper (lower) effort
    return min(known, key=lambda s: (abs(REASONING_EFFORTS.index(s) - rank), REASONING_EFFORTS.index(s)))


# ---------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------
def _error_message(err: Exception) -> str:
    return str(getattr(err, "message", "") or err)


def _supported_values(message: str) -> list[str]:
    """Extract ``'low', 'medium'`` from "... Supported values are: 'low', 'medium', and 'high'."."""
    match = re.search(r"supported values (?:are|is)\s*:?\s*(.*)", message, flags=re.I | re.S)
    return re.findall(r"'([a-z_]+)'", match.group(1)) if match else []


def _is_quota_error(err: RateLimitError) -> bool:
    code = (getattr(err, "code", None) or "").lower()
    return code == "insufficient_quota" or "insufficient_quota" in _error_message(err).lower()


def _retry_after_seconds(err: Exception) -> float | None:
    headers = getattr(getattr(err, "response", None), "headers", None)
    if not headers:
        return None
    try:
        if (value := headers.get("retry-after-ms")) is not None:
            return float(value) / 1000.0
        if (value := headers.get("retry-after")) is None:
            return None
        try:
            return float(value)
        except ValueError:
            return max(0.0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
    except Exception:
        return None


def image_input(image: str | Path, detail: str = "auto") -> dict[str, Any]:
    """Responses API ``input_image`` item for a local file, an http(s) URL or a data URL."""
    image = str(image)
    if image.startswith("data:") or urlparse(image).scheme in {"http", "https"}:
        url = image
    else:
        path = Path(image)
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        url = f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
    return {"type": "input_image", "image_url": url, "detail": detail}


def _drop_nones(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop_nones(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_nones(v) for v in value if v is not None]
    return value


def extract_output_text(response: Any) -> str:
    """Text of a Responses API result (tolerates unknown output item types)."""
    try:
        text = getattr(response, "output_text", None)
    except Exception:
        text = None
    if isinstance(text, str) and text:
        return text
    chunks = []
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", None) in {"output_text", "text"} and isinstance(getattr(part, "text", None), str):
                chunks.append(part.text)
    return "".join(chunks).strip()


class OpenAIClient:
    """Concurrent Responses API requests with per-model parameter handling."""

    # model -> adjustments learned from 400 responses (shared by all instances)
    _learned: dict[str, dict[str, Any]] = {}
    _learned_lock = threading.Lock()

    def __init__(
        self,
        model: str,
        api_key: str | None,
        *,
        max_concurrency: int = 8,
        max_attempts: int = 6,
        first_retry_wait: float = 2.0,
        request_timeout: float = 900.0,
        task: str = "",
        meter: UsageMeter | None = None,
        cancel: CancelToken | None = None,
    ) -> None:
        """``task``: what the requests are for (the key of ``meter``, e.g. "translation")."""
        if not api_key:
            raise ValueError("OpenAI API key is not set.")
        self.model = model
        self.api_key = api_key
        self.task = task
        self.meter = meter
        self.cancel = cancel
        self.max_concurrency = max(1, int(max_concurrency))
        self.max_attempts = max(1, int(max_attempts))
        self.first_retry_wait = float(first_retry_wait)
        self.request_timeout = float(request_timeout)

    # -----------------------------------------------------------------
    # Request parameters
    # -----------------------------------------------------------------
    def build_kwargs(self, gen_kwargs: dict[str, Any] | None) -> tuple[dict[str, Any], str]:
        """Return (Responses API kwargs, image detail) for this model.

        Accepted aliases: ``reasoning_effort`` -> ``reasoning.effort``,
        ``verbosity`` -> ``text.verbosity``, ``image_detail`` (wrapper only).
        """
        kw = _drop_nones(dict(gen_kwargs or {}))
        image_detail = kw.pop("image_detail", "auto")

        if "reasoning_effort" in kw:
            kw["reasoning"] = {**kw.get("reasoning", {}), "effort": kw.pop("reasoning_effort")}
        if "verbosity" in kw:
            kw["text"] = {**kw.get("text", {}), "verbosity": kw.pop("verbosity")}

        reasoning_model = is_reasoning_model(self.model)
        if not reasoning_model:
            kw.pop("reasoning", None)
        elif isinstance(kw.get("reasoning"), dict) and kw["reasoning"].get("effort") is not None:
            kw["reasoning"] = {
                **kw["reasoning"],
                "effort": normalize_reasoning_effort(self.model, kw["reasoning"]["effort"]),
            }
        if not supports_verbosity(self.model) and isinstance(kw.get("text"), dict):
            kw["text"] = {k: v for k, v in kw["text"].items() if k != "verbosity"}
            if not kw["text"]:
                del kw["text"]

        if reasoning_model:
            effort = (kw.get("reasoning") or {}).get("effort")
            if effort != "none":  # sampling parameters are rejected while reasoning
                kw.pop("temperature", None)
                kw.pop("top_p", None)
        else:
            kw.setdefault("temperature", 0.0)
            kw.setdefault("top_p", 1.0)

        # Reasoning tokens count against max_output_tokens.
        kw.setdefault("max_output_tokens", 32768 if reasoning_model else 16384)
        kw["store"] = False
        return kw, image_detail

    def _apply_learned(self, kw: dict[str, Any]) -> dict[str, Any]:
        with self._learned_lock:
            learned = self._learned.get(self.model)
            if not learned:
                return kw
            drop = set(learned["drop"])
            effort_map = dict(learned["effort"])
        kw = {k: v for k, v in kw.items() if k not in drop}
        if "text.verbosity" in drop and isinstance(kw.get("text"), dict):
            kw["text"] = {k: v for k, v in kw["text"].items() if k != "verbosity"}
            if not kw["text"]:
                del kw["text"]
        effort = (kw.get("reasoning") or {}).get("effort")
        if effort in effort_map:
            kw["reasoning"] = {**kw["reasoning"], "effort": effort_map[effort]}
        return kw

    def _learn(self, drop: str | None = None, effort: tuple[str, str] | None = None) -> None:
        with self._learned_lock:
            learned = self._learned.setdefault(self.model, {"drop": set(), "effort": {}})
            if drop:
                learned["drop"].add(drop)
            if effort:
                learned["effort"][effort[0]] = effort[1]

    def _adapt_to_bad_request(self, kw: dict[str, Any], err: BadRequestError) -> dict[str, Any] | None:
        """Request kwargs adapted to a 400 error, or None if it is not about a parameter."""
        param = (getattr(err, "param", None) or "").lower()
        message = _error_message(err)
        lowered = message.lower()

        reasoning = kw.get("reasoning")
        if isinstance(reasoning, dict) and (
            param.startswith("reasoning") or "reasoning.effort" in lowered or "reasoning effort" in lowered
        ):
            effort = reasoning.get("effort")
            new_effort = None
            if effort and "effort" in param + lowered:
                new_effort = _closest_effort(effort, _supported_values(message))
                if new_effort is None:
                    new_effort = {"none": "low", "minimal": "low", "max": "xhigh", "xhigh": "high"}.get(effort)
            if new_effort and new_effort != effort:
                logger.warning("%s does not accept reasoning.effort=%r; using %r", self.model, effort, new_effort)
                self._learn(effort=(effort, new_effort))
                return {**kw, "reasoning": {**reasoning, "effort": new_effort}}
            logger.warning("%s rejected the reasoning parameter; retrying without it", self.model)
            self._learn(drop="reasoning")
            return {k: v for k, v in kw.items() if k != "reasoning"}

        if ("verbosity" in param or "verbosity" in lowered) and isinstance(kw.get("text"), dict):
            logger.warning("%s rejected text.verbosity; retrying without it", self.model)
            self._learn(drop="text.verbosity")
            text_cfg = {k: v for k, v in kw["text"].items() if k != "verbosity"}
            new_kw = {k: v for k, v in kw.items() if k != "text"}
            return {**new_kw, "text": text_cfg} if text_cfg else new_kw

        for key in ("temperature", "top_p", "prompt_cache_key", "service_tier"):
            if key in kw and (param == key or f"'{key}'" in lowered):
                logger.warning("%s rejected %s; retrying without it", self.model, key)
                self._learn(drop=key)
                return {k: v for k, v in kw.items() if k != key}

        if "max_output_tokens" in kw and "max_output_tokens" in param + lowered and kw["max_output_tokens"] > 4096:
            return {**kw, "max_output_tokens": max(4096, kw["max_output_tokens"] // 2)}

        if "detail" in param:
            for item in kw.get("input", []):
                for part in item.get("content", []):
                    if part.get("type") == "input_image" and part.get("detail") != "auto":
                        part["detail"] = "auto"
                        return kw
        return None

    # -----------------------------------------------------------------
    # Calling the API
    # -----------------------------------------------------------------
    def _backoff(self, attempt: int, err: Exception | None = None) -> float:
        retry_after = _retry_after_seconds(err) if err is not None else None
        if retry_after is not None:
            return min(max(retry_after, 0.5), 120.0)
        wait = self.first_retry_wait * 2**attempt
        return min(wait + random.uniform(0.0, 0.25 * wait), 60.0)

    async def _with_retries(self, call: Callable[[], Awaitable[T]]) -> T:
        """Retry transient failures only."""
        for attempt in range(self.max_attempts):
            last = attempt == self.max_attempts - 1
            try:
                return await call()
            except NotFoundError as e:
                raise ModelNotAvailableError(f"{self.model!r} is not available: {_error_message(e)}") from e
            except RateLimitError as e:
                if _is_quota_error(e):
                    raise InsufficientQuotaError(_error_message(e)) from e
                if last:
                    raise
                wait = self._backoff(attempt, e)
                logger.warning("Rate limited (%s); retrying in %.1fs", self.model, wait)
            except APIStatusError as e:
                if e.status_code not in RETRYABLE_STATUS_CODES or last:
                    raise
                wait = self._backoff(attempt, e)
                logger.warning("OpenAI API error %s; retrying in %.1fs", e.status_code, wait)
            except APIConnectionError as e:  # includes APITimeoutError
                if last:
                    raise
                wait = self._backoff(attempt)
                logger.warning("OpenAI connection error (%s); retrying in %.1fs", e, wait)
            await asyncio.sleep(wait)
        raise RuntimeError("unreachable")

    async def _create(self, client: AsyncOpenAI, kw: dict[str, Any]) -> Any:
        """One logical request: parameter adaptation, retries and a larger output budget when cut off."""
        kw = self._apply_learned(kw)
        adaptations = grown = 0
        while True:
            try:
                response = await self._with_retries(lambda kw=kw: client.responses.create(model=self.model, **kw))
                if self.meter is not None:  # every response is billed, also one asked again below
                    self.meter.add(self.task, self.model, getattr(response, "usage", None))
            except BadRequestError as e:
                new_kw = self._adapt_to_bad_request(kw, e) if adaptations < 5 else None
                if new_kw is None:
                    raise
                adaptations += 1
                kw = new_kw
                continue

            reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
            if getattr(response, "status", None) == "incomplete":
                current = int(kw.get("max_output_tokens") or 16384)
                if reason == "max_output_tokens" and grown < 2 and current < MAX_OUTPUT_TOKENS_CAP:
                    grown += 1
                    kw = {**kw, "max_output_tokens": min(current * 2, MAX_OUTPUT_TOKENS_CAP)}
                    logger.warning(
                        "%s hit max_output_tokens=%d; retrying with %d", self.model, current, kw["max_output_tokens"]
                    )
                    continue
                logger.warning("%s response is incomplete (%s)", self.model, reason)
            return response

    def _request_kwargs(self, request: dict[str, Any]) -> dict[str, Any]:
        kw, detail = self.build_kwargs(request.get("gen_kwargs"))
        content: list[dict[str, Any]] = []
        if request.get("text"):
            content.append({"type": "input_text", "text": request["text"]})
        content += [image_input(image, detail) for image in request.get("images") or []]
        kw["input"] = [{"role": "user", "content": content or [{"type": "input_text", "text": ""}]}]
        if request.get("instructions"):
            kw["instructions"] = request["instructions"]
        return kw

    async def agenerate_batch(
        self,
        requests: list[dict[str, Any]],
        on_result: Callable[[int, str | BaseException], None] | None = None,
        return_exceptions: bool = False,
    ) -> list[str | BaseException]:
        semaphore = asyncio.Semaphore(self.max_concurrency)
        async with AsyncOpenAI(api_key=self.api_key, max_retries=0, timeout=self.request_timeout) as client:

            async def one(index: int, request: dict[str, Any]) -> str | BaseException:
                try:
                    kw = self._request_kwargs(request)
                    async with semaphore:
                        result: str | BaseException = extract_output_text(await self._create(client, kw))
                except Exception as e:
                    if not return_exceptions:
                        raise
                    logger.error("OpenAI request %d failed: %s", index, e)
                    result = e
                if on_result is not None:
                    try:
                        on_result(index, result)
                    except Exception:
                        logger.exception("on_result callback failed")
                return result

            if self.cancel is not None:
                self.cancel.check()
            return await until_cancelled(asyncio.gather(*(one(i, r) for i, r in enumerate(requests))), self.cancel)

    # -----------------------------------------------------------------
    # Sync API (called from worker threads)
    # -----------------------------------------------------------------
    @staticmethod
    def _run(coro: Awaitable[T]) -> T:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        raise RuntimeError("Use agenerate_batch() inside a running event loop.")

    def generate_batch(
        self,
        requests: list[dict[str, Any]],
        on_result: Callable[[int, str | BaseException], None] | None = None,
        return_exceptions: bool = False,
    ) -> list[str | BaseException]:
        """Run the requests concurrently.

        Args:
            on_result: called as ``on_result(index, text_or_exception)`` when a
                request finishes (e.g. to advance a progress bar).
            return_exceptions: a failed request yields its exception instead of
                failing the whole batch.
        """
        return self._run(self.agenerate_batch(requests, on_result, return_exceptions))

    def generate(self, request: dict[str, Any]) -> str:
        return self.generate_batch([request])[0]
