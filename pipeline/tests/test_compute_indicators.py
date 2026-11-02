"""Tests for pipeline/tools/compute_indicators.py."""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import compute_indicators as ci


class TestComputeIndicators(unittest.TestCase):
    def test_indicator_computation_and_enrichment(self):
        bars = []
        for i in range(35):
            bars.append({
                "date": f"2026-10-{i+1:02d}",
                "open": 100.0 + i,
                "high": 105.0 + i,
                "low": 98.0 + i,
                "close": 102.0 + i,
                "volume": 10000.0,
            })
        # Set last bar close above prior high
        bars[-1]["close"] = 150.0
        bars[-1]["high"] = 155.0

        ind, hist, extras = ci.compute_stock_indicators(bars)
        self.assertEqual(hist["bars"], 35)
        self.assertTrue(hist["adjusted"])
        self.assertIn("rsi14", ind)
        self.assertIn("macd", ind)
        self.assertIn("atr14", ind)
        self.assertIn("ema20", ind)
        self.assertIn("breakout_20", extras)
        self.assertTrue(extras["breakout_20"]["is_breakout_high"])

    def test_enrich_gate_input(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Write a stock history CSV
            csv_path = os.path.join(tmp_dir, "INFY.csv")
            with open(csv_path, "w", encoding="utf-8") as f:
                f.write("date,open,high,low,close,volume,adj_open,adj_high,adj_low,adj_close,adj_volume,adj_factor\n")
                for i in range(25):
                    f.write(f"2026-10-{i+1:02d},100,105,95,100,1000,100,105,95,100,1000,1.0\n")

            stocks = [{
                "symbol": "INFY",
                "isin": "INE009A01021",
                "trading_date": "2026-10-25",
                "evidence": {"cmp": [{"source": "nse_bhavcopy", "value": 100, "as_of": "2026-10-25"}]},
            }]

            enriched = ci.enrich_gate_input(stocks, tmp_dir)
            self.assertEqual(len(enriched), 1)
            ev = enriched[0]["evidence"]
            self.assertIn("indicators", ev)
            self.assertIn("history", ev)
            self.assertEqual(ev["history"]["bars"], 25)


if __name__ == "__main__":
    unittest.main()
