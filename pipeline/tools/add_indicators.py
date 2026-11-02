#!/usr/bin/env python3
"""
add_indicators.py - add price history and indicators to the gate input for one trading date.

Reads data/gate_input_<date>.json (from build_gate_input.py) and the history store, then for every
stock: adjusts past prices for splits/bonuses (using NSE's own reference prices), counts missing
days, and computes RSI(14), MACD(12,26,9), ATR(14), EMA20, EMA50 and SMA200 from the adjusted bars.

Writes into each stock's evidence:
  history    {bars, adjusted, gap_count, events_applied}
  indicators {rsi14, macd, atr14, ema20, ema50, sma200: {internal: value, vendors: []}}
Indicators without enough bars stay empty (MISSING); nothing is estimated. The adjusted last close
must equal NSE's close for the date, otherwise the stock gets no history at all.

Usage (from the workspace root):
    py pipeline\\tools\\add_indicators.py --date 2026-10-30
"""
import argparse
import json
import os
import sys
from collections import Counter

ROOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT_DIR)
from analytics import history as hs  # noqa: E402
from analytics import indicators as ind  # noqa: E402

DEFAULT_GATE = os.path.join(".agents", "skills", "nse-data-integrity-engine", "scripts")
NAMES = ("rsi14", "macd", "atr14", "ema20", "ema50", "sma200")


def augment(stocks, hist_dir, trading_date, large_threshold=0.10):
    """Mutates stocks in place. Returns (stats dict, error string or None)."""
    cal = hs.load_calendar(hist_dir)
    days = hs.open_days(cal, trading_date)
    if not days or days[-1] != trading_date:
        return None, (f"{trading_date} is not an open day in the history store. "
                      f"Run history_backfill.py for it first.")
    series = hs.load_series(hist_dir, days, {s["isin"] for s in stocks if s.get("isin")})
    stats = Counter()
    big = []
    for s in stocks:
        ev = s.setdefault("evidence", {})
        ser = series.get(s.get("isin"))
        nse_close = next((r["value"] for r in ev.get("cmp", []) if r["source"] == "nse_bhavcopy"), None)
        if ser is None or ser.idx[-1] != len(days) - 1 or nse_close is None or abs(ser.c[-1] - nse_close) > 0.01:
            stats["no_history"] += 1
            ev["history"] = {"bars": 0, "adjusted": False, "gap_count": 0, "events_applied": 0}
            continue
        a = hs.analyse(ser, large_threshold)
        o, h, l, c, v = hs.adjusted_arrays(ser, a["events"])
        vals = ind.compute_all(o, h, l, c)
        ev["history"] = {"bars": len(c), "adjusted": True, "gap_count": a["gaps"], "events_applied": len(a["events"])}
        ev["indicators"] = {n: {"internal": vals[n], "vendors": []} for n in NAMES}
        s.setdefault("extras", {})["indicators"] = {k: vals[k] for k in vals}
        s["extras"]["adjustment_events"] = [{"date": days[ser.idx[j]], "factor": round(r, 6)} for j, r in a["events"]]
        stats["with_history"] += 1
        stats["bars_ok"] += len(c) >= 260
        stats["gaps"] += a["gaps"] > 0
        stats["events"] += len(a["events"])
        stats["event_stocks"] += bool(a["events"])
        stats["minor_events"] += len(a["minor"])
        stats["all_indicators"] += all(vals[n] is not None for n in NAMES)
        for j, r in a["events"]:
            big.append((s["symbol"], days[ser.idx[j]], r))
    stats["total"] = len(stocks)
    stats["open_days"] = len(days)
    return {"counts": stats, "events": sorted(big, key=lambda x: x[1], reverse=True)}, None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True)
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--input", help="gate input JSON (default data/gate_input_<date>.json); rewritten in place")
    ap.add_argument("--hist-dir")
    ap.add_argument("--large-threshold", type=float, default=0.10,
                    help="a reference-price jump bigger than this is treated as a split/bonus (default 10%%)")
    ap.add_argument("--gate-scripts", default=DEFAULT_GATE)
    a = ap.parse_args(argv)

    path = a.input or os.path.join(a.out_dir, f"gate_input_{a.date}.json")
    if not os.path.exists(path):
        print(f"{path} not found. Run build_gate_input.py for {a.date} first.")
        return 1
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    stocks = data["stocks"]
    res, err = augment(stocks, a.hist_dir or os.path.join(a.out_dir, "history"), a.date, a.large_threshold)
    if err:
        print(err)
        return 1
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    c = res["counts"]
    print(f"Date {a.date}: {c['total']} stocks | history store: {c['open_days']} open days")
    print(f"  with history: {c['with_history']} | 260+ bars: {c['bars_ok']} | with missing days (gaps): {c['gaps']} | "
          f"no usable history: {c['no_history']}")
    print(f"  split/bonus-type adjustments applied: {c['events']} on {c['event_stocks']} stocks "
          f"(smaller dividend-type steps left unadjusted: {c['minor_events']})")
    print(f"  all six indicators computed: {c['all_indicators']}")
    for sym, d, r in res["events"][:8]:
        print(f"    {sym:<12}{d}  factor {r:.4f}")

    sys.path.insert(0, a.gate_scripts)
    try:
        import gate_check as gate
    except ImportError:
        print(f"\n(gate_check.py not found in {a.gate_scripts}; skipping the gate view)")
        return 0
    conf, cond, ind_status = [], Counter(), Counter()
    for s in stocks:
        r = gate.evaluate(s)
        conf.append(r["data_confidence"]["score"])
        for k, v in r["conditions"].items():
            cond[k] += bool(v)
        ind_status.update(v["status"] for v in r["validation"]["indicators"].values())
    n = len(stocks)
    print("\nGate view (data conditions only; the scoring modules are not built yet):")
    for k in ("CMP verified", "Volume verified", "History adjusted, gap-free, sufficient", "Technical indicators verified"):
        print(f"  {k:<42}{cond[k]:>6}  ({cond[k] / n * 100:.1f}%)")
    print(f"  indicator statuses: " + ", ".join(f"{k} {v}" for k, v in ind_status.most_common()))
    print(f"  Data Confidence: mean {sum(conf) / n:.1f}, highest {max(conf):.1f}")
    print(f"  accepting computed-only indicators: {gate.CONFIG['allow_computed_only_indicators']}"
          f" | NSE two-file volume accepted: {gate.CONFIG['volume_two_file_ok']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
