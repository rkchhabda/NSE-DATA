"""run_daily.py - One-command daily market orchestrator and top 5 stock selector.

Coordinates the daily post-market routine:
  1. Validates that Bhavcopy data exists for the target date (or can be fetched).
  2. Builds/updates historical price database (build_history.py).
  3. Builds corroborated gate input combining NSE + BSE (build_gate_input.py).
  4. Computes technical indicators (compute_indicators.py).
  5. Screens universe through multi-factor conviction and data-integrity gate (screen_universe.py).
  6. Filters out ETFs/Index funds to isolate pure individual equity stocks.
  7. Formats and prints the Top 5 High-Conviction BUY NOW setups.

Usage:
    py pipeline\tools\run_daily.py --date 2026-10-30
    py pipeline\tools\run_daily.py                     # runs on latest available date
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT))
NON_EQUITY_SUFFIXES = ("BEES", "ETF", "IETF", "GOLD", "SILVER", "LIQUID", "GSEC", "NIFTY", "SENSEX")


def is_pure_equity(symbol: str) -> bool:
    """Filter out ETFs, liquid funds, index trackers, and gold/silver tokens."""
    sym_upper = symbol.upper()
    for pattern in ("BEES", "ETF", "IETF", "LIQUID", "GSEC", "INVIT", "REIT"):
        if pattern in sym_upper:
            return False
    return True


def get_latest_available_date(bhav_dir: Path) -> str:
    """Find the most recent YYYY-MM-DD normalized Bhavcopy on disk."""
    if not bhav_dir.exists():
        raise FileNotFoundError(f"Directory {bhav_dir} does not exist.")
    files = sorted([f.stem for f in bhav_dir.glob("*.csv") if len(f.stem) == 10 and f.stem[4] == "-" and f.stem[7] == "-"])
    if not files:
        raise FileNotFoundError(f"No valid Bhavcopy files found in {bhav_dir}.")
    return files[-1]


def run_pipeline_step(cmd: List[str], desc: str) -> None:
    """Execute a pipeline command with clean error handling."""
    print(f"  [>] {desc}...")
    res = subprocess.run([sys.executable] + cmd, cwd=str(WORKSPACE_ROOT), capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Error during '{desc}':")
        print(res.stderr or res.stdout)
        raise RuntimeError(f"Step failed: {desc}")


def run_daily(date_str: Optional[str] = None, top_n: int = 5) -> Dict:
    bhav_dir = WORKSPACE_ROOT / "data" / "bhavcopy"
    if not date_str:
        date_str = get_latest_available_date(bhav_dir)

    print("=" * 72)
    print(f"  NSE DAILY SWING TRADING PIPELINE: {date_str}")
    print("=" * 72)

    # 1. Update history
    run_pipeline_step(
        ["pipeline/tools/build_history.py", "--bhavcopy-dir", "data/bhavcopy", "--out-dir", "data/history"],
        "Updating historical price series & corporate action adjustments"
    )

    # 1B. Fetch & normalize NSE F&O Bhavcopy (if available)
    fo_json_path = WORKSPACE_ROOT / "data" / "fo" / f"{date_str}.json"
    if not fo_json_path.exists():
        try:
            run_pipeline_step(
                ["pipeline/fetchers/nse_fo_bhavcopy.py", "--date", date_str],
                "Fetching & normalizing NSE F&O derivatives Bhavcopy"
            )
        except Exception as e:
            print(f"  [!] Note: F&O Bhavcopy not fetched for {date_str} (proceeding in standard cash mode).")

    # 2. Build gate input (corroborate NSE + BSE + F&O)
    gate_input_path = WORKSPACE_ROOT / "data" / f"gate_input_{date_str}.json"
    run_pipeline_step(
        ["pipeline/tools/build_gate_input.py", "--date", date_str, "--out", str(gate_input_path)],
        f"Corroborating NSE & BSE Bhavcopies into {gate_input_path.name}"
    )

    # 3. Compute indicators
    run_pipeline_step(
        ["pipeline/tools/compute_indicators.py", "--gate-input", str(gate_input_path)],
        "Computing technical indicators (RSI-14, MACD, ATR-14, 20/50 EMA, 200 SMA)"
    )

    # 4. Screen universe
    run_pipeline_step(
        ["pipeline/tools/screen_universe.py", "--date", date_str, "--input", str(gate_input_path)],
        "Running multi-factor scoring and data integrity gate check"
    )

    # 5. Load screening report JSON
    rep_path = WORKSPACE_ROOT / "data" / "reports" / f"screening_report_{date_str}.json"
    if not rep_path.exists():
        raise FileNotFoundError(f"Report JSON not found: {rep_path}")

    with open(rep_path, "r", encoding="utf-8") as f:
        rep_data = json.load(f)

    screened = rep_data.get("screened", [])
    buy_now_all = [s for s in screened if s.get("gate_status") == "BUY NOW"]

    # Filter out ETFs/index funds to get pure equities
    equity_buys = [s for s in buy_now_all if is_pure_equity(s["symbol"])]
    equity_buys.sort(key=lambda x: -x.get("conviction_score", 0))

    top_picks = equity_buys[:top_n]

    print("\n" + "=" * 72)
    print(f"  TOP {len(top_picks)} HIGH-CONVICTION ACTIONABLE SWING TRADES ({date_str})")
    print("=" * 72)

    for idx, s in enumerate(top_picks, 1):
        ts = s["trade_setup"]
        sc = s["upstream_scores"]
        val = s.get("validation", {})
        ind = val.get("indicators", {})
        rsi = ind.get("rsi14", {}).get("value", "N/A")
        macd = ind.get("macd", {}).get("value", "N/A")
        atr = ts.get("atr_14", "N/A")
        vol_info = val.get("volume", {})
        vol_status = vol_info.get("status", "VERIFIED")

        print(f"\n#{idx} {s['symbol']} | CMP: Rs {s['cmp']} | Conviction: {s['conviction_score']}/100")
        print(f"    Data Confidence: {s['data_confidence']} ({s['data_band']}) | Volume: {vol_status}")
        fo_info = s.get("fo")
        if fo_info:
            print(f"    Derivatives: {fo_info['buildup']} (Futures OI: {fo_info['fut_oi_change_pct']:+0.1f}%) | "
                  f"Call Wall: Rs {fo_info['call_wall']} | Put Wall: Rs {fo_info['put_wall']} | PCR: {fo_info['pcr_oi']}")
        print(f"    Setup: Entry: Rs {ts['entry_range']} | Stop Loss: Rs {ts['stop_loss']} (1.5x ATR)")
        print(f"    Targets: T1 = Rs {ts['target_1']} | T2 = Rs {ts['target_2']} | Risk:Reward = {ts['risk_reward_t1']}")
        print(f"    Signals: Technicals {sc['technical']}/100, RSI(14)={rsi}, MACD={macd}, ATR={atr}")

    print("\n" + "-" * 72)
    print("  Risk Rule: Risk at most 1% - 2% of portfolio capital per trade.")
    print(f"  Full report: data/reports/screening_report_{date_str}.md")
    print("-" * 72 + "\n")

    return {
        "date": date_str,
        "total_screened": len(screened),
        "total_buy_now": len(buy_now_all),
        "pure_equity_buy_now": len(equity_buys),
        "top_picks": top_picks
    }


def main():
    parser = argparse.ArgumentParser(description="One-command daily market orchestrator and top 5 stock selector")
    parser.add_argument("--date", type=str, default=None, help="Trading date (YYYY-MM-DD)")
    parser.add_argument("--top", type=int, default=5, help="Number of top stocks to display (default 5)")
    parser.add_argument("--morning", action="store_true", help="Run 09:30 AM morning opening range check instead of evening screen")
    parser.add_argument("--capital", type=float, default=500000.0, help="Trading capital in INR for morning check (default 5,00,000)")
    parser.add_argument("--risk-pct", type=float, default=1.0, help="Risk percentage per trade for morning check (default 1.0 percent)")
    parser.add_argument("--notify", action="store_true", help="Dispatch alerts to Telegram and Email")
    args = parser.parse_args()

    if args.morning:
        from pipeline.tools.check_morning_entry import run_morning_check
        run_morning_check(
            date_str=args.date,
            capital=args.capital,
            risk_pct=args.risk_pct,
            top_n=args.top,
            notify=args.notify,
        )
    else:
        run_daily(date_str=args.date, top_n=args.top)


if __name__ == "__main__":
    main()
