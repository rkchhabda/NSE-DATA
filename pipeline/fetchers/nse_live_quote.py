"""nse_live_quote.py - Fetches live or 15-minute intraday quotes from NSE India.

Extracts:
  - open             : Today's opening price
  - day_high         : Today's high so far (at 09:30 this is H15)
  - day_low          : Today's low so far (at 09:30 this is L15)
  - last_price       : Current Last Traded Price (at 09:30 this is P_930)
  - prev_close       : Yesterday's official closing price
  - p_change         : Percentage change vs previous close
  - vwap             : Volume Weighted Average Price
  - total_volume     : Total traded volume so far

Features:
  - Multi-tier fallback architecture:
      Tier 1: Direct NSE quote API (/api/quote-equity) via standard urllib
      Tier 2: Detailed scrip trade_info endpoint fallback
      Tier 3: Graceful optional third-party library fallback (from nse import NSE) if available
      Tier 4: Built-in deterministic mock engine for tests and dry-runs
  - Persistent session cookie management via http.cookiejar
  - Standard polite headers mimicking modern browser
  - Zero required external dependencies (100% Python standard library by default)

Usage:
    py pipeline\\fetchers\\nse_live_quote.py --symbol INFY
    py pipeline\\fetchers\\nse_live_quote.py --symbol INFY --mock green_light
"""

import argparse
import http.cookiejar
import json
import random
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, Optional

# Graceful optional fallback to third-party nse package if installed in environment
try:
    from nse import NSE as ThirdPartyNSE
except ImportError:
    ThirdPartyNSE = None

NSE_BASE_URL = "https://www.nseindia.com"
NSE_QUOTE_URL = "https://www.nseindia.com/api/quote-equity?symbol={symbol}"
NSE_QUOTE_TRADEINFO_URL = "https://www.nseindia.com/api/quote-equity?symbol={symbol}&section=trade_info"

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
    """Manages session cookies, resilient schema parsing, and requests live quotes from NSE India."""

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
        except Exception:
            self._session_initialized = False

    def fetch_quote(
        self,
        symbol: str,
        mock_scenario: Optional[str] = None,
        base_price: Optional[float] = None,
    ) -> Dict:
        """Fetches live intraday quote for a symbol with multi-tier fallback.

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
        primary_url = NSE_QUOTE_URL.format(symbol=encoded_sym)
        secondary_url = NSE_QUOTE_TRADEINFO_URL.format(symbol=encoded_sym)

        # Tier 1 & Tier 2: Direct HTTP calls via standard library opener
        last_err = None
        for endpoint_url in (primary_url, secondary_url):
            req = urllib.request.Request(endpoint_url, headers=HEADERS)
            for attempt in range(1, self.max_retries + 1):
                try:
                    with self.opener.open(req, timeout=self.timeout) as resp:
                        raw_data = resp.read()
                        data = json.loads(raw_data.decode("utf-8"))
                        parsed = self._parse_nse_response(sym_clean, data)
                        if parsed and parsed.get("last_price", 0) > 0:
                            return parsed
                except urllib.error.HTTPError as e:
                    last_err = e
                    if e.code in (401, 403):
                        self._init_session()
                    time.sleep(1.0 * attempt)
                except Exception as e:
                    last_err = e
                    time.sleep(1.0 * attempt)

        # Tier 3: Optional third-party package fallback (if user has `nse` installed)
        if ThirdPartyNSE is not None:
            try:
                temp_dir = Path(tempfile.gettempdir()) / "nse_cache"
                temp_dir.mkdir(parents=True, exist_ok=True)
                with ThirdPartyNSE(download_folder=temp_dir, use_http2=True) as nse:
                    raw_detailed = nse.get_detailed_scrip_data(sym_clean)
                    parsed = self._parse_nse_response(sym_clean, raw_detailed)
                    if parsed and parsed.get("last_price", 0) > 0:
                        parsed["source_tier"] = "library_fallback"
                        return parsed
            except Exception as e:
                last_err = f"Tier 3 library fallback failed: {e}"

        raise RuntimeError(
            f"Failed to fetch live quote for {sym_clean} after all tiers exhausted: {last_err}"
        )

    def _parse_nse_response(self, symbol: str, data: Dict) -> Dict:
        """Parses NSE API JSON into standardized dictionary.

        Supports both standard /api/quote-equity schemas and nested detailed scrip
        structures (equityResponse, metaData, tradeInfo, securityWiseDP).
        """
        if not data or not isinstance(data, dict):
            raise ValueError(f"Empty or non-dict response for {symbol}")

        # Check for nested equityResponse (detailed scrip format from rkchhabda/NSE-DATA)
        eq_list = data.get("equityResponse", [])
        eq_resp = (eq_list[0] if isinstance(eq_list, list) and len(eq_list) > 0 else {}) or {}
        meta_data = eq_resp.get("metaData") or {}
        trade_info = eq_resp.get("tradeInfo") or {}

        p_info = data.get("priceInfo") or eq_resp.get("priceInfo") or {}
        sec_info = data.get("securityWiseDP") or eq_resp.get("secInfo") or data.get("securityInfo") or {}
        intraday = p_info.get("intraDayHighLow") or {}

        # Last Traded Price
        last_price = float(
            p_info.get("lastPrice")
            or trade_info.get("lastPrice")
            or meta_data.get("closePrice")
            or meta_data.get("lastPrice")
            or 0.0
        )

        # Open Price
        open_p = float(
            p_info.get("open")
            or meta_data.get("open")
            or trade_info.get("open")
            or (last_price if last_price > 0 else 0.0)
        )

        # Day High
        day_high = float(
            intraday.get("max")
            or p_info.get("intradayHigh")
            or meta_data.get("dayHigh")
            or (max(open_p, last_price) if last_price > 0 else 0.0)
        )

        # Day Low
        day_low = float(
            intraday.get("min")
            or p_info.get("intradayLow")
            or meta_data.get("dayLow")
            or (min(open_p, last_price) if last_price > 0 else 0.0)
        )

        # Previous Close
        prev_close = float(
            p_info.get("previousClose")
            or meta_data.get("previousClose")
            or trade_info.get("previousClose")
            or 0.0
        )

        # Percentage Change
        p_change = float(
            p_info.get("pChange")
            or meta_data.get("pChange")
            or (((last_price - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0)
        )

        # VWAP (Volume Weighted Average Price)
        vwap = float(
            p_info.get("vwap")
            or meta_data.get("averagePrice")
            or trade_info.get("vwap")
            or (round((day_high + day_low + 2 * last_price) / 4.0, 2) if last_price > 0 else open_p)
        )

        # Traded Volume
        volume = int(
            sec_info.get("quantityTraded")
            or trade_info.get("totalTradedVolume")
            or trade_info.get("quantitytraded")
            or p_info.get("totalTradedVolume")
            or 0
        )

        # Fallback sanity adjustments
        if day_high == 0.0:
            day_high = max(open_p, last_price)
        if day_low == 0.0:
            day_low = min(open_p, last_price)
        if open_p == 0.0:
            open_p = last_price
        if vwap == 0.0:
            vwap = last_price

        return {
            "symbol": symbol,
            "open": round(open_p, 2),
            "day_high": round(day_high, 2),
            "day_low": round(day_low, 2),
            "last_price": round(last_price, 2),
            "prev_close": round(prev_close, 2),
            "p_change": round(p_change, 2),
            "vwap": round(vwap, 2),
            "volume": volume,
            "is_mock": False,
        }

    @staticmethod
    def _generate_mock_quote(symbol: str, scenario: str, base_price: float = 1000.0) -> Dict:
        """Generates realistic 09:30 mock data for testing or offline dry-runs."""
        scenario = scenario.lower()
        if scenario == "green_light":
            # Modest +0.8% open, holding upper half, trading above VWAP
            prev_close = base_price
            open_p = round(prev_close * 1.008, 2)
            day_low = round(prev_close * 0.998, 2)
            day_high = round(prev_close * 1.015, 2)
            last_price = round(prev_close * 1.013, 2)
            vwap = round(prev_close * 1.009, 2)
        elif scenario == "breakout":
            # Significant +2.5% gap-up, coiling near high of day (+2.8%)
            prev_close = base_price
            open_p = round(prev_close * 1.025, 2)
            day_low = round(prev_close * 1.020, 2)
            day_high = round(prev_close * 1.028, 2)
            last_price = round(prev_close * 1.024, 2)
            vwap = round(prev_close * 1.022, 2)
        elif scenario == "red_light":
            # Opened flat or down, traded down to -1.5%, currently at -1.1% (below VWAP)
            prev_close = base_price
            open_p = round(prev_close * 0.995, 2)
            day_high = round(prev_close * 1.002, 2)
            day_low = round(prev_close * 0.985, 2)
            last_price = round(prev_close * 0.989, 2)
            vwap = round(prev_close * 0.997, 2)
        else:
            prev_close = base_price
            open_p = prev_close
            day_high = prev_close * 1.01
            day_low = prev_close * 0.99
            last_price = prev_close
            vwap = prev_close

        p_change = round(((last_price - prev_close) / prev_close) * 100.0, 2)
        return {
            "symbol": symbol,
            "open": open_p,
            "day_high": day_high,
            "day_low": day_low,
            "last_price": last_price,
            "prev_close": prev_close,
            "p_change": p_change,
            "vwap": vwap,
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
