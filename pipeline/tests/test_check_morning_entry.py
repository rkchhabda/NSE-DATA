"""Unit tests for pipeline/tools/check_morning_entry.py."""

import json
import tempfile
import unittest
from pathlib import Path
from pipeline.tools.check_morning_entry import evaluate_opening_range, run_morning_check

class TestCheckMorningEntry(unittest.TestCase):

    def test_evaluate_opening_range_green_light(self):
        # Controlled +0.8% open, closing at upper 80% of range
        res = evaluate_opening_range(
            symbol="INFY",
            prev_close=1000.0,
            open_price=1008.0,
            h15=1015.0,
            l15=1002.0,
            p930=1013.0,
            atr14=25.0,
            base_sl=962.5,
            target_1=1050.0,
            target_2=1087.5,
            risk_reward="1:1.33",
            capital=500000.0,
            risk_pct=1.0,
        )
        self.assertEqual(res["verdict"], "GREEN_LIGHT")
        self.assertEqual(res["action"], "BUY LIMIT ORDER (09:30 - 09:35 AM)")
        self.assertIsNotNone(res["order"])
        self.assertEqual(res["order"]["order_type"], "BUY LIMIT")
        self.assertLessEqual(res["order"]["risk_amount"], 5000.0)

    def test_evaluate_opening_range_vwap_filter(self):
        # Otherwise Green Light (range_pos = 70%), but price 1007 is below VWAP 1009
        res = evaluate_opening_range(
            symbol="INFY",
            prev_close=1000.0,
            open_price=1005.0,
            h15=1010.0,
            l15=1000.0,
            p930=1007.0,
            atr14=20.0,
            base_sl=970.0,
            target_1=1040.0,
            target_2=1070.0,
            risk_reward="1:1.33",
            vwap=1009.0,
        )
        # Should switch to BREAKOUT_TRIGGER due to trading below VWAP
        self.assertEqual(res["verdict"], "BREAKOUT_TRIGGER")
        self.assertIn("VWAP", res["reason"])
        self.assertEqual(res["order"]["order_type"], "BUY STOP-LIMIT")

    def test_evaluate_opening_range_breakout_trigger(self):
        # Large gap-up of +2.8%
        res = evaluate_opening_range(
            symbol="TCS",
            prev_close=3000.0,
            open_price=3084.0,
            h15=3095.0,
            l15=3075.0,
            p930=3090.0,
            atr14=60.0,
            base_sl=2910.0,
            target_1=3120.0,
            target_2=3210.0,
            risk_reward="1:1.33",
            capital=500000.0,
            risk_pct=1.0,
        )
        self.assertEqual(res["verdict"], "BREAKOUT_TRIGGER")
        self.assertEqual(res["action"], "PLACE BUY STOP-LIMIT ORDER")
        self.assertIsNotNone(res["order"])
        self.assertEqual(res["order"]["order_type"], "BUY STOP-LIMIT")
        self.assertGreater(res["order"]["trigger_price"], 3095.0)
        self.assertGreater(res["order"]["limit_price"], res["order"]["trigger_price"])

    def test_evaluate_opening_range_red_light_negative(self):
        # Negative price vs previous close
        res = evaluate_opening_range(
            symbol="RELIANCE",
            prev_close=1200.0,
            open_price=1202.0,
            h15=1205.0,
            l15=1185.0,
            p930=1190.0,
            atr14=20.0,
            base_sl=1170.0,
            target_1=1240.0,
            target_2=1270.0,
            risk_reward="1:1.33",
        )
        self.assertEqual(res["verdict"], "RED_LIGHT")
        self.assertEqual(res["action"], "SKIP TODAY")
        self.assertIsNone(res["order"])

    def test_evaluate_opening_range_red_light_rejection(self):
        # Rejection from open with long upper wick into bottom 20% of range
        res = evaluate_opening_range(
            symbol="SBIN",
            prev_close=800.0,
            open_price=805.0,
            h15=820.0,
            l15=801.0,
            p930=802.0,
            atr14=15.0,
            base_sl=777.5,
            target_1=830.0,
            target_2=852.5,
            risk_reward="1:1.33",
        )
        self.assertEqual(res["verdict"], "RED_LIGHT")
        self.assertEqual(res["action"], "SKIP TODAY")

    def test_position_sizing_calculation(self):
        # Capital 1,00,000, risk 1% = 1000
        # Entry 100, Stop Loss 90 -> risk per share = 10 -> shares = 100
        res = evaluate_opening_range(
            symbol="TEST",
            prev_close=100.0,
            open_price=100.5,
            h15=101.5,
            l15=100.0,
            p930=101.0,
            atr14=5.0,
            base_sl=90.0,
            target_1=110.0,
            target_2=117.5,
            risk_reward="1:1.33",
            capital=100000.0,
            risk_pct=1.0,
        )
        ord_info = res["order"]
        self.assertIsNotNone(ord_info)
        self.assertLessEqual(ord_info["risk_amount"], 1000.0)

    def test_run_morning_check_mock(self):
        results = run_morning_check(date_str="2026-10-30", mock=True, top_n=3)
        self.assertEqual(len(results), 3)
        self.assertIn(results[0]["verdict"], ["GREEN_LIGHT", "BREAKOUT_TRIGGER", "RED_LIGHT"])

if __name__ == "__main__":
    unittest.main()

