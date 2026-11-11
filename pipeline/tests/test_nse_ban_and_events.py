#!/usr/bin/env python3
"""
test_nse_ban_and_events.py - Unit tests for F&O Ban & Pre-Earnings Binary Risk filters.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from pipeline.fetchers.nse_ban_and_events import fetch_fno_ban_list, fetch_upcoming_events
from pipeline.tools.check_morning_entry import evaluate_opening_range
from pipeline.tools.screen_universe import screen_stock


class TestNSEBanAndEvents(unittest.TestCase):

    def test_fetch_fno_ban_cache(self):
        """Verify that F&O ban list is cached and re-read accurately."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = os.path.join(tmpdir, "fo_ban_2026-11-09.json")
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump({"date": "2026-11-09", "banned_securities": ["KAYNES", "DELTACORP"]}, f)

            result = fetch_fno_ban_list("2026-11-09", cache_dir=tmpdir)
            self.assertEqual(result, ["KAYNES", "DELTACORP"])

    def test_fetch_upcoming_events_cache(self):
        """Verify that corporate events are cached and loaded accurately."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = os.path.join(tmpdir, "earnings_2026-11-09.json")
            mock_events = {
                "TCS": {
                    "symbol": "TCS",
                    "company": "Tata Consultancy Services Limited",
                    "date": "10-Nov-2026",
                    "purpose": "Financial Results",
                    "is_earnings": True
                }
            }
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump({"from_date": "2026-11-09", "events": mock_events}, f)

            result = fetch_upcoming_events("2026-11-09", cache_dir=tmpdir)
            self.assertIn("TCS", result)
            self.assertTrue(result["TCS"]["is_earnings"])

    @patch("pipeline.tools.screen_universe.gate_check.evaluate")
    def test_screen_stock_blocks_fno_ban(self, mock_gate):
        """Verify that a stock in the F&O ban is disqualified from BUY NOW."""
        mock_gate.return_value = {
            "status": "BUY NOW",
            "failed_conditions": [],
            "data_confidence": {"score": 90, "band": "HIGH"},
            "validation_status": "PASS",
            "validation": {}
        }
        mock_stock = {
            "symbol": "KAYNES",
            "isin": "INE918Z01012",
            "trading_date": "2026-11-09",
            "evidence": {
                "cmp": [{"source": "nse", "value": 5000.0}, {"source": "bse", "value": 5002.0}],
                "indicators": {
                    "ema20": {"internal": 4800.0},
                    "rsi14": {"internal": 60.0},
                    "macd": {"internal": 12.0},
                    "atr14": {"internal": 120.0},
                },
                "history": {"bars": 265},
            },
            "extras": {
                "delivery_pct": 55.0,
                "breakout_20": {"pct_from_high": 0.5, "is_breakout_high": True},
            }
        }

        # Case 1: Without ban, evaluates to BUY NOW
        res_normal = screen_stock(mock_stock.copy(), regime_score=75, fo_ban_set=set())
        self.assertEqual(res_normal["gate_status"], "BUY NOW")
        
        # Case 2: In F&O Ban -> demoted to WATCH
        res_banned = screen_stock(mock_stock.copy(), regime_score=75, fo_ban_set={"KAYNES"})
        self.assertTrue(res_banned["is_fo_ban"])
        self.assertEqual(res_banned["gate_status"], "WATCH")
        self.assertTrue(any("F&O Ban" in c for c in res_banned["failed_conditions"]))

    @patch("pipeline.tools.screen_universe.gate_check.evaluate")
    def test_screen_stock_demotes_pre_earnings(self, mock_gate):
        """Verify that a stock with imminent quarterly earnings is demoted to WAIT FOR BREAKOUT."""
        mock_gate.return_value = {
            "status": "BUY NOW",
            "failed_conditions": [],
            "data_confidence": {"score": 90, "band": "HIGH"},
            "validation_status": "PASS",
            "validation": {}
        }
        mock_stock = {
            "symbol": "TCS",
            "isin": "INE467B01029",
            "trading_date": "2026-11-09",
            "evidence": {
                "cmp": [{"source": "nse", "value": 4100.0}, {"source": "bse", "value": 4101.0}],
                "indicators": {
                    "ema20": {"internal": 3950.0},
                    "rsi14": {"internal": 62.0},
                    "macd": {"internal": 15.0},
                    "atr14": {"internal": 80.0},
                },
                "history": {"bars": 265},
            },
            "extras": {
                "delivery_pct": 58.0,
                "breakout_20": {"pct_from_high": 0.2, "is_breakout_high": True},
            }
        }

        earnings_map = {
            "TCS": {
                "symbol": "TCS",
                "date": "11-Nov-2026",
                "purpose": "Financial Results/Dividend",
                "is_earnings": True
            }
        }

        res = screen_stock(mock_stock, regime_score=75, earnings_events=earnings_map)
        self.assertIsNotNone(res["upcoming_earnings"])
        self.assertEqual(res["gate_status"], "WAIT FOR BREAKOUT")
        self.assertTrue(any("Pre-Earnings Risk" in c for c in res["failed_conditions"]))

    def test_morning_entry_blocks_banned_or_earnings_stocks(self):
        """Verify that 09:30 AM check issues RED LIGHT for banned or earnings stocks."""
        # 1. Banned stock
        res_ban = evaluate_opening_range(
            symbol="KAYNES",
            prev_close=5000.0,
            open_price=5050.0,
            h15=5080.0,
            l15=5020.0,
            p930=5060.0,
            atr14=120.0,
            base_sl=4820.0,
            target_1=5240.0,
            target_2=5420.0,
            risk_reward="1:2",
            is_fo_ban=True,
        )
        self.assertEqual(res_ban["verdict"], "RED_LIGHT")
        self.assertEqual(res_ban["action"], "SKIP TODAY")
        self.assertIn("F&O ban", res_ban["reason"])

        # 2. Upcoming earnings stock
        res_earn = evaluate_opening_range(
            symbol="INFY",
            prev_close=1850.0,
            open_price=1860.0,
            h15=1870.0,
            l15=1855.0,
            p930=1865.0,
            atr14=35.0,
            base_sl=1797.5,
            target_1=1920.0,
            target_2=1972.5,
            risk_reward="1:2",
            earnings_info={"date": "12-Nov-2026", "purpose": "Financial Results", "is_earnings": True},
        )
        self.assertEqual(res_earn["verdict"], "RED_LIGHT")
        self.assertEqual(res_earn["action"], "SKIP TODAY")
        self.assertIn("Binary Event Risk", res_earn["reason"])


if __name__ == "__main__":
    unittest.main()
