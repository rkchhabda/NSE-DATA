#!/usr/bin/env python3
"""
build_gate_input.py - combine NSE and BSE Bhavcopy output into gate input (cmp + volume evidence).

For every usable NSE EQ stock it writes:
  cmp    : the NSE official close, plus the BSE close (with BSE trade count) when the stock
           matches by ISIN, is usable on BSE and traded there
  volume : the NSE volume, plus a second NSE record (nse_udiff) when NSE's two files were
           cross-checked. BSE volume is never added; it cannot verify NSE volume.
Scores, indicators, fundamentals and history are NOT included: other modules supply those.

It then (if the skill's gate_check.py can be found) runs the gate's price and volume
validation over every stock and prints how many are VERIFIED, SINGLE_OFFICIAL, CONFLICT, etc.

Usage (from the workspace root, after both fetchers have run for the date):
    py pipeline\\tools\\build_gate_input.py --date 2026-10-30
"""
import argparse
import csv
import json
import os
import sys
from collections import Counter

DEFAULT_GATE = os.path.join(".agents", "skills", "nse-data-integrity-engine", "scripts")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def read_rows(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def bse_index(rows):
    """usable, traded BSE rows by ISIN; an ISIN that appears twice is ambiguous and dropped."""
    out, dup = {}, set()
    for r in rows:
        flags = (r.get("flags") or "").split(";")
        if r.get("usable") != "True" or "no_trades" in flags or not r.get("isin"):
            continue
        if r["isin"] in out:
            dup.add(r["isin"])
        out[r["isin"]] = r
    for i in dup:
        out.pop(i, None)
    return out


def build(nse_rows, bse_rows, date):
    bse = bse_index(bse_rows)
    stocks = []
    for r in nse_rows:
        close, vol = _f(r.get("close")), _f(r.get("volume"))
        if r.get("usable") != "True" or r.get("series") != "EQ" or not close or vol is None:
            continue
        flags = [x for x in (r.get("flags") or "").split(";") if x]
        cmp = [{"source": "nse_bhavcopy", "value": close, "as_of": date}]
        extras = {"series": r["series"], "prev_close": _f(r.get("prev_close")),
                  "delivery_pct": _f(r.get("deliv_pct")), "delivery_qty": _f(r.get("deliv_qty")),
                  "flags": flags, "needs_adjustment_check": "possible_corporate_action" in flags,
                  "bse_matched": False}
        b = bse.get(r.get("isin"))
        if b and _f(b.get("close")):
            cmp.append({"source": "bse_bhavcopy", "value": _f(b["close"]), "as_of": date,
                        "trades": _f(b.get("trades"))})
            extras.update({"bse_matched": True, "bse_scrip_code": b.get("scrip_code"),
                           "bse_trades": _f(b.get("trades"))})
        volume = [{"source": "nse_bhavcopy", "value": vol, "as_of": date}]
        # An ISIN exists only when the stock was found in NSE's second file (UDiFF), and a row is
        # only "usable" if that file's close and volume matched the first file exactly. So a usable
        # row with an ISIN has passed NSE's two-file cross-check: record it as a second NSE source.
        if r.get("isin"):
            volume.append({"source": "nse_udiff", "value": vol, "as_of": date})
        extras["volume_two_files"] = bool(r.get("isin"))
        stocks.append({"symbol": r["symbol"], "isin": r.get("isin") or None, "trading_date": date,
                       "cmp_mode": "eod",
                       "evidence": {"cmp": cmp, "volume": volume},
                       "extras": extras})
    return stocks


def load_gate(scripts_dir):
    if not scripts_dir or not os.path.exists(os.path.join(scripts_dir, "gate_check.py")):
        return None
    sys.path.insert(0, scripts_dir)
    import gate_check
    return gate_check


def summarise(stocks, gate):
    cmp_c, vol_c = Counter(), Counter()
    for s in stocks:
        r = gate.evaluate(s)["validation"]
        cmp_c[r["cmp"]["status"]] += 1
        vol_c[r["volume"]["status"]] += 1
    return cmp_c, vol_c


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True)
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--out", help="output JSON (default data/gate_input_<date>.json)")
    ap.add_argument("--gate-scripts", default=DEFAULT_GATE, help="folder containing gate_check.py (for the summary)")
    a = ap.parse_args(argv)

    nse_path = os.path.join(a.out_dir, "bhavcopy", f"{a.date}.csv")
    bse_path = os.path.join(a.out_dir, "bse", f"{a.date}.csv")
    for p, name in ((nse_path, "NSE"), (bse_path, "BSE")):
        if not os.path.exists(p):
            print(f"{name} file not found: {p}. Run its fetcher for {a.date} first.")
            return 1
    stocks = build(read_rows(nse_path), read_rows(bse_path), a.date)
    out = a.out or os.path.join(a.out_dir, f"gate_input_{a.date}.json")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"stocks": stocks}, f)
    matched = sum(s["extras"]["bse_matched"] for s in stocks)
    print(f"Date {a.date}: {len(stocks)} stocks written to {out}")
    print(f"  with a BSE price record: {matched} | NSE only: {len(stocks) - matched}")

    gate = load_gate(a.gate_scripts)
    if not gate:
        print(f"  (gate_check.py not found in {a.gate_scripts}; skipping the validation summary)")
        return 0
    cmp_c, vol_c = summarise(stocks, gate)
    print("\nPrice (CMP) validation across all stocks:")
    for k, v in cmp_c.most_common():
        print(f"  {k:<18}{v:>6}  ({v / len(stocks) * 100:.1f}%)")
    print("\nVolume validation across all stocks:")
    for k, v in vol_c.most_common():
        print(f"  {k:<18}{v:>6}  ({v / len(stocks) * 100:.1f}%)")
    print(f"\nThe gate rules used: BSE must have >= {gate.CONFIG['min_bse_trades']} trades and agree within "
          f"{gate.CONFIG['cross_exchange_pct']}%. Volume cannot be verified from BSE; NSE_TWO_FILES = NSE's two files agree "
          f"(accepted: {gate.CONFIG['volume_two_file_ok']}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
