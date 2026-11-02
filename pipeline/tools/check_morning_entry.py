r"""check_morning_entry.py - Post-09:30 AM Opening Range Decision Engine for Swing Trades.

Observes the first 15-minute market candle (09:15 - 09:30 IST) for shortlisted stocks:
  1. Checks current price (P_930) relative to yesterday's close (C_prev).
  2. Evaluates opening gap magnitude (Gap %).
  3. Evaluates candle body structure within the 15-minute range [L15, H15].
  4. Classifies each stock into:
     - 🟢 GREEN LIGHT      : Buy Limit Order (healthy accumulation holding upper half)
     - 🟡 BREAKOUT TRIGGER : Buy Stop-Limit Order above H15 (gap-up protection)
     - ❌ RED LIGHT        : Skip today (intraday selling pressure / red candle)
  5. Computes exact share sizing based on user portfolio risk (default 1.0%).

Usage:
    py pipeline\tools\check_morning_entry.py
    py pipeline\tools\check_morning_entry.py --date 2026-10-30 --mock
    py pipeline\tools\check_morning_entry.py --capital 300000 --risk-pct 1.5
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT))

from pipeline.fetchers.nse_live_quote import NSELiveQuoteFetcher
from pipeline.tools.run_daily import get_latest_available_date, is_pure_equity


def get_latest_screening_report(reports_dir: Path, date_str: Optional[str] = None) -> Tuple[Path, str]:
    """Finds the most recent screening report JSON."""
    if date_str:
        target = reports_dir / f"screening_report_{date_str}.json"
        if not target.exists():
            raise FileNotFoundError(f"Screening report not found for date: {target}")
        return target, date_str

    files = sorted(reports_dir.glob("screening_report_*.json"))
    if not files:
        raise FileNotFoundError(f"No screening reports found in {reports_dir}")
    latest = files[-1]
    # parse YYYY-MM-DD from screening_report_YYYY-MM-DD.json
    parsed_date = latest.stem.replace("screening_report_", "")
    return latest, parsed_date


def evaluate_opening_range(
    symbol: str,
    prev_close: float,
    open_price: float,
    h15: float,
    l15: float,
    p930: float,
    atr14: float,
    base_sl: float,
    target_1: float,
    target_2: float,
    risk_reward: str,
    capital: float = 500000.0,
    risk_pct: float = 1.0,
) -> Dict:
    """Evaluates 15-minute opening range against quantitative swing rules."""
    gap_pct = ((open_price - prev_close) / prev_close) * 100.0 if prev_close > 0 else 0.0
    change_pct = ((p930 - prev_close) / prev_close) * 100.0 if prev_close > 0 else 0.0

    range_spread = h15 - l15
    range_pos = (p930 - l15) / range_spread if range_spread > 0.001 else 0.50

    # 1. Check Scenario C: RED LIGHT (Skip)
    if p930 < prev_close:
        verdict = "RED_LIGHT"
        reason = f"Trading in the red ({change_pct:+.2f}% vs Prev Close ₹{prev_close:.2f}). Lacks relative strength."
        action = "SKIP TODAY"
        order_details = None

    elif gap_pct < -1.5:
        verdict = "RED_LIGHT"
        reason = f"Severe gap-down open ({gap_pct:+.2f}%). Daily structure damaged."
        action = "SKIP TODAY"
        order_details = None

    elif range_pos < 0.30 and (h15 - open_price) > (open_price - l15):
        verdict = "RED_LIGHT"
        reason = f"Heavy rejection from open (price collapsed to lower {range_pos*100:.0f}% of 15m range). Sell volume dominant."
        action = "SKIP TODAY"
        order_details = None

    # 2. Check Scenario B: BREAKOUT TRIGGER (Gap > 2% or coiling just below H15)
    elif gap_pct > 2.0 or range_pos < 0.50:
        verdict = "BREAKOUT_TRIGGER"
        reason = f"Gap-up (+{gap_pct:.2f}%) or coiling near resistance. Do NOT chase at market; enter only on true H15 breakout."
        action = "PLACE BUY STOP-LIMIT ORDER"

        trigger_price = round(h15 * 1.001, 2)
        limit_price = round(h15 * 1.003, 2)
        # Stop loss: tighter of ATR SL or slightly below L15
        tight_sl = round(l15 * 0.998, 2)
        sl_to_use = min(base_sl, tight_sl) if base_sl > 0 else tight_sl

        per_share_risk = max(0.05, limit_price - sl_to_use)
        capital_risk = capital * (risk_pct / 100.0)
        shares = int(capital_risk / per_share_risk)
        max_investment = round(shares * limit_price, 2)

        order_details = {
            "order_type": "BUY STOP-LIMIT",
            "trigger_price": trigger_price,
            "limit_price": limit_price,
            "stop_loss": sl_to_use,
            "target_1": target_1,
            "target_2": target_2,
            "shares": shares,
            "max_investment": max_investment,
            "risk_amount": round(shares * per_share_risk, 2),
            "risk_reward": risk_reward,
        }

    # 3. Scenario A: GREEN LIGHT (Healthy controlled open holding upper half)
    else:
        verdict = "GREEN_LIGHT"
        reason = f"Healthy controlled open ({gap_pct:+.2f}%) holding firmly in upper {range_pos*100:.0f}% of 15m range. Buyers in control."
        action = "BUY LIMIT ORDER (09:30 - 09:35 AM)"

        limit_price = round(p930 * 1.001, 2)  # small buffer for limit fill
        tight_sl = round(l15 * 0.998, 2)
        sl_to_use = min(base_sl, tight_sl) if base_sl > 0 else tight_sl

        per_share_risk = max(0.05, limit_price - sl_to_use)
        capital_risk = capital * (risk_pct / 100.0)
        shares = int(capital_risk / per_share_risk)
        max_investment = round(shares * limit_price, 2)

        order_details = {
            "order_type": "BUY LIMIT",
            "limit_price": limit_price,
            "stop_loss": sl_to_use,
            "target_1": target_1,
            "target_2": target_2,
            "shares": shares,
            "max_investment": max_investment,
            "risk_amount": round(shares * per_share_risk, 2),
            "risk_reward": risk_reward,
        }

    return {
        "symbol": symbol,
        "verdict": verdict,
        "action": action,
        "reason": reason,
        "prev_close": prev_close,
        "open": open_price,
        "h15": h15,
        "l15": l15,
        "p930": p930,
        "gap_pct": round(gap_pct, 2),
        "change_pct": round(change_pct, 2),
        "range_pos_pct": round(range_pos * 100.0, 1),
        "order": order_details,
    }


def run_morning_check(
    date_str: Optional[str] = None,
    mock: bool = False,
    capital: float = 500000.0,
    risk_pct: float = 1.0,
    top_n: int = 5,
    notify: bool = False,
) -> List[Dict]:
    """Runs the 09:30 morning entry evaluation across top shortlisted candidates."""
    rep_dir = WORKSPACE_ROOT / "data" / "reports"
    rep_path, resolved_date = get_latest_screening_report(rep_dir, date_str)

    with open(rep_path, "r", encoding="utf-8") as f:
        rep_data = json.load(f)

    screened = rep_data.get("screened", [])
    buy_now_all = [s for s in screened if s.get("gate_status") == "BUY NOW"]
    pure_buys = [s for s in buy_now_all if is_pure_equity(s["symbol"])]
    pure_buys.sort(key=lambda x: -x.get("conviction_score", 0))

    candidates = pure_buys[:top_n]
    if not candidates:
        print("No pure equity BUY NOW candidates found in report.")
        return []

    print("=" * 74)
    print(f"  NSE 09:30 AM MORNING SWING ENTRY CHECK (Date Ref: {resolved_date})")
    print(f"  Capital: ₹{capital:,.2f} | Risk per trade: {risk_pct}% (₹{capital*(risk_pct/100):,.2f})")
    print("=" * 74)

    fetcher = NSELiveQuoteFetcher()
    results = []

    # If mock is requested, provide varied realistic scenarios for demonstration
    mock_scenarios = ["green_light", "breakout", "red_light", "green_light", "breakout"]

    for idx, s in enumerate(candidates):
        sym = s["symbol"]
        ts = s.get("trade_setup", {})
        cmp_prev = s.get("cmp", 0.0)
        sl_base = ts.get("stop_loss", 0.0)
        t1 = ts.get("target_1", 0.0)
        t2 = ts.get("target_2", 0.0)
        rr = ts.get("risk_reward_t1", "1:1.33")
        atr = ts.get("atr_14", 0.0)

        # Fetch live 09:30 quote
        mock_arg = mock_scenarios[idx % len(mock_scenarios)] if mock else None
        try:
            quote = fetcher.fetch_quote(sym, mock_scenario=mock_arg, base_price=cmp_prev)
        except Exception as e:
            if not mock:
                # If network fails or outside market hours, fall back to mock gracefully
                print(f"  [!] Live quote unavailable for {sym} ({e}); falling back to simulated scenario.")
                quote = fetcher._generate_mock_quote(sym, mock_scenarios[idx % len(mock_scenarios)], base_price=cmp_prev)
            else:
                raise

        open_p = quote["open"] or cmp_prev
        h15 = quote["day_high"] or cmp_prev
        l15 = quote["day_low"] or cmp_prev
        p930 = quote["last_price"] or cmp_prev
        c_prev = quote["prev_close"] or cmp_prev

        eval_res = evaluate_opening_range(
            symbol=sym,
            prev_close=c_prev,
            open_price=open_p,
            h15=h15,
            l15=l15,
            p930=p930,
            atr14=atr,
            base_sl=sl_base,
            target_1=t1,
            target_2=t2,
            risk_reward=rr,
            capital=capital,
            risk_pct=risk_pct,
        )
        eval_res["conviction_score"] = s.get("conviction_score")
        fo_info = s.get("fo")
        if fo_info:
            eval_res["fo"] = fo_info
        results.append(eval_res)

        # Print Executive Formatted Output
        verdict = eval_res["verdict"]
        badge = "🟢 GREEN LIGHT" if verdict == "GREEN_LIGHT" else ("🟡 BREAKOUT TRIGGER" if verdict == "BREAKOUT_TRIGGER" else "❌ RED LIGHT")

        print(f"\n#{idx+1} {sym} | CMP: ₹{p930:.2f} ({eval_res['change_pct']:+.2f}%) | Prev Close: ₹{c_prev:.2f}")
        print(f"    Verdict: [{badge}] -> {eval_res['action']}")
        if fo_info:
            print(f"    Derivatives: {fo_info['buildup']} (OI: {fo_info['fut_oi_change_pct']:+0.1f}%) | "
                  f"Call Wall: ₹{fo_info['call_wall']} | Put Wall: ₹{fo_info['put_wall']} | PCR: {fo_info['pcr_oi']}")
        print(f"    15m Range: Low ₹{l15:.2f} — High ₹{h15:.2f} | Position: {eval_res['range_pos_pct']}% of range | Gap: {eval_res['gap_pct']:+.2f}%")
        print(f"    Analysis: {eval_res['reason']}")

        ord_info = eval_res["order"]
        if ord_info:
            if ord_info["order_type"] == "BUY STOP-LIMIT":
                print(f"    >>> ORDER DETAILS: {ord_info['order_type']}")
                print(f"        • Trigger Price: ₹{ord_info['trigger_price']} | Limit Price: ₹{ord_info['limit_price']}")
                print(f"        • Stop Loss: ₹{ord_info['stop_loss']} | Target 1: ₹{ord_info['target_1']} | Target 2: ₹{ord_info['target_2']}")
                print(f"        • Position Size: {ord_info['shares']} shares (~₹{ord_info['max_investment']:,.2f}) | Risk: ₹{ord_info['risk_amount']:,.2f}")
            else:
                print(f"    >>> ORDER DETAILS: {ord_info['order_type']}")
                print(f"        • Limit Entry: ₹{ord_info['limit_price']}")
                print(f"        • Stop Loss: ₹{ord_info['stop_loss']} | Target 1: ₹{ord_info['target_1']} | Target 2: ₹{ord_info['target_2']}")
                print(f"        • Position Size: {ord_info['shares']} shares (~₹{ord_info['max_investment']:,.2f}) | Risk: ₹{ord_info['risk_amount']:,.2f}")

    print("\n" + "=" * 74)
    print("  Execution Summary:")
    green_count = sum(1 for r in results if r["verdict"] == "GREEN_LIGHT")
    breakout_count = sum(1 for r in results if r["verdict"] == "BREAKOUT_TRIGGER")
    red_count = sum(1 for r in results if r["verdict"] == "RED_LIGHT")
    print(f"  • GREEN LIGHT (Immediate Limit Buy)   : {green_count}")
    print(f"  • BREAKOUT TRIGGER (Stop-Limit Orders) : {breakout_count}")
    print(f"  • RED LIGHT (Skipped Today)            : {red_count}")
    print("=" * 74 + "\n")

    # Save output report JSON
    out_json = rep_dir / f"morning_entry_{resolved_date}.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"date": resolved_date, "capital": capital, "risk_pct": risk_pct, "evaluations": results}, f, indent=2)

    if notify:
        from pipeline.tools.notify import dispatch_morning_alerts
        print("  [>] Dispatching mobile alerts via Telegram & Email...")
        dispatch_morning_alerts(results, resolved_date, capital, risk_pct)

    return results


def main():
    parser = argparse.ArgumentParser(description="Post-09:30 AM Opening Range Decision Engine")
    parser.add_argument("--date", type=str, default=None, help="Reference screening date (YYYY-MM-DD)")
    parser.add_argument("--mock", action="store_true", help="Use simulated 09:30 quote scenarios for dry-run")
    parser.add_argument("--capital", type=float, default=500000.0, help="Total trading capital in INR (default ₹5,00,000)")
    parser.add_argument("--risk-pct", type=float, default=1.0, help="Max risk percentage per trade (default 1.0 percent)")
    parser.add_argument("--top", type=int, default=5, help="Number of shortlisted stocks to evaluate (default 5)")
    parser.add_argument("--notify", action="store_true", help="Dispatch alerts to Telegram and Email")
    args = parser.parse_args()

    run_morning_check(
        date_str=args.date,
        mock=args.mock,
        capital=args.capital,
        risk_pct=args.risk_pct,
        top_n=args.top,
        notify=args.notify,
    )


if __name__ == "__main__":
    main()
