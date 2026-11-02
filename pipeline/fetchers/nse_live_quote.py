"""nse_live_quote.py - Fetches live or 15-minute intraday quotes from NSE India.

Extracts:
  - open             : Today's opening price
  - day_high         : Today's high so far (at 09:30 this is H15)
  - day_low          : Today's low so far (at 09:30 this is L15)
  - last_price       : Current Last Traded Price (at 09:30 this is P_930)
  - prev_close       : Yesterday's official closing price
  - p_change         : Percentage change vs previous close
  - total_volume     : Total traded volume so far

Features:
  - Persistent session cookie management via http.cookiejar
  - Standard polite headers mimicking modern browser
  - Built-in mock / offline mode for reliable testing outside trading hours

Usage:
    py pipeline\fetchers\nse_live_quote.py --symbol INFY
    py pipeline\fetchers\nse_live_quote.py --symbol INFY --mock green_light
"""

import argparse
import http.cookiejar
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional

NSE_BASE_URL = "https://www.nseindia.com"
NSE_QUOTE_URL = "https://www.nseindia.com/api/quote-equity?symbol={symbol}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}


class NSELiveQuoteFetcher:
    """Manages session cookies and requests live quotes from NSE India."""

    def __init__(self, timeout: int = 10, max_retries: int = 3):
        self.timeout = timeout
        self.max_retries = max_retries
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )
        self._session_initialized = False

    def _init_session(self) -> None:
        """Hits NSE home page to establish initial session cookies."""
        req = urllib.request.Request(NSE_BASE_URL, headers=HEADERS)
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                _ = resp.read(1024)
            self._session_initialized = True
        except Exception as e:
            # We don't fail immediately; the API call may still work or retry
            self._session_initialized = False

    def fetch_quote(
        self,
        symbol: str,
        mock_scenario: Optional[str] = None,
        base_price: Optional[float] = None,
    ) -> Dict:
        """Fetches live intraday quote for a symbol.

        If mock_scenario is provided ('green_light', 'breakout', 'red_light'), returns
        realistic simulated data without hitting the network.
        """
        sym_clean = symbol.strip().upper()

        if mock_scenario:
            bp = base_price if (base_price and base_price > 0) else 1000.0
            return self._generate_mock_quote(sym_clean, mock_scenario, base_price=bp)

        if not self._session_initialized:
            self._init_session()
            time.sleep(0.5)

        encoded_sym = urllib.parse.quote(sym_clean)
        url = NSE_QUOTE_URL.format(symbol=encoded_sym)
        req = urllib.request.Request(url, headers=HEADERS)

        last_err = None
        for attempt in range(1, self.max_retries + 1):
            try:
                with self.opener.open(req, timeout=self.timeout) as resp:
                    raw_data = resp.read()
                    data = json.loads(raw_data.decode("utf-8"))
                    return self._parse_nse_response(sym_clean, data)
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 401 or e.code == 403:
                    # Session expired; re-init
                    self._init_session()
                time.sleep(1.0 * attempt)
            except Exception as e:
                last_err = e
                time.sleep(1.0 * attempt)

        raise RuntimeError(f"Failed to fetch live quote for {sym_clean} after {self.max_retries} attempts: {last_err}")

    def _parse_nse_response(self, symbol: str, data: Dict) -> Dict:
        """Parses NSE API JSON into standardized dictionary."""
        p_info = data.get("priceInfo", {})
        sec_info = data.get("securityInfo", {})
        intraday = p_info.get("intraDayHighLow", {})

        open_p = float(p_info.get("open", 0.0) or 0.0)
        day_high = float(intraday.get("max", 0.0) or p_info.get("intraDayHighLow", {}).get("max", 0.0) or 0.0)
        day_low = float(intraday.get("min", 0.0) or p_info.get("intraDayHighLow", {}).get("min", 0.0) or 0.0)
        last_price = float(p_info.get("lastPrice", 0.0) or 0.0)
        prev_close = float(p_info.get("previousClose", 0.0) or 0.0)
        p_change = float(p_info.get("pChange", 0.0) or 0.0)

        # Fallback if intraDayHighLow missing
        if day_high == 0.0:
            day_high = max(open_p, last_price)
        if day_low == 0.0:
            day_low = min(open_p, last_price)

        pre_open_p = float(data.get("preOpenMarket", {}).get("totalTradedValue", 0.0) or 0.0)

        return {
            "symbol": symbol,
            "open": round(open_p, 2),
            "day_high": round(day_high, 2),
            "day_low": round(day_low, 2),
            "last_price": round(last_price, 2),
            "prev_close": round(prev_close, 2),
            "p_change": round(p_change, 2),
            "volume": int(p_info.get("totalTradedVolume", 0) or 0),
            "is_mock": False,
        }

    @staticmethod
    def _generate_mock_quote(symbol: str, scenario: str, base_price: float = 1000.0) -> Dict:
        """Generates realistic 09:30 mock data for testing or offline dry-runs."""
        scenario = scenario.lower()
        if scenario == "green_light":
            # Modest +0.8% open, traded between -0.2% and +1.5%, currently at +1.3% (upper half)
            prev_close = base_price
            open_p = round(prev_close * 1.008, 2)
            day_low = round(prev_close * 0.998, 2)
            day_high = round(prev_close * 1.015, 2)
            last_price = round(prev_close * 1.013, 2)
        elif scenario == "breakout":
            # Significant +2.5% gap-up, coiling near high of day (+2.8%)
            prev_close = base_price
            open_p = round(prev_close * 1.025, 2)
            day_low = round(prev_close * 1.020, 2)
            day_high = round(prev_close * 1.028, 2)
            last_price = round(prev_close * 1.024, 2)
        elif scenario == "red_light":
            # Opened flat or down, traded down to -1.5%, currently at -1.1%
            prev_close = base_price
            open_p = round(prev_close * 0.995, 2)
            day_high = round(prev_close * 1.002, 2)
            day_low = round(prev_close * 0.985, 2)
            last_price = round(prev_close * 0.989, 2)
        else:
            prev_close = base_price
            open_p = prev_close
            day_high = prev_close * 1.01
            day_low = prev_close * 0.99
            last_price = prev_close

        p_change = round(((last_price - prev_close) / prev_close) * 100.0, 2)
        return {
            "symbol": symbol,
            "open": open_p,
            "day_high": day_high,
            "day_low": day_low,
            "last_price": last_price,
            "prev_close": prev_close,
            "p_change": p_change,
            "volume": 250000,
            "is_mock": True,
            "mock_scenario": scenario,
        }


def main():
    parser = argparse.ArgumentParser(description="Fetch live intraday quotes from NSE India")
    parser.add_argument("--symbol", type=str, required=True, help="Stock symbol (e.g. INFY, RELIANCE)")
    parser.add_argument(
        "--mock",
        type=str,
        choices=["green_light", "breakout", "red_light"],
        default=None,
        help="Use simulated 09:30 quote scenario",
    )
    args = parser.parse_args()

    fetcher = NSELiveQuoteFetcher()
    quote = fetcher.fetch_quote(args.symbol, mock_scenario=args.mock)
    print(json.dumps(quote, indent=2))


if __name__ == "__main__":
    main()
