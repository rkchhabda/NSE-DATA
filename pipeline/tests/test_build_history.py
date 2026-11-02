"""Tests for pipeline/tools/build_history.py."""
import csv
import json
import os
import sys
import tempfile
import unittest
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import build_history as bh


class TestBuildHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bhav_dir = os.path.join(self.tmp.name, "bhavcopy")
        self.out_dir = os.path.join(self.tmp.name, "history")
        os.makedirs(self.bhav_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def write_bhavcopy(self, dt_str: str, rows: list):
        path = os.path.join(self.bhav_dir, f"{dt_str}.csv")
        cols = ["symbol", "series", "date", "isin", "prev_close", "open", "high", "low",
                "last", "close", "avg_price", "volume", "turnover_lacs", "trades",
                "deliv_qty", "deliv_pct", "usable", "flags"]
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in rows:
                w.writerow(r)

    def test_corporate_action_adjustment(self):
        # Day 1: stock closes at 1000
        # Day 2: 1:2 split, prev_close is 500, closes at 510
        # Day 3: closes at 520
        d1 = ["TEST", "EQ", "2026-10-01", "INE001A01010", "990", "995", "1010", "990", "1000", "1000", "1000", "10000", "100", "500", "5000", "50", "True", ""]
        d2 = ["TEST", "EQ", "2026-10-02", "INE001A01010", "500", "500", "515", "495", "510", "510", "505", "20000", "100", "1000", "10000", "50", "True", "possible_corporate_action"]
        d3 = ["TEST", "EQ", "2026-10-05", "INE001A01010", "510", "512", "525", "510", "520", "520", "518", "22000", "110", "1100", "11000", "50", "True", ""]
        self.write_bhavcopy("2026-10-01", [d1])
        self.write_bhavcopy("2026-10-02", [d2])
        self.write_bhavcopy("2026-10-05", [d3])

        files = bh.find_bhavcopy_files(self.bhav_dir)
        self.assertEqual(len(files), 3)

        history, meta = bh.build_stock_histories(files)
        self.assertIn("TEST", history)
        bars = history["TEST"]
        self.assertEqual(len(bars), 3)

        # Day 1 should be adjusted backwards by factor 0.5 (1000 -> 500)
        self.assertAlmostEqual(bars[0]["adj_close"], 500.0)
        self.assertAlmostEqual(bars[0]["adj_volume"], 20000.0)
        self.assertAlmostEqual(bars[0]["adj_factor"], 0.5)

        # Day 2 and 3 should have factor 1.0
        self.assertAlmostEqual(bars[1]["adj_close"], 510.0)
        self.assertAlmostEqual(bars[1]["adj_factor"], 1.0)
        self.assertAlmostEqual(bars[2]["adj_close"], 520.0)
        self.assertAlmostEqual(bars[2]["adj_factor"], 1.0)

        # Meta
        self.assertEqual(meta["TEST"]["bars"], 3)
        self.assertTrue(meta["TEST"]["adjusted"])
        self.assertEqual(len(meta["TEST"]["corporate_actions"]), 1)
        self.assertAlmostEqual(meta["TEST"]["corporate_actions"][0]["factor"], 0.5)

    def test_gap_calculation(self):
        # 2026-10-01 (Thu) to 2026-10-07 (Wed)
        # Trading days: Thu (1), Fri (2), Mon (5), Tue (6), Wed (7)
        # If dates are only [1, 7], missing = 2, 5, 6 (3 trading days)
        holidays = {date(2026, 10, 2)}  # suppose Oct 2 is holiday
        dates = [date(2026, 10, 1), date(2026, 10, 7)]
        # Missing should be Mon 5, Tue 6 = 2 days
        gaps = bh.compute_gaps(dates, holidays)
        self.assertEqual(gaps, 2)


if __name__ == "__main__":
    unittest.main()

