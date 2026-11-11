"""The cost of OpenAI requests and the exchange rate."""

import unittest
from unittest import mock

from paper2md import usage
from paper2md.usage import UsageMeter, request_cost


class CostTests(unittest.TestCase):
    def test_cache_writes_and_long_requests(self):
        written = {
            "input_tokens": 10_000,
            "output_tokens": 0,
            "input_tokens_details": {
                "cached_tokens": 0,
                "cache_write_tokens": 10_000,
                "cache_write_tokens_details": {"ttl_30m_tokens": 6_000, "ttl_12h_tokens": 4_000},
            },
        }
        # Sol: writes at $2.5 (30 minutes) and $5 (12 hours) per 1M tokens
        self.assertAlmostEqual(request_cost("gpt-6.1-sol", written), (6_000 * 2.5 + 4_000 * 5) / 1e6)
        long = {"input_tokens": 300_000, "output_tokens": 2_000, "input_tokens_details": {"cached_tokens": 100_000}}
        # Luna above 272K input tokens: 2x the input and cache rates, 1.5x the output rate
        self.assertAlmostEqual(
            request_cost("gpt-6-luna", long), ((200_000 * 0.1 + 100_000 * 0.01) * 2 + 2_000 * 0.5 * 1.5) / 1e6
        )

    def test_meter(self):
        meter = UsageMeter()
        meter.add("translation", "gpt-6-luna", {"input_tokens": 200_000, "output_tokens": 0})  # below 272K
        meter.add("translation", "gpt-6-luna", {"input_tokens": 0, "output_tokens": 1_000_000})
        meter.add("summary", "my-model", {"input_tokens": 5, "output_tokens": 5})
        meter.add("summary", "gpt-6-luna", None)  # a response without usage: not counted
        self.assertEqual(meter.requests, 3)
        self.assertAlmostEqual(meter.usd, 0.02 + 0.5)
        self.assertEqual(meter.unpriced_models, ["my-model"])


class RateTests(unittest.TestCase):
    def setUp(self):
        usage._rate_cache = None

    def test_fetched_and_reused(self):
        answer = mock.Mock(json=lambda: {"date": "2026-11-10", "rates": {"JPY": 158.18}})
        with mock.patch.object(usage.requests, "get", return_value=answer) as get:
            self.assertEqual(usage.usd_to_jpy(), (158.18, "2026-11-10"))
            self.assertEqual(usage.usd_to_jpy(), (158.18, "2026-11-10"))
        get.assert_called_once()

    def test_fallback_and_fixed(self):
        with mock.patch.object(usage.requests, "get", side_effect=OSError("offline")):
            self.assertEqual(usage.usd_to_jpy(), (usage.FALLBACK_USD_JPY, ""))
        self.assertEqual(usage.usd_to_jpy(149.5), (149.5, "USD_JPY_RATE"))


if __name__ == "__main__":
    unittest.main()
