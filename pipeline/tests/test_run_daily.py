"""Unit tests for pipeline/tools/run_daily.py."""

import json
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
from pipeline.tools.run_daily import is_pure_equity, get_latest_available_date

class TestRunDaily(unittest.TestCase):

    def test_is_pure_equity(self):
        self.assertTrue(is_pure_equity("INFY"))
        self.assertTrue(is_pure_equity("RELIANCE"))
        self.assertTrue(is_pure_equity("TATACONSUM"))
        self.assertTrue(is_pure_equity("ETERNAL"))

        # ETFs and non-equities
        self.assertFalse(is_pure_equity("NIFTYBEES"))
        self.assertFalse(is_pure_equity("ITBEES"))
        self.assertFalse(is_pure_equity("SETFGOLD"))
        self.assertFalse(is_pure_equity("LIQUIDBEES"))
        self.assertFalse(is_pure_equity("HDFCNIFETF"))
        self.assertFalse(is_pure_equity("EMBASSY_REIT"))

    def test_get_latest_available_date(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir)
            (p / "2026-10-28.csv").touch()
            (p / "2026-10-30.csv").touch()
            (p / "2026-10-29.csv").touch()
            (p / "invalid.csv").touch()

            latest = get_latest_available_date(p)
            self.assertEqual(latest, "2026-10-30")

    @patch("pipeline.tools.check_morning_entry.run_morning_check")
    def test_run_daily_morning_dispatch(self, mock_morning):
        from pipeline.tools.run_daily import main
        mock_morning.return_value = []
        with patch("sys.argv", ["run_daily.py", "--morning", "--top", "3"]):
            main()
        mock_morning.assert_called_once()

if __name__ == "__main__":
    unittest.main()

