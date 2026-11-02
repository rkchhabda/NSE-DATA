#!/usr/bin/env python3
"""
compare_exchanges.py - how closely do NSE and BSE closing prices agree?

Joins the normalised NSE and BSE Bhavcopy files by ISIN and reports the distribution of
|NSE close - BSE close| in percent, so the cross-exchange tolerance can be chosen from data
instead of guessed. Also shows how BSE volume compares with NSE volume (it is not a
substitute), and the stocks with the largest gaps.

Rows excluded from the comparison: unusable rows, non-EQ NSE series, BSE rows with no
trades (BSE carries the previous close forward, so they are not an independent price).

Usage (from the workspace root, after running both fetchers for the same date):
    py pipeline\\tools\\compare_exchanges.py --date 2026-10-30
    py pipeline\\tools\\compare_exchanges.py --date 2026-10-30 --out data\\reports\\nse_vs_bse_2026-10-30.csv
"""
import argparse
import csv
import os
import sys

THRESHOLDS = (0.10, 0.25, 0.50, 1.00, 2.00)
TRADE_TIERS = (0, 100, 1000)  # BSE trades on the day: all / at least 100 / at least 1000


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load(path, nse):
    out = {}
    dup = set()
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            flags = (r.get("flags") or "").split(";")
            if r.get("usable") != "True" or "no_trades" in flags or not r.get("isin"):
                continue
            if nse and r.get("series") != "EQ":
                continue
            if r["isin"] in out:
                dup.add(r["isin"])
            out[r["isin"]] = r
    for i in dup:  # ambiguous: never compare
        out.pop(i, None)
    return out


def compare(nse_rows, bse_rows):
    res = []
    for isin, n in nse_rows.items():
        b = bse_rows.get(isin)
        if not b:
            continue
        nc, bc = _f(n["close"]), _f(b["close"])
        if not nc or not bc or nc <= 0 or bc <= 0:
            continue
        nv, bv = _f(n["volume"]), _f(b["volume"])
        res.append({"isin": isin, "symbol": n["symbol"], "nse_close": nc, "bse_close": bc,
                    "diff_pct": abs(nc - bc) / nc * 100.0, "nse_volume": nv, "bse_volume": bv,
                    "bse_trades": _f(b.get("trades")) or 0.0,
                    "vol_ratio": (bv / nv) if nv else None})
    return res


def percentile(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def summarise(res, min_trades):
    sub = sorted(r["diff_pct"] for r in res if r["bse_trades"] >= min_trades)
    n = len(sub)
    return {"n": n,
            "within": {t: (sum(d <= t for d in sub) / n * 100.0 if n else float("nan")) for t in THRESHOLDS},
            "p50": percentile(sub, 50), "p90": percentile(sub, 90), "p95": percentile(sub, 95),
            "p99": percentile(sub, 99), "max": sub[-1] if sub else float("nan")}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True)
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--nse")
    ap.add_argument("--bse")
    ap.add_argument("--out", help="write every compared stock to this CSV")
    ap.add_argument("--worst", type=int, default=10)
    ap.add_argument("--symbols", default="TCS,INFY,RELIANCE")
    a = ap.parse_args(argv)

    nse_path = a.nse or os.path.join(a.out_dir, "bhavcopy", f"{a.date}.csv")
    bse_path = a.bse or os.path.join(a.out_dir, "bse", f"{a.date}.csv")
    for p, name in ((nse_path, "NSE"), (bse_path, "BSE")):
        if not os.path.exists(p):
            print(f"{name} file not found: {p}. Run its fetcher for {a.date} first.")
            return 1
    nse_rows, bse_rows = load(nse_path, True), load(bse_path, False)
    res = compare(nse_rows, bse_rows)
    if not res:
        print("No stocks could be compared (no ISIN matches). Check both files.")
        return 1

    print(f"Date {a.date}")
    print(f"NSE usable EQ rows: {len(nse_rows)} | BSE usable traded rows: {len(bse_rows)} | matched by ISIN: {len(res)}\n")
    print("Share of matched stocks whose NSE and BSE closes agree within X%:")
    print(f"{'BSE trades that day':<22}{'stocks':>8}" + "".join(f"{'<=' + format(t, '.2f') + '%':>9}" for t in THRESHOLDS)
          + f"{'median':>9}{'p90':>8}{'p99':>8}{'max':>8}")
    for tier in TRADE_TIERS:
        s = summarise(res, tier)
        label = "all matched" if tier == 0 else f">= {tier}"
        print(f"{label:<22}{s['n']:>8}" + "".join(f"{s['within'][t]:>8.1f}%" for t in THRESHOLDS)
              + f"{s['p50']:>9.3f}{s['p90']:>8.3f}{s['p99']:>8.3f}{s['max']:>8.2f}")

    ratios = sorted(r["vol_ratio"] for r in res if r["vol_ratio"] is not None)
    print(f"\nBSE volume as a share of NSE volume, median: {percentile(ratios, 50) * 100:.1f}% "
          f"(90th percentile {percentile(ratios, 90) * 100:.1f}%). BSE volume cannot stand in for NSE volume.")

    want = {s.strip().upper() for s in a.symbols.split(",") if s.strip()}
    picked = [r for r in res if r["symbol"] in want]
    if picked:
        print("\nRequested symbols:")
        for r in picked:
            print(f"  {r['symbol']:<10} NSE {r['nse_close']:>10.2f}  BSE {r['bse_close']:>10.2f}  diff {r['diff_pct']:.3f}%  BSE trades {r['bse_trades']:.0f}")
    missing = want - {r["symbol"] for r in res}
    if missing:
        print(f"  not compared (absent, unusable or no trades on one exchange): {', '.join(sorted(missing))}")

    print(f"\nLargest {a.worst} gaps:")
    for r in sorted(res, key=lambda x: x["diff_pct"], reverse=True)[:a.worst]:
        print(f"  {r['symbol']:<12} NSE {r['nse_close']:>10.2f}  BSE {r['bse_close']:>10.2f}  diff {r['diff_pct']:>6.2f}%  BSE trades {r['bse_trades']:>6.0f}")

    if a.out:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        with open(a.out, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(res[0].keys()))
            w.writeheader()
            w.writerows(sorted(res, key=lambda x: x["diff_pct"], reverse=True))
        print(f"\nWrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
