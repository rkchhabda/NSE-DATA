#!/usr/bin/env python3
"""
history_backfill.py - build the compact NSE daily price history used for indicators.

For each weekday in the range it asks NSE for that day's Bhavcopy (through the same validated
fetcher as everything else), keeps only the columns indicators need (about 60 KB gzipped per day),
and DISCARDS the raw downloads, so a year of history is roughly 20 MB, not 300 MB.

  - fetched file passes every check  -> the day is recorded "open" in data/history/calendar.json
  - NSE has no new file for a past day (404, a web page, or the previous day's file: a holiday)
                                      -> recorded "closed"
  - anything else (failure, today's file not yet out) -> left UNKNOWN, reported, nothing invented

It resumes where it stopped (known days are skipped), pauses between downloads, and stops at once
on an HTTP 403. Needs the ISIN, so both NSE files must be available for a day.

Usage (from the workspace root):
    py pipeline\\tools\\history_backfill.py                    # about 400 calendar days up to the latest session
    py pipeline\\tools\\history_backfill.py --from 2025-09-01 --to 2026-10-30
"""
import argparse
import csv
import os
import sys
import tempfile
import time
from datetime import date, timedelta

ROOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT_DIR, "fetchers"))
sys.path.insert(0, ROOT_DIR)
import nse_bhavcopy as nb  # noqa: E402
from analytics import history as hs  # noqa: E402

NUM = ("open", "high", "low", "close", "prev_close", "volume")


def weekdays(start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def fetch_day(d, hist_dir, cal, pause, sleep, now, min_rows, today):
    """Fetch one day into the store. Returns (status, note); status in
    fetched | closed | not yet | FAILED | BLOCKED."""
    iso = d.isoformat()
    sleep(pause)
    with tempfile.TemporaryDirectory() as tmp:       # raw files live only here and vanish afterwards
        code, s = nb.run(d, out_dir=tmp, min_rows=min_rows, holidays=set(), now=now, sleep=sleep)
        msgs = "; ".join(s.get("messages", []))
        if "HTTP 403" in msgs:
            return "BLOCKED", ""
        if s["status"] == "OK":
            rows = []
            with open(os.path.join(tmp, "bhavcopy", f"{iso}.csv"), encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    if r["usable"] != "True" or r["series"] != "EQ" or not r["isin"]:
                        continue
                    try:
                        rows.append({"isin": r["isin"], "symbol": r["symbol"], **{k: float(r[k]) for k in NUM}})
                    except ValueError:
                        continue                      # a blank price: never filled in
            if not rows:
                return "FAILED", "no usable rows with an ISIN (is the UDiFF file available?)"
            hs.write_day(hist_dir, iso, rows)
            cal[iso] = "open"
            return "fetched", f"{len(rows)} stocks"
        if s["status"] == "MISSING":
            if d < today:
                cal[iso] = "closed"
                return "closed", "no new file from NSE (holiday)"
            return "not yet", "today's file is not published yet"
        return "FAILED", msgs[:130]


def main(argv=None, sleep=time.sleep, now=nb.now_ist):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--days", type=int, default=400, help="calendar days back from --to (default 400)")
    ap.add_argument("--hist-dir", default=os.path.join("data", "history"))
    ap.add_argument("--min-rows", type=int, default=1000)
    ap.add_argument("--pause", type=float, default=2.0)
    ap.add_argument("--refresh", action="store_true", help="re-fetch days already in the calendar")
    a = ap.parse_args(argv)

    today = now().date()
    end = nb.parse_date(a.end) if a.end else nb.default_trading_date(now(), set())
    start = nb.parse_date(a.start) if a.start else end - timedelta(days=a.days)
    if end < start or (end - start).days > 800:
        print("Choose a range of at most 800 days, with --from before --to.")
        return 1
    cal = hs.load_calendar(a.hist_dir)
    todo = [d for d in weekdays(start, end) if a.refresh or d.isoformat() not in cal]
    allw = sum(1 for _ in weekdays(start, end))
    print(f"{start} to {end}: {allw} weekdays, {allw - len(todo)} already known, {len(todo)} to fetch "
          f"(about {len(todo) * (a.pause + 2) / 60:.0f} min).\n")
    counts = {}
    for d in todo:
        status, note = fetch_day(d, a.hist_dir, cal, a.pause, sleep, now, a.min_rows, today)
        counts[status] = counts.get(status, 0) + 1
        if status != "BLOCKED":
            hs.save_calendar(a.hist_dir, cal)
        print(f"{d.isoformat()}  {status:<10}{note}")
        if status == "BLOCKED":
            print("\nStopped: NSE answered HTTP 403 (refusing automated access). Not retrying. "
                  "Run again later; days already stored are kept.")
            return 3
    opened = hs.open_days(cal, end.isoformat())
    unknown = [d.isoformat() for d in weekdays(start, end) if d.isoformat() not in cal]
    print(f"\nThis run: " + (", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "nothing to do"))
    print(f"History now holds {len(opened)} open trading days up to {end} "
          f"({sum(1 for d, v in cal.items() if v == 'closed' and start.isoformat() <= d <= end.isoformat())} closed in range).")
    if unknown:
        print(f"Unknown weekdays (not fetched, or failed): {len(unknown)}"
              + (f", e.g. {', '.join(unknown[:5])}" if unknown else "") + ". Each is a gap: run again to retry.")
    need = 260
    print(f"Indicators need at least {need} bars: " + ("enough days stored." if len(opened) >= need else
          f"{need - len(opened)} more open days needed (extend --from further back)."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
