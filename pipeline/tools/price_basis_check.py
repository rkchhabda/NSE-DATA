#!/usr/bin/env python3
"""
price_basis_check.py - which price does a vendor's daily "close" correspond to?

NSE publishes two prices per stock: the OFFICIAL close and the LAST traded price, and
they differ noticeably for most stocks. This tool takes the most-traded stocks where
they differ, fetches the vendor's daily bar for the same date, and reports whether the
vendor's close matches the official close, the last price, or neither. It also compares
volume. The answer decides how the next fetcher must compare prices.

Usage (PowerShell, from the workspace root):
    $env:TWELVE_DATA_API_KEY = "your-key"        # stays in this window only
    py pipeline\\tools\\price_basis_check.py --date 2026-10-30 --count 8

The key is read from the environment and is never printed or written anywhere.
Makes at most --count requests, with a pause between them. Standard library only.
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

KEY_ENV = "TWELVE_DATA_API_KEY"


def pct(a, b):
    return abs(a - b) / abs(b) * 100.0 if b else float("inf")


def pick_candidates(csv_path, count, min_diff):
    """Most-traded usable EQ stocks where |last - close| >= min_diff percent."""
    rows = []
    with open(csv_path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            try:
                close, last, vol = float(r["close"]), float(r["last"]), float(r["volume"])
            except (ValueError, KeyError):
                continue
            if r.get("usable") != "True" or r.get("series") != "EQ" or close <= 0 or last <= 0:
                continue
            if pct(last, close) >= min_diff:
                rows.append({"symbol": r["symbol"], "close": close, "last": last, "volume": vol})
    rows.sort(key=lambda x: x["volume"], reverse=True)
    return rows[:count]


def get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "price-basis-check/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        finally:
            e.close()
        try:
            return json.loads(body)
        except ValueError:
            return {"status": "error", "message": f"HTTP {e.code}"}


def vendor_bar(base_url, symbol, exchange, date, key):
    q = urllib.parse.urlencode({"symbol": symbol, "exchange": exchange, "interval": "1day",
                                "outputsize": 5, "apikey": key})
    data = get_json(f"{base_url.rstrip('/')}/time_series?{q}")
    if data.get("status") == "error" or "values" not in data:
        return None, str(data.get("message", "unexpected response"))[:200]
    for v in data["values"]:
        if str(v.get("datetime", "")).startswith(date):
            return {"close": float(v["close"]), "volume": float(v.get("volume") or 0)}, None
    return None, f"no bar for {date}"


def classify(vendor_close, close, last, tol):
    near_close, near_last = pct(vendor_close, close) <= tol, pct(vendor_close, last) <= tol
    if near_close and not near_last:
        return "OFFICIAL"
    if near_last and not near_close:
        return "LAST"
    if near_close and near_last:
        return "BOTH"
    return "NEITHER"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True, help="trading date YYYY-MM-DD")
    ap.add_argument("--csv", help="normalised Bhavcopy CSV (default data/bhavcopy/<date>.csv)")
    ap.add_argument("--count", type=int, default=8)
    ap.add_argument("--min-diff", type=float, default=0.3, help="min |last-close| percent to use a stock")
    ap.add_argument("--tolerance", type=float, default=0.10, help="match tolerance percent (gate uses 0.10)")
    ap.add_argument("--exchange", default="NSE")
    ap.add_argument("--base-url", default="https://api.twelvedata.com")
    ap.add_argument("--pause", type=float, default=8.0, help="seconds between requests (rate limits)")
    a = ap.parse_args(argv)

    key = os.environ.get(KEY_ENV)
    if not key:
        print(f"{KEY_ENV} is not set. In PowerShell:  $env:{KEY_ENV} = \"your-key\"   then run again.")
        return 1
    path = a.csv or os.path.join("data", "bhavcopy", f"{a.date}.csv")
    if not os.path.exists(path):
        print(f"File not found: {path}. Run nse_bhavcopy.py for {a.date} first.")
        return 1
    picks = pick_candidates(path, a.count, a.min_diff)
    if not picks:
        print(f"No stocks with |last-close| >= {a.min_diff}% found in {path}.")
        return 1

    print(f"Date {a.date}: testing {len(picks)} most-traded stocks where NSE last and official close differ by >= {a.min_diff}%\n")
    print(f"{'symbol':<12}{'nse_close':>11}{'nse_last':>11}{'vendor':>11}{'d_close%':>10}{'d_last%':>9}{'vol_diff%':>10}  basis")
    results = []
    for i, p in enumerate(picks):
        if i:
            time.sleep(a.pause)
        bar, err = vendor_bar(a.base_url, p["symbol"], a.exchange, a.date, key)
        if err:
            print(f"{p['symbol']:<12} vendor error: {err}")
            if "no bar" not in err:
                print("\nStopping at the first vendor error (likely key, plan or rate limit). "
                      "Paste this message so it can be diagnosed.")
                return 2
            continue
        basis = classify(bar["close"], p["close"], p["last"], a.tolerance)
        results.append(basis)
        print(f"{p['symbol']:<12}{p['close']:>11.2f}{p['last']:>11.2f}{bar['close']:>11.2f}"
              f"{pct(bar['close'], p['close']):>10.2f}{pct(bar['close'], p['last']):>9.2f}"
              f"{pct(bar['volume'], p['volume']):>10.2f}  {basis}")

    n = len(results)
    if not n:
        print("\nNo comparable bars returned.")
        return 2
    cnt = {k: results.count(k) for k in ("OFFICIAL", "LAST", "BOTH", "NEITHER")}
    print(f"\nOf {n} compared: matches OFFICIAL close {cnt['OFFICIAL']}, matches LAST price {cnt['LAST']}, "
          f"both {cnt['BOTH']}, neither {cnt['NEITHER']}")
    if cnt["OFFICIAL"] >= 0.75 * n:
        print("Verdict: vendor daily close behaves like the OFFICIAL close. Compare it with Bhavcopy `close` as planned.")
    elif cnt["LAST"] >= 0.75 * n:
        print("Verdict: vendor daily close behaves like the LAST traded price. Evidence must record the price basis and compare against Bhavcopy `last`.")
    else:
        print("Verdict: inconclusive or mixed. Paste this table; do not build the vendor fetcher yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
