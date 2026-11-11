"""
What the OpenAI requests for one paper cost.

``UsageMeter`` adds up the ``usage`` of every response (``OpenAIClient(meter=...)``),
by task and model, and prices each response with ``models.Prices`` as OpenAI
bills it (prompt-caching guide and model pages, November 2026):

* ``input_tokens`` holds ordinary input, ``input_tokens_details.cached_tokens``
  (read from the prompt cache) and ``input_tokens_details.cache_write_tokens``
  (written to it); each is priced at its own rate.  30-minute writes cost 1.25x
  the input rate (``Prices.cache_write``), 12-hour writes 2.5x.
* ``output_tokens`` includes the reasoning tokens.
* A request with more than 272K input tokens is priced at 2x the input and
  cache rates and 1.5x the output rate as a whole.

Responses that were cut off and asked again (a larger output budget, a
rejected parameter) are counted as well, since they are billed.  Requests
dropped by ``Cancelled`` have no usage and are not counted.

``usd_to_jpy`` gives the yen per dollar: ``USD_JPY_RATE`` when set, else the
ECB reference rate of the day from frankfurter.dev, else ``FALLBACK_USD_JPY``.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

from .models import model_spec

logger = logging.getLogger(__name__)

LONG_CONTEXT_TOKENS = 272_000
LONG_CONTEXT_INPUT_RATE, LONG_CONTEXT_OUTPUT_RATE = 2.0, 1.5
RATE_URL = "https://api.frankfurter.dev/v1/latest?base=USD&symbols=JPY"
FALLBACK_USD_JPY = 158.0  # used when the rate cannot be fetched (the ECB rate in November 2026)
RATE_TTL = 6 * 3600  # seconds a fetched rate is reused


def _field(data: Any, *path: str) -> int:
    """An integer in a nested dict / SDK object, 0 when missing."""
    for key in path:
        if data is None:
            return 0
        data = data.get(key) if isinstance(data, dict) else getattr(data, key, None)
    return int(data or 0)


def _as_dict(usage: Any) -> Any:
    """SDK objects as plain data (keeps fields the SDK does not know yet, such as cache writes)."""
    if hasattr(usage, "model_dump"):
        try:
            return usage.model_dump()
        except Exception:
            return usage
    return usage


@dataclass
class TokenCount:
    requests: int = 0
    input_tokens: int = 0  # all input: ordinary, cached and written to the cache
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0  # reasoning included
    usd: float = 0.0
    priced: bool = True  # False: the model has no known prices (usd is 0)


def request_cost(model: str, usage: Any) -> float | None:
    """
    The USD cost of one response; None for a model without known prices.

    >>> round(request_cost("gpt-6.1-terra", {"input_tokens": 12000, "output_tokens": 1000,
    ...     "input_tokens_details": {"cached_tokens": 4000, "cache_write_tokens": 7000}}), 6)
    0.01495
    >>> round(request_cost("gpt-6.1-terra", {"input_tokens": 300000, "output_tokens": 1000}), 6)
    0.6075
    >>> request_cost("my-model", {"input_tokens": 10}) is None
    True
    """
    prices = model_spec(model).prices
    if prices is None:
        return None
    usage = _as_dict(usage)
    total = _field(usage, "input_tokens")
    cached = _field(usage, "input_tokens_details", "cached_tokens")
    written = _field(usage, "input_tokens_details", "cache_write_tokens")
    written_12h = _field(usage, "input_tokens_details", "cache_write_tokens_details", "ttl_12h_tokens")
    written_30m = written - written_12h
    ordinary = max(total - cached - written, 0)
    output = _field(usage, "output_tokens")
    long = total > LONG_CONTEXT_TOKENS
    input_rate = LONG_CONTEXT_INPUT_RATE if long else 1.0
    output_rate = LONG_CONTEXT_OUTPUT_RATE if long else 1.0
    input_usd = (
        ordinary * prices.input
        + cached * prices.cached_input
        + written_30m * prices.cache_write
        + written_12h * prices.cache_write * 2  # 12-hour writes: 2.5x the input rate
    )
    return (input_usd * input_rate + output * prices.output * output_rate) / 1_000_000


class UsageMeter:
    """Token counts and cost of the requests of one paper, by (task, model)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counts: dict[tuple[str, str], TokenCount] = {}

    def add(self, task: str, model: str, usage: Any) -> None:
        if usage is None:
            return
        data = _as_dict(usage)
        cost = request_cost(model, data)
        with self._lock:
            count = self.counts.setdefault((task, model), TokenCount())
            count.requests += 1
            count.input_tokens += _field(data, "input_tokens")
            count.cached_tokens += _field(data, "input_tokens_details", "cached_tokens")
            count.cache_write_tokens += _field(data, "input_tokens_details", "cache_write_tokens")
            count.output_tokens += _field(data, "output_tokens")
            if cost is None:
                count.priced = False
            else:
                count.usd += cost

    @property
    def requests(self) -> int:
        return sum(count.requests for count in self.counts.values())

    @property
    def usd(self) -> float:
        return sum(count.usd for count in self.counts.values())

    @property
    def unpriced_models(self) -> list[str]:
        return sorted({model for (_, model), count in self.counts.items() if not count.priced})


_rate_lock = threading.Lock()
_rate_cache: tuple[float, float, str] | None = None  # (fetched at, rate, date of the rate)


def usd_to_jpy(fixed: float | None = None, timeout: float = 3.0) -> tuple[float, str]:
    """
    (Yen per dollar, where the rate comes from).

    >>> usd_to_jpy(150.0)
    (150.0, 'USD_JPY_RATE')
    """
    global _rate_cache
    if fixed:
        return float(fixed), "USD_JPY_RATE"
    with _rate_lock:
        if _rate_cache and time.time() - _rate_cache[0] < RATE_TTL:
            return _rate_cache[1], _rate_cache[2]
        try:
            response = requests.get(RATE_URL, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            rate = float(data["rates"]["JPY"])
            _rate_cache = (time.time(), rate, str(data.get("date") or ""))
            return rate, _rate_cache[2]
        except Exception as e:
            logger.warning("Could not fetch the USD/JPY rate: %s", e)
            return FALLBACK_USD_JPY, ""
