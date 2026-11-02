"""Unit tests for pipeline/fetchers/nse_live_quote.py."""

import json
import unittest
from unittest.mock import patch, MagicMock
from pipeline.fetchers.nse_live_quote import NSELiveQuoteFetcher

class TestNSELiveQuote(unittest.TestCase):

    def setUp(self):
        self.fetcher = NSELiveQuoteFetcher()

    def test_mock_scenarios(self):
        # Green light: positive, upper half
        q_green = self.fetcher.fetch_quote("INFY", mock_scenario="green_light")
        self.assertTrue(q_green["is_mock"])
        self.assertGreater(q_green["last_price"], q_green["prev_close"])
        midpoint = (q_green["day_high"] + q_green["day_low"]) / 2.0
        self.assertGreaterEqual(q_green["last_price"], midpoint)

        # Breakout: gap up > 2%
        q_breakout = self.fetcher.fetch_quote("TCS", mock_scenario="breakout")
        gap_pct = ((q_breakout["open"] - q_breakout["prev_close"]) / q_breakout["prev_close"]) * 100.0
        self.assertGreater(gap_pct, 2.0)

        # Red light: negative
        q_red = self.fetcher.fetch_quote("RELIANCE", mock_scenario="red_light")
        self.assertLess(q_red["last_price"], q_red["prev_close"])

    def test_parse_nse_response(self):
        sample_api_json = {
            "priceInfo": {
                "open": 1500.50,
                "intraDayHighLow": {
                    "max": 1525.00,
                    "min": 1495.00
                },
                "lastPrice": 1520.00,
                "previousClose": 1490.00,
                "pChange": 2.01,
                "totalTradedVolume": 500000
            }
        }
        res = self.fetcher._parse_nse_response("TESTSYM", sample_api_json)
        self.assertEqual(res["symbol"], "TESTSYM")
        self.assertEqual(res["open"], 1500.50)
        self.assertEqual(res["day_high"], 1525.00)
        self.assertEqual(res["day_low"], 1495.00)
        self.assertEqual(res["last_price"], 1520.00)
        self.assertEqual(res["prev_close"], 1490.00)
        self.assertEqual(res["p_change"], 2.01)
        self.assertEqual(res["volume"], 500000)
        self.assertFalse(res["is_mock"])

if __name__ == "__main__":
    unittest.main()

