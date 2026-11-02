"""Unit tests for pipeline/tools/notify.py."""

import json
import unittest
from unittest.mock import patch, MagicMock
from pipeline.tools.notify import (
    format_morning_telegram_message,
    format_morning_email_html,
    send_telegram_alert,
    send_email_alert,
)

class TestNotify(unittest.TestCase):

    def setUp(self):
        self.sample_evaluations = [
            {
                "symbol": "INFY",
                "verdict": "GREEN_LIGHT",
                "action": "BUY LIMIT ORDER (09:30 - 09:35 AM)",
                "p930": 1013.0,
                "change_pct": 1.3,
                "reason": "Healthy controlled open holding upper half.",
                "order": {
                    "order_type": "BUY LIMIT",
                    "limit_price": 1014.0,
                    "stop_loss": 995.0,
                    "target_1": 1050.0,
                    "target_2": 1080.0,
                    "shares": 263,
                    "max_investment": 266682.0,
                }
            },
            {
                "symbol": "TCS",
                "verdict": "RED_LIGHT",
                "action": "SKIP TODAY",
                "p930": 2980.0,
                "change_pct": -0.8,
                "reason": "Trading in the red below yesterday's close.",
                "order": None
            }
        ]

    def test_format_morning_telegram_message(self):
        msg = format_morning_telegram_message(self.sample_evaluations, "2026-10-30")
        self.assertIn("NSE 09:30 AM SWING ENTRY ALERTS", msg)
        self.assertIn("INFY", msg)
        self.assertIn("BUY LIMIT", msg)
        self.assertIn("1014.0", msg)
        self.assertIn("TCS", msg)
        self.assertIn("SKIP TODAY", msg)

    def test_format_morning_email_html(self):
        html = format_morning_email_html(self.sample_evaluations, "2026-10-30", capital=500000.0, risk_pct=1.0)
        self.assertIn("<table", html)
        self.assertIn("INFY", html)
        self.assertIn("BUY LIMIT", html)
        self.assertIn("₹1014.0", html)
        self.assertIn("TCS", html)
        self.assertIn("SKIP TODAY", html)

    def test_missing_credentials_handling(self):
        # Empty tokens should return False gracefully without raising exceptions
        self.assertFalse(send_telegram_alert("", "", "test"))
        self.assertFalse(send_email_alert("smtp.gmail.com", 465, "", "", "", "test", "<p>test</p>"))

    @patch("urllib.request.urlopen")
    def test_send_telegram_alert_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": True}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        res = send_telegram_alert("123456789:test_token", "12345", "test message")
        self.assertTrue(res)

if __name__ == "__main__":
    unittest.main()

