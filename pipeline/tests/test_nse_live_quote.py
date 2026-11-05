"""Unit tests for pipeline/fetchers/nse_live_quote.py."""

import json
import unittest
from unittest.mock import patch, MagicMock
from pipeline.fetchers.nse_live_quote import NSELiveQuoteFetcher


class TestNSELiveQuote(unittest.TestCase):

    def setUp(self):
        self.fetcher = NSELiveQuoteFetcher()

    def test_mock_scenarios(self):
        # Green light: positive, upper half, above VWAP
        q_green = self.fetcher.fetch_quote("INFY", mock_scenario="green_light")
        self.assertTrue(q_green["is_mock"])
        self.assertGreater(q_green["last_price"], q_green["prev_close"])
        midpoint = (q_green["day_high"] + q_green["day_low"]) / 2.0
        self.assertGreaterEqual(q_green["last_price"], midpoint)
        self.assertIn("vwap", q_green)
        self.assertGreater(q_green["vwap"], 0)

        # Breakout: gap up > 2%
        q_breakout = self.fetcher.fetch_quote("TCS", mock_scenario="breakout")
        gap_pct = ((q_breakout["open"] - q_breakout["prev_close"]) / q_breakout["prev_close"]) * 100.0
        self.assertGreater(gap_pct, 2.0)
        self.assertIn("vwap", q_breakout)

        # Red light: negative
        q_red = self.fetcher.fetch_quote("RELIANCE", mock_scenario="red_light")
        self.assertLess(q_red["last_price"], q_red["prev_close"])
        self.assertIn("vwap", q_red)

    def test_parse_standard_nse_response(self):
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
                "vwap": 1512.45,
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
        self.assertEqual(res["vwap"], 1512.45)
        self.assertEqual(res["volume"], 500000)
        self.assertFalse(res["is_mock"])

    def test_parse_detailed_scrip_response_schema(self):
        # Nested schema format as extracted by rkchhabda/NSE-DATA get_detailed_scrip_data()
        sample_detailed_json = {
            "equityResponse": [
                {
                    "metaData": {
                        "open": 2705.00,
                        "dayHigh": 2755.00,
                        "dayLow": 2690.00,
                        "closePrice": 2740.00,
                        "previousClose": 2695.00,
                        "averagePrice": 2728.50,
                        "pChange": 1.67
                    },
                    "tradeInfo": {
                        "totalTradedVolume": 1250000
                    }
                }
            ]
        }
        res = self.fetcher._parse_nse_response("RELIANCE", sample_detailed_json)
        self.assertEqual(res["symbol"], "RELIANCE")
        self.assertEqual(res["open"], 2705.00)
        self.assertEqual(res["day_high"], 2755.00)
        self.assertEqual(res["day_low"], 2690.00)
        self.assertEqual(res["last_price"], 2740.00)
        self.assertEqual(res["prev_close"], 2695.00)
        self.assertEqual(res["vwap"], 2728.50)
        self.assertEqual(res["volume"], 1250000)

    def test_fallback_when_vwap_missing(self):
        sample_no_vwap = {
            "priceInfo": {
                "open": 100.0,
                "intraDayHighLow": {"max": 105.0, "min": 98.0},
                "lastPrice": 104.0,
                "previousClose": 99.0
            }
        }
        res = self.fetcher._parse_nse_response("FALLBACK", sample_no_vwap)
        self.assertEqual(res["symbol"], "FALLBACK")
        # Default vwap calculation fallback (H + L + 2C) / 4 = (105 + 98 + 208) / 4 = 102.75
        self.assertAlmostEqual(res["vwap"], 102.75, places=2)


if __name__ == "__main__":
    unittest.main()
