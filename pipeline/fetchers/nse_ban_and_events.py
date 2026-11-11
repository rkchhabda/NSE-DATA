#!/usr/bin/env python3
"""
nse_ban_and_events.py - Fetches and caches NSE F&O Ban securities and upcoming corporate events (earnings calendar).

Features:
  1. F&O Ban Period Filter:
     - Detects securities where Open Interest has crossed 95% MWPL (Market Wide Position Limit).
     - New positions cannot be initiated; highly prone to extreme volatility and short-squeeze unwinding.
     - Cash entries on banned F&O stocks are blocked.
  2. Pre-Earnings Binary Risk Filter:
     - Fetches corporate filings & event calendar for the next 48 to 72 hours.
     - Identifies scheduled board meetings for Financial Results, Dividends, Buybacks, or Bonus issues.
     - Demotes stocks with imminent earnings from BUY NOW to eliminate binary gap risk.

Usage:
    py pipeline/fetchers/nse_ban_and_events.py --date 2026-11-09
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _to_date_obj(date_str: str) -> datetime:
    """Parses date string in YYYY-MM-DD or DD-MM-YYYY format."""
    for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            pass
    raise ValueError(f"Unsupported date format: {date_str}. Use YYYY-MM-DD or DD-MM-YYYY.")


def fetch_fno_ban_list(date_str: str, cache_dir: Optional[str] = None) -> List[str]:
    """
    Fetches the list of securities currently in the NSE F&O ban period.
    Returns list of uppercase ticker symbols, e.g. ['KAYNES', 'DELTACORP'].
    """
    dt = _to_date_obj(date_str)
    iso_date = dt.strftime("%Y-%m-%d")
    dd_mm_yyyy = dt.strftime("%d-%m-%Y")
    ddmmyyyy = dt.strftime("%d%m%Y")

    if cache_dir is None:
        cache_dir = os.path.join("data", "fo")
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, f"fo_ban_{iso_date}.json")

    # Check cache first
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
                if isinstance(data, dict) and "banned_securities" in data:
                    return data["banned_securities"]
        except Exception as e:
            logger.warning(f"Error reading ban cache {cache_file}: {e}")

    # 1. Attempt using nselib
    banned_securities = []
    try:
        from nselib import derivatives
        raw_list = derivatives.fno_security_in_ban_period(dd_mm_yyyy)
        if raw_list:
            banned_securities = [s.strip().upper() for s in raw_list if s and s.strip()]
    except Exception as e:
        logger.info(f"nselib ban fetch failed for {dd_mm_yyyy} ({e}); attempting direct archive fetch...")
        # 2. Fallback direct CSV fetch
        url = f"https://nsearchives.nseindia.com/archives/fo/sec_ban/fo_secban_{ddmmyyyy}.csv"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 200:
                lines = resp.text.strip().splitlines()
                # format: Sr_No,Security_Name
                for line in lines[1:]:
                    parts = line.split(",")
                    if len(parts) >= 2 and parts[1].strip():
                        banned_securities.append(parts[1].strip().upper())
            elif resp.status_code == 404:
                # No securities in ban or holiday
                banned_securities = []
        except Exception as err:
            logger.warning(f"Direct ban fetch failed: {err}")

    # Write cache
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump({
                "date": iso_date,
                "banned_securities": banned_securities,
                "count": len(banned_securities),
                "updated_at": datetime.now().isoformat(),
            }, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to cache F&O ban list: {e}")

    return banned_securities


def fetch_upcoming_events(
    date_str: str,
    days_ahead: int = 3,
    cache_dir: Optional[str] = None
) -> Dict[str, dict]:
    """
    Fetches corporate events (financial results, board meetings, dividends)
    scheduled in the window [target_date, target_date + days_ahead].
    Returns dict mapping: symbol -> { 'symbol', 'company', 'date', 'purpose', 'bm_desc', 'is_earnings' }
    """
    dt_from = _to_date_obj(date_str)
    dt_to = dt_from + timedelta(days=days_ahead)
    iso_date = dt_from.strftime("%Y-%m-%d")
    dd_from = dt_from.strftime("%d-%m-%Y")
    dd_to = dt_to.strftime("%d-%m-%Y")

    if cache_dir is None:
        cache_dir = os.path.join("data", "calendar")
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, f"earnings_{iso_date}.json")

    # Check cache first
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "events" in data:
                    return data["events"]
        except Exception as e:
            logger.warning(f"Error reading earnings cache {cache_file}: {e}")

    events_map: Dict[str, dict] = {}
    try:
        from nselib import capital_market
        df = capital_market.event_calendar_for_equity(from_date=dd_from, to_date=dd_to)
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                sym = str(row.get("symbol", "")).strip().upper()
                if not sym:
                    continue
                purpose = str(row.get("purpose", "")).strip()
                comp = str(row.get("company", "")).strip()
                ev_date = str(row.get("date", "")).strip()
                bm_desc = str(row.get("bm_desc", "")).strip()

                is_earnings = any(k in purpose.lower() for k in ("result", "financial", "quarter"))
                events_map[sym] = {
                    "symbol": sym,
                    "company": comp,
                    "date": ev_date,
                    "purpose": purpose,
                    "bm_desc": bm_desc,
                    "is_earnings": is_earnings,
                }
    except Exception as e:
        logger.warning(f"Failed to fetch event calendar for {dd_from} to {dd_to}: {e}")

    # Write cache
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump({
                "from_date": iso_date,
                "days_ahead": days_ahead,
                "count": len(events_map),
                "events": events_map,
                "updated_at": datetime.now().isoformat(),
            }, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to cache earnings events: {e}")

    return events_map


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"), help="Target date YYYY-MM-DD")
    parser.add_argument("--days-ahead", type=int, default=3, help="Days ahead to inspect for events")
    args = parser.parse_args(argv)

    print(f"[*] Fetching F&O Ban securities for {args.date}...")
    bans = fetch_fno_ban_list(args.date)
    print(f"    Securities in F&O Ban ({len(bans)}): {bans if bans else 'None'}")

    print(f"[*] Fetching Upcoming Corporate Events / Earnings for next {args.days_ahead} days from {args.date}...")
    events = fetch_upcoming_events(args.date, days_ahead=args.days_ahead)
    fin_results = [ev for ev in events.values() if ev.get("is_earnings")]
    print(f"    Total corporate events: {len(events)}")
    print(f"    Financial Results scheduled: {len(fin_results)}")
    if fin_results:
        sample = fin_results[:5]
        for s in sample:
            print(f"      - {s['symbol']}: {s['date']} | {s['purpose']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
