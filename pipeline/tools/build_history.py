#!/usr/bin/env python3
"""
build_history.py - aggregate daily Bhavcopy files into compact, adjusted historical series.

Extracts OHLCV for EQ equities, detects corporate actions (splits, bonuses, dividends)
via prev_close != prior_day_close discrepancies, calculates backward adjustment factors,
and writes per-stock compact history plus history integrity metadata.

Usage:
    py pipeline\\tools\\build_history.py --bhavcopy-dir data\\bhavcopy --out-dir data\\history
"""
import argparse
import csv
import json
import os
import sys
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple, Set

EPS = 0.01  # 1 paisa


def _f(val) -> Optional[float]:
    try:
        return float(val) if val not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_iso_date(d_str: str) -> date:
    return date.fromisoformat(d_str.strip())


def load_holidays(path: Optional[str]) -> Set[date]:
    holidays = set()
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    try:
                        holidays.add(parse_iso_date(line))
                    except ValueError:
                        pass
    return holidays


def is_trading_day(d: date, holidays: Set[date]) -> bool:
    return d.weekday() < 5 and d not in holidays


def find_bhavcopy_files(bhavcopy_dir: str) -> List[Tuple[date, str]]:
    """Find all YYYY-MM-DD.csv files sorted by date."""
    files = []
    if not os.path.isdir(bhavcopy_dir):
        return files
    for name in os.listdir(bhavcopy_dir):
        if name.endswith(".csv") and len(name) == 14:  # YYYY-MM-DD.csv
            try:
                d = date.fromisoformat(name[:-4])
                files.append((d, os.path.join(bhavcopy_dir, name)))
            except ValueError:
                continue
    files.sort(key=lambda x: x[0])
    return files


def load_day_bhavcopy(path: str) -> Dict[str, dict]:
    """Read a normalized Bhavcopy CSV into symbol -> dict."""
    stocks = {}
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("usable") != "True" or row.get("series") != "EQ":
                continue
            sym = row.get("symbol")
            close = _f(row.get("close"))
            vol = _f(row.get("volume"))
            if not sym or close is None or vol is None:
                continue
            stocks[sym] = {
                "date": row.get("date"),
                "symbol": sym,
                "isin": row.get("isin"),
                "prev_close": _f(row.get("prev_close")),
                "open": _f(row.get("open")),
                "high": _f(row.get("high")),
                "low": _f(row.get("low")),
                "close": close,
                "volume": vol,
                "trades": _f(row.get("trades")),
                "flags": [x for x in (row.get("flags") or "").split(";") if x],
            }
    return stocks


def build_stock_histories(bhavcopy_files: List[Tuple[date, str]],
                          tol_pct: float = 0.05) -> Tuple[Dict[str, List[dict]], Dict[str, dict]]:
    """
    Build unadjusted daily series per stock, detect corporate actions,
    and apply backwards adjustment.
    """
    # Accumulate bars chronologically per stock
    raw_history: Dict[str, List[dict]] = {}
    
    for d, path in bhavcopy_files:
        day_stocks = load_day_bhavcopy(path)
        for sym, data in day_stocks.items():
            if sym not in raw_history:
                raw_history[sym] = []
            raw_history[sym].append(data)

    adjusted_history: Dict[str, List[dict]] = {}
    metadata: Dict[str, dict] = {}

    for sym, bars in raw_history.items():
        if not bars:
            continue
        
        # Sort by date
        bars.sort(key=lambda b: b["date"])
        
        # Detect corporate action adjustments
        actions = []
        adj_bars = [dict(b) for b in bars]
        # Track cumulative adjustment factor starting from latest bar backwards
        cum_factors = [1.0] * len(adj_bars)
        
        for i in range(1, len(adj_bars)):
            today_pc = adj_bars[i]["prev_close"]
            yest_c = adj_bars[i - 1]["close"]
            if today_pc is not None and yest_c is not None and yest_c > 0:
                diff = abs(today_pc - yest_c)
                diff_pct = (diff / yest_c) * 100.0
                if diff_pct > tol_pct and diff > EPS:
                    factor = today_pc / yest_c
                    actions.append({
                        "date": adj_bars[i]["date"],
                        "factor": round(factor, 6),
                        "prev_close": today_pc,
                        "prior_close": yest_c,
                        "diff_pct": round(diff_pct, 2),
                    })
                    # Adjust all previous bars (0 .. i-1) by factor
                    for j in range(0, i):
                        cum_factors[j] *= factor

        # Apply cumulative factors to generate adjusted prices
        for j in range(len(adj_bars)):
            f = cum_factors[j]
            adj_bars[j]["adj_factor"] = round(f, 6)
            adj_bars[j]["adj_open"] = round(adj_bars[j]["open"] * f, 2) if adj_bars[j]["open"] is not None else None
            adj_bars[j]["adj_high"] = round(adj_bars[j]["high"] * f, 2) if adj_bars[j]["high"] is not None else None
            adj_bars[j]["adj_low"] = round(adj_bars[j]["low"] * f, 2) if adj_bars[j]["low"] is not None else None
            adj_bars[j]["adj_close"] = round(adj_bars[j]["close"] * f, 2)
            adj_bars[j]["adj_volume"] = round(adj_bars[j]["volume"] / f, 2) if f > 0 else adj_bars[j]["volume"]

        adjusted_history[sym] = adj_bars
        metadata[sym] = {
            "symbol": sym,
            "isin": adj_bars[-1].get("isin"),
            "bars": len(adj_bars),
            "start_date": adj_bars[0]["date"],
            "end_date": adj_bars[-1]["date"],
            "adjusted": True,
            "corporate_actions": actions,
            "gap_count": 0,  # calculated with calendar below if needed
        }

    return adjusted_history, metadata


def compute_gaps(dates: List[date], holidays: Set[date]) -> int:
    """Count missing trading days between first and last date."""
    if len(dates) <= 1:
        return 0
    date_set = set(dates)
    curr = dates[0] + timedelta(days=1)
    end = dates[-1]
    gaps = 0
    while curr < end:
        if is_trading_day(curr, holidays) and curr not in date_set:
            gaps += 1
        curr += timedelta(days=1)
    return gaps


def write_history_files(adjusted_history: Dict[str, List[dict]],
                        metadata: Dict[str, dict],
                        out_dir: str,
                        holidays: Set[date]):
    """Save history per stock and manifest JSON."""
    os.makedirs(out_dir, exist_ok=True)
    manifest_path = os.path.join(out_dir, "history_manifest.json")
    
    # Update gap counts against trading calendar
    for sym, meta in metadata.items():
        bars = adjusted_history[sym]
        dates = [parse_iso_date(b["date"]) for b in bars]
        meta["gap_count"] = compute_gaps(dates, holidays)

    # Save manifest
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    # Save compact CSV per stock
    cols = ["date", "open", "high", "low", "close", "volume",
            "adj_open", "adj_high", "adj_low", "adj_close", "adj_volume", "adj_factor"]
    
    for sym, bars in adjusted_history.items():
        sym_file = os.path.join(out_dir, f"{sym}.csv")
        with open(sym_file, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for b in bars:
                w.writerow([b.get(c) for c in cols])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bhavcopy-dir", default=os.path.join("data", "bhavcopy"))
    ap.add_argument("--out-dir", default=os.path.join("data", "history"))
    ap.add_argument("--holidays", default=os.path.join("pipeline", "config", "holidays.txt"))
    a = ap.parse_args(argv)

    files = find_bhavcopy_files(a.bhavcopy_dir)
    if not files:
        print(f"No Bhavcopy CSV files found in {a.bhavcopy_dir}")
        return 1

    holidays = load_holidays(a.holidays)
    print(f"Processing {len(files)} Bhavcopy dates ({files[0][0]} to {files[-1][0]})...")

    history, meta = build_stock_histories(files)
    write_history_files(history, meta, a.out_dir, holidays)

    actions_count = sum(len(m["corporate_actions"]) for m in meta.values())
    print(f"Aggregated {len(history)} stocks into {a.out_dir}")
    print(f"  Total corporate action adjustments detected: {actions_count}")
    print(f"  Manifest written to {os.path.join(a.out_dir, 'history_manifest.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

