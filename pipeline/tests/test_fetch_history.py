"""Tests for pipeline/tools/fetch_history.py."""
import os
import sys
import unittest
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import fetch_history as fh


class TestFetchHistory(unittest.TestCase):
    def test_generate_trading_days(self):
        # 2026-10-12 (Mon) to 2026-10-18 (Sun)
        # Weekdays: Mon 12, Tue 13, Wed 14, Thu 15, Fri 16 (5 days)
        # Suppose Wed 14 is a holiday
        holidays = {date(2026, 10, 14)}
        days = fh.generate_trading_days(date(2026, 10, 12), date(2026, 10, 18), holidays)
        expected = [date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 15), date(2026, 10, 16)]
        self.assertEqual(days, expected)

    def test_last_n_trading_days(self):
        # Request last 3 trading days up to Friday 2026-10-16
        holidays = set()
        days = fh.last_n_trading_days(3, date(2026, 10, 16), holidays)
        expected = [date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16)]
        self.assertEqual(days, expected)


if __name__ == "__main__":
    unittest.main()

