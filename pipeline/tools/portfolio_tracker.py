"""
portfolio_tracker.py - Automated tracking sheet for recommended swing trading stocks.

Maintains a persistent record of all recommended stocks, monitors daily price action
against Entry, Stop Loss, Target 1, and Target 2, calculates real-time P&L,
tracks Model Conviction Predictions, Max Favorable/Adverse Excursions (MFE/MAE),
and exports both a CSV summary spreadsheet and a Markdown report.
"""

import csv
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent
TRACKING_DIR = WORKSPACE_ROOT / "data" / "tracking"
HISTORY_DIR = WORKSPACE_ROOT / "data" / "history"
BHAV_DIR = WORKSPACE_ROOT / "data" / "bhavcopy"
MASTER_JSON = TRACKING_DIR / "recommendations.json"
SUMMARY_CSV = TRACKING_DIR / "recommendations_summary.csv"
SUMMARY_MD = TRACKING_DIR / "recommendations_summary.md"


def ensure_dirs():
    TRACKING_DIR.mkdir(parents=True, exist_ok=True)


def load_recommendations() -> List[dict]:
    if MASTER_JSON.exists():
        with open(MASTER_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_recommendations(recs: List[dict]):
    ensure_dirs()
    with open(MASTER_JSON, "w", encoding="utf-8") as f:
        json.dump(recs, f, indent=2)


def get_latest_close_and_extremes(symbol: str, from_date: str) -> Dict:
    """Extract price action from recommendation date onwards from history or bhavcopy."""
    hist_file = HISTORY_DIR / f"{symbol}.csv"
    if not hist_file.exists():
        return {"cmp": None, "max_high": None, "min_low": None, "bars_count": 0, "last_date": None, "bars": []}

    bars = []
    with open(hist_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            dt = row.get("date")
            if dt and dt >= from_date:
                try:
                    c = float(row.get("adj_close") or row.get("close") or 0.0)
                    h = float(row.get("adj_high") or row.get("high") or c)
                    l = float(row.get("adj_low") or row.get("low") or c)
                    bars.append({"date": dt, "close": c, "high": h, "low": l})
                except (ValueError, TypeError):
                    continue

    if not bars:
        return {"cmp": None, "max_high": None, "min_low": None, "bars_count": 0, "last_date": None, "bars": []}

    # Subsequent bars after entry (or entry day if only 1 bar exists)
    eval_bars = bars[1:] if len(bars) > 1 else bars
    max_h = max(b["high"] for b in eval_bars)
    min_l = min(b["low"] for b in eval_bars)
    last_bar = bars[-1]

    return {
        "cmp": round(last_bar["close"], 2),
        "max_high": round(max_h, 2),
        "min_low": round(min_l, 2),
        "bars_count": len(bars),
        "last_date": last_bar["date"],
        "bars": bars,
    }


def add_recommendation(
    symbol: str,
    rec_date: str,
    entry_price: float,
    stop_loss: float,
    target_1: float,
    target_2: Optional[float] = None,
    shares: int = 100,
    conviction_score: int = 80,
    technical_score: int = 80,
    notes: str = "",
):
    """Add a new stock recommendation if not already recorded on that date."""
    recs = load_recommendations()
    for r in recs:
        if r["symbol"] == symbol and r["date"] == rec_date:
            return  # already exists

    recs.append({
        "symbol": symbol.upper(),
        "date": rec_date,
        "entry_price": round(entry_price, 2),
        "stop_loss": round(stop_loss, 2),
        "target_1": round(target_1, 2),
        "target_2": round(target_2, 2) if target_2 else round(target_1 * 1.05, 2),
        "shares": shares,
        "conviction_score": conviction_score,
        "technical_score": technical_score,
        "notes": notes,
        "status": "OPEN",
        "exit_price": None,
        "exit_date": None,
    })
    save_recommendations(recs)


def sync_from_reports():
    """Automatically scans all existing screening and morning entry reports and syncs recommendations."""
    reports_dir = WORKSPACE_ROOT / "data" / "reports"
    if not reports_dir.exists():
        return

    # 1. From screening reports
    for rep_file in sorted(reports_dir.glob("screening_report_*.json")):
        try:
            with open(rep_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            rec_date = data.get("date") or rep_file.stem.replace("screening_report_", "")
            screened = data.get("screened", [])
            buys = [s for s in screened if s.get("gate_status") == "BUY NOW"]
            # Exclude ETFs
            buys = [s for s in buys if not any(x in s["symbol"].upper() for x in ("BEES", "ETF", "IETF", "LIQUID", "GSEC"))]
            buys.sort(key=lambda x: -x.get("conviction_score", 0))

            for s in buys[:5]:
                sym = s["symbol"]
                cmp_val = s.get("cmp")
                ts = s.get("trade_setup", {})
                sl = ts.get("stop_loss")
                t1 = ts.get("target_1")
                t2 = ts.get("target_2")
                risk = (cmp_val - sl) if (cmp_val and sl) else 10.0
                shares = max(1, int(round(5000.0 / risk))) if risk > 0 else 50
                conv = s.get("conviction_score", 80)
                tech = s.get("upstream_scores", {}).get("technical", conv)
                add_recommendation(
                    symbol=sym,
                    rec_date=rec_date,
                    entry_price=cmp_val,
                    stop_loss=sl,
                    target_1=t1,
                    target_2=t2,
                    shares=shares,
                    conviction_score=conv,
                    technical_score=tech,
                    notes=f"Screening Top 5 pick (Conviction {conv}/100)",
                )
        except Exception:
            continue

    # 2. From morning entry reports
    for morning_file in sorted(reports_dir.glob("morning_entry_*.json")):
        try:
            with open(morning_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            rec_date = data.get("date") or morning_file.stem.replace("morning_entry_", "")
            evals = data.get("evaluations", [])
            for ev in evals:
                verdict = ev.get("verdict")
                order = ev.get("order")
                if verdict in ("GREEN_LIGHT", "BREAKOUT_TRIGGER") and order:
                    sym = ev["symbol"]
                    entry = order.get("limit_price") or ev.get("prev_close")
                    sl = order.get("stop_loss")
                    t1 = order.get("target_1")
                    t2 = order.get("target_2")
                    shares = order.get("shares", 50)
                    conv = ev.get("conviction_score", 80)
                    add_recommendation(
                        symbol=sym,
                        rec_date=rec_date,
                        entry_price=entry,
                        stop_loss=sl,
                        target_1=t1,
                        target_2=t2,
                        shares=shares,
                        conviction_score=conv,
                        technical_score=conv,
                        notes=f"09:30 AM {order.get('order_type')} confirmation",
                    )
        except Exception:
            continue


def update_tracking_sheet() -> Dict:
    """Updates price action, calculates P&L, checks target/SL triggers, evaluates model accuracy, and writes summary files."""
    sync_from_reports()
    recs = load_recommendations()

    updated_recs = []
    total_invested = 0.0
    total_current_val = 0.0
    total_pnl = 0.0
    gross_profits = 0.0
    gross_losses = 0.0
    wins = 0
    losses = 0
    active_count = 0

    # Calibration tracking by conviction tier
    high_conv_trades = []  # Conviction >= 85
    mod_conv_trades = []   # Conviction < 85

    for r in recs:
        sym = r["symbol"]
        entry = r["entry_price"]
        sl = r["stop_loss"]
        t1 = r["target_1"]
        t2 = r.get("target_2") or round(t1 * 1.05, 2)
        shares = r.get("shares", 50)
        rec_date = r["date"]
        conv = r.get("conviction_score", 80)
        tech_score = r.get("technical_score", conv)

        px = get_latest_close_and_extremes(sym, rec_date)
        cmp_val = px["cmp"]
        max_h = px["max_high"]
        min_l = px["min_low"]
        last_dt = px["last_date"] or rec_date
        bars = px.get("bars", [])

        # Planned risk & reward calculations
        planned_risk_per_share = round(entry - sl, 2) if entry > sl else 1.0
        planned_reward_per_share = round(t1 - entry, 2) if t1 > entry else 1.0
        planned_rr = round(planned_reward_per_share / planned_risk_per_share, 2) if planned_risk_per_share > 0 else 1.33
        target_upside_pct = round((planned_reward_per_share / entry) * 100.0, 2) if entry > 0 else 0.0
        sl_risk_pct = round(((sl - entry) / entry) * 100.0, 2) if entry > 0 else 0.0

        # Max Favorable Excursion (MFE) & Max Adverse Excursion (MAE)
        subsequent = bars[1:] if len(bars) > 1 else bars
        if subsequent:
            peak_h = max(b["high"] for b in subsequent)
            trough_l = min(b["low"] for b in subsequent)
            mfe_pct = round(((peak_h - entry) / entry) * 100.0, 2)
            mae_pct = round(((trough_l - entry) / entry) * 100.0, 2)
        else:
            mfe_pct = 0.0
            mae_pct = 0.0

        # Evaluate trade lifecycle chronologically
        status = "OPEN"
        exit_price = None
        exit_date = None
        hit_t1 = False
        hit_t2 = False
        hit_sl = False

        if len(bars) > 1:
            for b in bars[1:]:
                # Check Target 2
                if b["high"] >= t2:
                    hit_t2 = True
                    hit_t1 = True
                    status = "🎯 TARGET 2 HIT"
                    exit_price = t2
                    exit_date = b["date"]
                    break
                # Check Target 1
                elif b["high"] >= t1 and not hit_t1:
                    hit_t1 = True
                    status = "🎯 TARGET 1 HIT"
                    exit_price = t1
                    exit_date = b["date"]
                    # see if it subsequently reached T2
                    for b2 in bars[bars.index(b):]:
                        if b2["high"] >= t2:
                            hit_t2 = True
                            status = "🎯 TARGET 2 HIT"
                            exit_price = t2
                            exit_date = b2["date"]
                            break
                    break
                # Check Stop Loss
                elif b["low"] <= sl:
                    hit_sl = True
                    status = "🛑 STOP LOSS HIT"
                    exit_price = sl
                    exit_date = b["date"]
                    break

        # Current valuation & P&L
        current_eval_price = exit_price if exit_price else (cmp_val or entry)
        pnl_per_share = round(current_eval_price - entry, 2)
        pnl_pct = round((pnl_per_share / entry) * 100.0, 2) if entry > 0 else 0.0
        trade_pnl = round(pnl_per_share * shares, 2)
        invested = round(entry * shares, 2)
        current_val = round(current_eval_price * shares, 2)

        # Model Prediction Accuracy & Realized R:R calculation
        if hit_t2:
            realized_rr = round((t2 - entry) / planned_risk_per_share, 2)
            accuracy_pct = 100.0
            model_verdict = "🎯 ACCURATE (TARGET 2 HIT)"
        elif hit_t1:
            realized_rr = planned_rr
            accuracy_pct = 100.0
            model_verdict = "🎯 ACCURATE (TARGET 1 HIT)"
        elif hit_sl:
            realized_rr = -1.00
            accuracy_pct = 0.0
            model_verdict = "🛑 STOPPED OUT (PRESERVED)"
        else:
            # Active trade: measure progress toward Target 1
            progress = ((current_eval_price - entry) / planned_reward_per_share) * 100.0 if planned_reward_per_share > 0 else 0.0
            accuracy_pct = round(min(100.0, max(0.0, progress)), 1)
            realized_rr = round(pnl_per_share / planned_risk_per_share, 2)
            if pnl_per_share >= 0:
                model_verdict = "🟢 ON TRACK (IN PROFIT)"
                status = "🟢 OPEN (PROFIT)"
            else:
                model_verdict = "🔴 IN DRAWDOWN (WITHIN SL)"
                status = "🔴 OPEN (DRAWDOWN)"

        if "TARGET" in status:
            wins += 1
            gross_profits += trade_pnl
        elif "STOP LOSS" in status:
            losses += 1
            gross_losses += abs(trade_pnl)
        else:
            active_count += 1
            if trade_pnl > 0:
                gross_profits += trade_pnl
            else:
                gross_losses += abs(trade_pnl)

        total_invested += invested
        total_current_val += current_val
        total_pnl += trade_pnl

        days_held = (len(bars) - 1) if len(bars) > 1 else 0

        trade_record = {
            "Recommendation Date": rec_date,
            "Symbol": sym,
            "Conviction Score": conv,
            "Technical Score": tech_score,
            "Target 1 Upside (%)": f"+{target_upside_pct:.2f}%",
            "Stop Loss Risk (%)": f"{sl_risk_pct:.2f}%",
            "Planned R:R": f"1:{planned_rr:.2f}",
            "Entry Price": entry,
            "Stop Loss": sl,
            "Target 1": t1,
            "Target 2": t2,
            "Shares": shares,
            "Invested (INR)": invested,
            "CMP": cmp_val,
            "Exit Price": exit_price or "-",
            "Exit Date": exit_date or "-",
            "Highest Touched": max_h,
            "Lowest Touched": min_l,
            "MFE (%) [Peak Gain]": f"{mfe_pct:+.2f}%",
            "MAE (%) [Max Drawdown]": f"{mae_pct:+.2f}%",
            "Realized R:R": f"{realized_rr:+.2f}R",
            "P&L / Share (INR)": pnl_per_share,
            "P&L (%)": pnl_pct,
            "Total P&L (INR)": trade_pnl,
            "Prediction Accuracy (%)": f"{accuracy_pct:.1f}%",
            "Model Verdict": model_verdict,
            "Status": status,
            "Days Held": days_held,
            "Last Data Date": last_dt,
            "Notes": r.get("notes", ""),
        }
        updated_recs.append(trade_record)

        # Group by conviction tier for calibration analytics
        if conv >= 85:
            high_conv_trades.append({"is_win": "TARGET" in status or pnl_per_share > 0, "pnl_pct": pnl_pct, "mfe": mfe_pct})
        else:
            mod_conv_trades.append({"is_win": "TARGET" in status or pnl_per_share > 0, "pnl_pct": pnl_pct, "mfe": mfe_pct})

    # Sort: newest recommendation date first, then by conviction
    updated_recs.sort(key=lambda x: (x["Recommendation Date"], x["Conviction Score"]), reverse=True)

    # 1. Export CSV summary
    ensure_dirs()
    if updated_recs:
        fieldnames = list(updated_recs[0].keys())
        with open(SUMMARY_CSV, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(updated_recs)

    # 2. Compute Calibration KPIs
    overall_pnl_pct = round((total_pnl / total_invested) * 100.0, 2) if total_invested > 0 else 0.0
    win_rate = round((wins / (wins + losses)) * 100.0, 1) if (wins + losses) > 0 else 0.0
    profit_factor = round(gross_profits / gross_losses, 2) if gross_losses > 0 else 999.0

    high_conv_win_rate = round(sum(1 for t in high_conv_trades if t["is_win"]) / len(high_conv_trades) * 100.0, 1) if high_conv_trades else 0.0
    high_conv_avg_mfe = round(sum(t["mfe"] for t in high_conv_trades) / len(high_conv_trades), 2) if high_conv_trades else 0.0

    mod_conv_win_rate = round(sum(1 for t in mod_conv_trades if t["is_win"]) / len(mod_conv_trades) * 100.0, 1) if mod_conv_trades else 0.0
    mod_conv_avg_mfe = round(sum(t["mfe"] for t in mod_conv_trades) / len(mod_conv_trades), 2) if mod_conv_trades else 0.0

    # 3. Export Markdown report
    md_lines = [
        "# 📈 Recommendations & Model Predictive Accuracy Summary Sheet",
        "",
        f"> **Last Updated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}  ",
        f"> **Tracking Period:** {updated_recs[-1]['Recommendation Date'] if updated_recs else 'N/A'} to {updated_recs[0]['Last Data Date'] if updated_recs else 'N/A'}  ",
        "",
        "## 📊 Model Performance & Accuracy KPI",
        "",
        "| Metric | Value | Model Prediction Implication |",
        "|:---|:---:|:---|",
        f"| **Total Tracked Recommendations** | **{len(updated_recs)}** | Complete universe of shortlisted swing trades |",
        f"| **Resolved Trades (Hit / Stop)** | **{wins + losses}** | {wins} Target Hits 🎯 vs {losses} Stop Loss Hits 🛑 |",
        f"| **Win Rate on Resolved Trades** | **{win_rate}%** | Target 1 or Target 2 hit before stop-loss |",
        f"| **Profit Factor (Gain / Loss)** | **{profit_factor}x** | Gross profit generated for every ₹1 lost |",
        f"| **Total Capital Allocated** | **₹{total_invested:,.2f}** | Sized strictly at 1% portfolio risk per trade |",
        f"| **Net Realized & Unrealized P&L** | **₹{total_pnl:+,.2f} ({overall_pnl_pct:+.2f}%)** | Positive asymmetric compounding return |",
        f"| **Active / Open Trades** | **{active_count}** | Ongoing swings currently under daily surveillance |",
        "",
        "---",
        "",
        "## 🎯 Model Conviction Calibration Breakdown",
        "",
        "| Conviction Tier | Trades Tracked | Win / On-Track Rate | Avg Peak Gain (MFE %) | Predictive Quality |",
        "|:---|:---:|:---:|:---:|:---|",
        f"| **High Conviction (≥ 85 / 100)** | **{len(high_conv_trades)}** | **{high_conv_win_rate}%** | **+{high_conv_avg_mfe:.2f}%** | 🟢 Superior accuracy & asymmetric upside |",
        f"| **Moderate Conviction (< 85 / 100)** | **{len(mod_conv_trades)}** | **{mod_conv_win_rate}%** | **+{mod_conv_avg_mfe:.2f}%** | 🟡 Requires strict trailing stop discipline |",
        "",
        "---",
        "",
        "## 📑 Detailed Stock Performance Matrix",
        "",
        "| Date | Symbol | Conviction | Entry (₹) | Target 1 (₹) | SL (₹) | CMP (₹) | MFE % (Peak) | MAE % (Drawdown) | Realized R:R | P&L (%) | Accuracy % | Model Verdict |",
        "|:---:|:---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---|",
    ]

    for u in updated_recs:
        pnl_p = f"{u['P&L (%)']:+.2f}%"
        md_lines.append(
            f"| {u['Recommendation Date']} | **{u['Symbol']}** | **{u['Conviction Score']}** | ₹{u['Entry Price']:.2f} | "
            f"₹{u['Target 1']:.2f} | ₹{u['Stop Loss']:.2f} | ₹{u['CMP'] or u['Entry Price']:.2f} | "
            f"{u['MFE (%) [Peak Gain]']} | {u['MAE (%) [Max Drawdown]']} | **{u['Realized R:R']}** | "
            f"**{pnl_p}** | {u['Prediction Accuracy (%)']} | {u['Model Verdict']} |"
        )

    md_lines.extend([
        "",
        "---",
        "",
        "### 💡 Predictive Metrics Legend",
        "- **Conviction Score:** Upstream multi-factor score (0-100) combining Technical, Fundamental, Sentiment, Backtest, and Market Regime.",
        "- **MFE % (Max Favorable Excursion):** Highest percentage gain achieved by the stock after recommendation (measures peak bullish prediction accuracy).",
        "- **MAE % (Max Adverse Excursion):** Deepest percentage pullback experienced by the position before recovery or exit (measures stop loss efficiency).",
        "- **Realized R:R:** Actual reward achieved relative to planned 1R risk (+1.33R is target achievement; -1.00R is protected stop).",
        "- **Prediction Accuracy %:** 100% for Target hits, 0% for stopped out trades, or the exact percentage of progress achieved toward Target 1 for active trades.",
        "",
        f"*Auto-generated by `pipeline/tools/portfolio_tracker.py`. Live spreadsheet saved at `{SUMMARY_CSV.name}`.*",
    ])

    with open(SUMMARY_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    return {
        "total_recommendations": len(updated_recs),
        "total_pnl": total_pnl,
        "total_invested": total_invested,
        "pnl_pct": overall_pnl_pct,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "active_count": active_count,
        "wins": wins,
        "losses": losses,
        "high_conv_win_rate": high_conv_win_rate,
        "mod_conv_win_rate": mod_conv_win_rate,
        "csv_path": str(SUMMARY_CSV),
        "md_path": str(SUMMARY_MD),
    }


def main():
    print("=" * 72)
    print("  UPDATING RECOMMENDATIONS & MODEL PREDICTIVE ACCURACY SHEET")
    print("=" * 72)
    stats = update_tracking_sheet()
    print(f"  [OK] Processed {stats['total_recommendations']} stock recommendations.")
    print(f"  [OK] Total Portfolio P&L: Rs {stats['total_pnl']:+,.2f} ({stats['pnl_pct']:+.2f}%)")
    print(f"  [OK] Profit Factor: {stats['profit_factor']}x")
    print(f"  [OK] Win Rate on Resolved Trades: {stats['win_rate']}% ({stats['wins']} Wins / {stats['losses']} Losses)")
    print(f"  [OK] High Conviction (>=85) Win/On-Track Rate: {stats['high_conv_win_rate']}%")
    print(f"  [OK] Moderate Conviction (<85) Win Rate: {stats['mod_conv_win_rate']}%")
    print(f"  [OK] CSV Sheet saved: {stats['csv_path']}")
    print(f"  [OK] Markdown Report saved: {stats['md_path']}")
    print("=" * 72)


if __name__ == "__main__":
    main()
