#!/usr/bin/env python3
"""
compute_indicators.py - compute technical indicators from price history and merge into gate input.

Computes:
  - rsi14   : 14-period Wilder RSI
  - macd    : MACD line (12, 26, 9)
  - atr14   : 14-period Wilder ATR
  - ema20   : 20-period EMA
  - ema50   : 50-period EMA
  - sma200  : 200-period SMA
  - breakout: 20-day high/low breakout status

Merges computed indicators into stock["evidence"]["indicators"] and
history integrity into stock["evidence"]["history"] for gate_check.py.

Usage:
    py pipeline\\tools\\compute_indicators.py --gate-input data\\gate_input_2026-10-30.json
"""
import argparse
import csv
import json
import os
import sys
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from indicators.maths import sma, ema, rsi, macd, atr, breakout


def _f(val) -> Optional[float]:
    try:
        return float(val) if val not in (None, "") else None
    except (TypeError, ValueError):
        return None


def load_stock_history_csv(csv_path: str, up_to_date: Optional[str] = None) -> List[dict]:
    """Read a stock's historical CSV, optionally filtering up to a specific date."""
    bars = []
    if not os.path.exists(csv_path):
        return bars
    with open(csv_path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            dt = row.get("date")
            if up_to_date and dt and dt > up_to_date:
                continue
            c = _f(row.get("adj_close") or row.get("close"))
            h = _f(row.get("adj_high") or row.get("high"))
            l = _f(row.get("adj_low") or row.get("low"))
            o = _f(row.get("adj_open") or row.get("open"))
            v = _f(row.get("adj_volume") or row.get("volume"))
            if c is not None:
                bars.append({
                    "date": dt,
                    "open": o or c,
                    "high": h or c,
                    "low": l or c,
                    "close": c,
                    "volume": v or 0.0,
                })
    bars.sort(key=lambda b: b["date"])
    return bars


def compute_stock_indicators(bars: List[dict]) -> Tuple[Dict[str, dict], Dict[str, any], Dict[str, any]]:
    """
    Compute indicators and history block from bars.
    Returns (indicators_dict, history_dict, extras_dict).
    """
    if not bars:
        return {}, {"bars": 0, "adjusted": False, "gap_count": 0}, {}

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]

    ind = {}
    
    rsi_val = rsi(closes, 14)
    if rsi_val is not None:
        ind["rsi14"] = {"internal": round(rsi_val, 2)}

    macd_res = macd(closes, 12, 26, 9)
    if macd_res is not None:
        ind["macd"] = {"internal": macd_res["macd"]}

    atr_val = atr(highs, lows, closes, 14)
    if atr_val is not None:
        ind["atr14"] = {"internal": round(atr_val, 2)}

    ema20_val = ema(closes, 20)
    if ema20_val is not None:
        ind["ema20"] = {"internal": round(ema20_val, 2)}

    ema50_val = ema(closes, 50)
    if ema50_val is not None:
        ind["ema50"] = {"internal": round(ema50_val, 2)}

    sma200_val = sma(closes, 200)
    if sma200_val is not None:
        ind["sma200"] = {"internal": round(sma200_val, 2)}

    hist_meta = {
        "bars": len(bars),
        "adjusted": True,
        "gap_count": 0,
    }

    extras = {}
    brk = breakout(highs, lows, closes, 20)
    if brk is not None:
        extras["breakout_20"] = brk

    return ind, hist_meta, extras


def enrich_gate_input(stocks: List[dict],
                      history_dir: str,
                      manifest: Optional[Dict[str, dict]] = None) -> List[dict]:
    """Enrich each stock in gate input with calculated indicators and history."""
    enriched = []
    manifest = manifest or {}
    for s in stocks:
        s = dict(s)
        sym = s.get("symbol")
        td = s.get("trading_date")
        csv_path = os.path.join(history_dir, f"{sym}.csv")
        bars = load_stock_history_csv(csv_path, up_to_date=td)

        ind, hist, extra_ind = compute_stock_indicators(bars)
        
        # Override history metadata if available from manifest
        if sym in manifest:
            m = manifest[sym]
            hist["adjusted"] = m.get("adjusted", True)
            hist["gap_count"] = m.get("gap_count", 0)

        ev = s.setdefault("evidence", {})
        if ind:
            ev["indicators"] = ind
        ev["history"] = hist

        extras = s.setdefault("extras", {})
        extras.update(extra_ind)
        enriched.append(s)

    return enriched


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gate-input", required=True, help="Path to gate_input_YYYY-MM-DD.json")
    ap.add_argument("--history-dir", default=os.path.join("data", "history"))
    ap.add_argument("--out", help="Output path (default overwrites gate-input)")
    a = ap.parse_args(argv)

    if not os.path.exists(a.gate_input):
        print(f"Gate input file not found: {a.gate_input}")
        return 1

    with open(a.gate_input, "r", encoding="utf-8") as f:
        data = json.load(f)

    stocks = data["stocks"] if isinstance(data, dict) and "stocks" in data else data
    if not isinstance(stocks, list):
        print("Invalid gate input JSON: expected list or {'stocks': [...]}")
        return 1

    manifest_path = os.path.join(a.history_dir, "history_manifest.json")
    manifest = {}
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

    print(f"Enriching {len(stocks)} stocks with indicators from {a.history_dir}...")
    enriched_stocks = enrich_gate_input(stocks, a.history_dir, manifest)

    out_path = a.out or a.gate_input
    out_obj = {"stocks": enriched_stocks} if isinstance(data, dict) and "stocks" in data else enriched_stocks
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out_obj, f, indent=2)

    with_ind = sum(1 for s in enriched_stocks if s.get("evidence", {}).get("indicators"))
    print(f"Successfully enriched {len(enriched_stocks)} stocks -> {out_path}")
    print(f"  Stocks with computed indicators: {with_ind}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

