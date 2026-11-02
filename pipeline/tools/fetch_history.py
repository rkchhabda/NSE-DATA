#!/usr/bin/env python3
"""
fetch_history.py - batch historical NSE Bhavcopy fetcher with polite pacing and disk preservation.

Fetches historical daily NSE Bhavcopy files for a date range or the last N trading days.
Skips weekends, holidays (from pipeline/config/holidays.txt), and already-cached dates.
Features:
  - Polite pacing with jitter (default 1.5s)
  - Automatic cleanup of large raw zip/csv files to save disk and avoid OneDrive bloat
  - Automatic history rebuilding via build_history.py

Usage:
    py pipeline\\tools\\fetch_history.py --days 20
    py pipeline\\tools\\fetch_history.py --from 2026-09-01 --to 2026-10-15
"""
import argparse
import os
import shutil
import sys
import time
from datetime import date, timedelta
from typing import List, Set

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "fetchers"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import nse_bhavcopy as nb  # noqa: E402
import build_history as bh  # noqa: E402


def generate_trading_days(start_date: date, end_date: date, holidays: Set[date]) -> List[date]:
    """Return chronological list of trading days between start_date and end_date."""
    days = []
    curr = start_date
    while curr <= end_date:
        if nb.is_trading_day(curr, holidays):
            days.append(curr)
        curr += timedelta(days=1)
    return days


def last_n_trading_days(n: int, end_date: date, holidays: Set[date]) -> List[date]:
    """Return chronological list of the most recent N trading days ending on end_date."""
    days = []
    curr = end_date
    while len(days) < n and curr >= date(2020, 1, 1):
        if nb.is_trading_day(curr, holidays):
            days.append(curr)
        curr -= timedelta(days=1)
    days.reverse()
    return days


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--days", type=int, help="Fetch last N trading days")
    group.add_argument("--from", dest="start", help="Start date (YYYY-MM-DD)")
    ap.add_argument("--to", dest="end", help="End date (YYYY-MM-DD, defaults to latest available)")
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--holidays", default=os.path.join("pipeline", "config", "holidays.txt"))
    ap.add_argument("--pause", type=float, default=1.5, help="Pause between server requests in seconds")
    ap.add_argument("--clean-raw", action="store_true", default=True, help="Remove raw download files after normalization")
    ap.add_argument("--no-clean-raw", dest="clean_raw", action="store_false", help="Keep raw download files")
    ap.add_argument("--rebuild-history", action="store_true", default=True, help="Rebuild data/history after fetch")
    a = ap.parse_args(argv)

    holidays = nb.load_holidays(a.holidays)

    if a.days:
        end_d = nb.parse_date(a.end) if a.end else date(2026, 10, 30)
        target_days = last_n_trading_days(a.days, end_d, holidays)
    else:
        start_d = nb.parse_date(a.start)
        end_d = nb.parse_date(a.end) if a.end else date(2026, 10, 30)
        if end_d < start_d:
            print("Error: --from date must be before --to date")
            return 1
        target_days = generate_trading_days(start_d, end_d, holidays)

    if not target_days:
        print("No trading days in specified range.")
        return 0

    print(f"Targeting {len(target_days)} trading days ({target_days[0]} to {target_days[-1]})...")

    bhavcopy_dir = os.path.join(a.out_dir, "bhavcopy")
    raw_dir = os.path.join(a.out_dir, "raw")
    os.makedirs(bhavcopy_dir, exist_ok=True)

    fetched_count = 0
    cached_count = 0
    skipped_count = 0

    for i, d in enumerate(target_days, 1):
        iso = d.isoformat()
        norm_csv = os.path.join(bhavcopy_dir, f"{iso}.csv")

        if os.path.exists(norm_csv):
            print(f"[{i}/{len(target_days)}] {iso}: CACHED")
            cached_count += 1
            continue

        print(f"[{i}/{len(target_days)}] {iso}: Fetching from NSE...", end=" ", flush=True)
        code, summary = nb.run(d, out_dir=a.out_dir, holidays=holidays, min_rows=1000)

        status = summary.get("status", "FAILED")
        if code == 0 and status == "OK":
            print(f"OK ({summary.get('rows_usable', 0)} stocks)")
            fetched_count += 1

            if a.clean_raw:
                day_raw = os.path.join(raw_dir, iso)
                if os.path.isdir(day_raw):
                    shutil.rmtree(day_raw, ignore_errors=True)
        elif status == "MISSING":
            print(f"NOT PUBLISHED / HOLIDAY")
            skipped_count += 1
        elif status == "NON_TRADING_DAY":
            print(f"HOLIDAY")
            skipped_count += 1
        else:
            msg = "; ".join(summary.get("messages", []))[:100]
            print(f"FAILED ({msg})")
            if any("HTTP 403" in m for m in summary.get("messages", [])):
                print("Encountered HTTP 403 Forbidden. Stopping batch to prevent rate limiting.")
                break

        # Polite pacing
        time.sleep(a.pause)

    print(f"\nBatch Summary: {fetched_count} fetched, {cached_count} cached, {skipped_count} missing/holiday.")

    if a.rebuild_history and (fetched_count > 0 or cached_count > 0):
        print("\nRebuilding historical series...")
        bhav_files = bh.find_bhavcopy_files(bhavcopy_dir)
        history_dir = os.path.join(a.out_dir, "history")
        histories, meta = bh.build_stock_histories(bhav_files)
        bh.write_history_files(histories, meta, history_dir, holidays)
        print(f"Rebuilt history for {len(histories)} stocks in {history_dir} (total dates: {len(bhav_files)})")

    return 0


if __name__ == "__main__":
    sys.exit(main())

