"""Tests for pipeline/tools/screen_universe.py."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import screen_universe as su


class TestScreenUniverse(unittest.TestCase):
    def test_market_regime_calculation(self):
        stocks = [
            {"evidence": {"cmp": [{"value": 110}], "indicators": {"ema20": {"internal": 100}}}},
            {"evidence": {"cmp": [{"value": 90}], "indicators": {"ema20": {"internal": 100}}}},
            {"evidence": {"cmp": [{"value": 120}], "indicators": {"ema20": {"internal": 100}}}},
        ]
        # 2 out of 3 above ema20 = 66.7%
        regime, info = su.calculate_market_regime(stocks)
        self.assertAlmostEqual(info["breadth_pct"], 66.7, places=1)
        self.assertGreaterEqual(regime, 65)

    def test_trade_setup_generation(self):
        setup = su.generate_trade_setup(1000.0, 20.0, {})
        self.assertEqual(setup["cmp"], 1000.0)
        self.assertEqual(setup["stop_loss"], 970.0)    # 1000 - 1.5 * 20
        self.assertEqual(setup["target_1"], 1040.0)    # 1000 + 2.0 * 20
        self.assertEqual(setup["target_2"], 1070.0)    # 1000 + 3.5 * 20
        self.assertEqual(setup["risk_reward_t1"], "1:1.33")


if __name__ == "__main__":
    unittest.main()

