#!/usr/bin/env python3
"""
backfill_compare.py - fetch several days of NSE and BSE Bhavcopy and compare closes day by day.

For each weekday in the range it fetches (if not already on disk) the NSE and BSE files, then
compares the closes of stocks matched by ISIN, using the same rules as the gate (BSE needs at
least --min-trades trades; the gap is judged against --tolerance percent). It prints one line per
day, then pooled results: how stable the agreement is from day to day, and which stocks conflict
persistently. This is how the cross-exchange tolerance should be judged: over many days.

It stops at once if either exchange answers HTTP 403, and pauses between downloads.

Usage (from the workspace root):
    py pipeline\\tools\\backfill_compare.py --from 2026-10-19 --to 2026-10-30
"""
import argparse
import os
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from statistics import median

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fetchers"))
import bse_bhavcopy as bb  # noqa: E402
import compare_exchanges as ce  # noqa: E402
import nse_bhavcopy as nb  # noqa: E402


def weekdays(start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def _blocked(summary):
    return any("HTTP 403" in m for m in summary.get("messages", []))


def fetch_day(d, out_dir, holidays, refresh, pause, sleep, now, min_rows=1000):
    """Returns (info, blocked). Fetches NSE then BSE unless the normalised file already exists."""
    iso = d.isoformat()
    info = {"date": iso, "nse": "", "bse": "", "res": None}
    if not nb.is_trading_day(d, holidays):
        info["nse"] = info["bse"] = "holiday"
        return info, False
    nse_csv = os.path.join(out_dir, "bhavcopy", f"{iso}.csv")
    bse_csv = os.path.join(out_dir, "bse", f"{iso}.csv")
    for key, path, runner in (("nse", nse_csv, lambda: nb.run(d, out_dir=out_dir, holidays=holidays, min_rows=min_rows, refresh=refresh, now=now, sleep=sleep)),
                              ("bse", bse_csv, lambda: bb.run(d, out_dir=out_dir, holidays=holidays, min_rows=min_rows, refresh=refresh, now=now, sleep=sleep))):
        if os.path.exists(path) and not refresh:
            info[key] = "cached"
            continue
        sleep(pause)
        code, s = runner()
        info[key] = {"OK": "fetched", "MISSING": "not published", "NON_TRADING_DAY": "holiday"}.get(s["status"], "FAILED")
        if _blocked(s):
            info[key] = "BLOCKED"
            return info, True
        if info[key] == "FAILED":
            info["note"] = (info.get("note", "") + f" {key.upper()}: " + "; ".join(s["messages"])[:110]).strip()
    if all(os.path.exists(p) for p in (nse_csv, bse_csv)):
        info["res"] = ce.compare(ce.load(nse_csv, True), ce.load(bse_csv, False))
    return info, False


def day_stats(res, min_trades, tol):
    active = [r for r in res if r["bse_trades"] >= min_trades]
    if not active:
        return None
    diffs = sorted(r["diff_pct"] for r in active)
    n = len(diffs)
    share = lambda t: sum(x <= t for x in diffs) / n * 100.0
    return {"matched": len(res), "active": n, "w25": share(0.25), "w_tol": share(tol), "w100": share(1.0),
            "conflicts": sum(x > tol for x in diffs), "median": ce.percentile(diffs, 50)}


def main(argv=None, sleep=time.sleep, now=nb.now_ist):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--min-trades", type=int, default=100)
    ap.add_argument("--tolerance", type=float, default=0.50)
    ap.add_argument("--pause", type=float, default=2.0, help="seconds before each download")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--min-rows", type=int, default=1000, help="minimum rows for a file to be trusted")
    ap.add_argument("--holidays", default=os.path.join("pipeline", "config", "holidays.txt"))
    a = ap.parse_args(argv)

    start, end = nb.parse_date(a.start), nb.parse_date(a.end)
    if end < start or (end - start).days > 45:
        print("Choose a range of at most 45 days, with --from before --to.")
        return 1
    holidays = nb.load_holidays(a.holidays)
    print(f"{'date':<12}{'NSE':<15}{'BSE':<15}{'matched':>8}{'active':>8}{'<=0.25%':>9}{'<=' + format(a.tolerance, '.2f') + '%':>9}"
          f"{'<=1.00%':>9}{'conflicts':>10}{'median%':>9}")
    days, hist = [], defaultdict(list)
    for d in weekdays(start, end):
        info, blocked = fetch_day(d, a.out_dir, holidays, a.refresh, a.pause, sleep, now, a.min_rows)
        st = day_stats(info["res"], a.min_trades, a.tolerance) if info["res"] else None
        line = f"{info['date']:<12}{info['nse']:<15}{info['bse']:<15}"
        if st:
            line += (f"{st['matched']:>8}{st['active']:>8}{st['w25']:>8.1f}%{st['w_tol']:>8.1f}%{st['w100']:>8.1f}%"
                     f"{st['conflicts']:>10}{st['median']:>9.3f}")
            days.append(st)
            for r in info["res"]:
                if r["bse_trades"] >= a.min_trades:
                    hist[r["isin"]].append((r["symbol"], r["diff_pct"]))
        elif info.get("note"):
            line += "   " + info["note"]
        print(line)
        if blocked:
            print("\nStopped: the server answered HTTP 403 (refusing automated access). Not retrying. "
                  "Download the file by hand and use --file / --delivery-file, or try again later.")
            return 3
    if not days:
        print("\nNo day had comparable data (check the statuses above).")
        return 1

    shares = [d["w_tol"] for d in days]
    total = sum(d["active"] for d in days)
    pooled = sum(d["w_tol"] * d["active"] for d in days) / total
    print(f"\nDays compared: {len(days)} | stock-days with >= {a.min_trades} BSE trades: {total}")
    print(f"Share agreeing within {a.tolerance:.2f}%: pooled {pooled:.1f}% | day-to-day range {min(shares):.1f}% to {max(shares):.1f}%"
          f" | median daily gap {median(d['median'] for d in days):.3f}%")
    multi = {i: v for i, v in hist.items() if len(v) >= 3}
    persistent = {i: v for i, v in multi.items() if sum(x > a.tolerance for _, x in v) / len(v) >= 0.5}
    print(f"Stocks compared on 3+ days: {len(multi)} | in conflict on at least half of their days: {len(persistent)}"
          f" ({(len(persistent) / len(multi) * 100 if multi else 0):.1f}%)")
    if persistent:
        print("Persistent conflicts (symbol, days compared, average gap %):")
        for i, v in sorted(persistent.items(), key=lambda kv: -sum(x for _, x in kv[1]) / len(kv[1]))[:10]:
            print(f"  {v[0][0]:<12}{len(v):>3} days   {sum(x for _, x in v) / len(v):>6.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
