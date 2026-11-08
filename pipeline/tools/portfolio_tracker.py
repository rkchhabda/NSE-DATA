"""
portfolio_tracker.py - Automated tracking sheet for recommended swing trading stocks.

Maintains a persistent record of all recommended stocks, monitors daily price action
against Entry, Stop Loss, Target 1, and Target 2, calculates real-time P&L,
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
        return {"cmp": None, "max_high": None, "min_low": None, "bars_count": 0, "last_date": None}

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
        return {"cmp": None, "max_high": None, "min_low": None, "bars_count": 0, "last_date": None}

    # Subsequent bars after entry
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
                add_recommendation(
                    symbol=sym,
                    rec_date=rec_date,
                    entry_price=cmp_val,
                    stop_loss=sl,
                    target_1=t1,
                    target_2=t2,
                    shares=shares,
                    conviction_score=conv,
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
                        notes=f"09:30 AM {order.get('order_type')} confirmation",
                    )
        except Exception:
            continue


def update_tracking_sheet() -> Dict:
    """Updates price action, calculates P&L, checks target/SL triggers, and writes summary files."""
    sync_from_reports()
    recs = load_recommendations()

    updated_recs = []
    total_invested = 0.0
    total_current_val = 0.0
    total_pnl = 0.0
    wins = 0
    losses = 0
    active_count = 0

    for r in recs:
        sym = r["symbol"]
        entry = r["entry_price"]
        sl = r["stop_loss"]
        t1 = r["target_1"]
        t2 = r.get("target_2") or (t1 * 1.05)
        shares = r.get("shares", 50)
        rec_date = r["date"]

        px = get_latest_close_and_extremes(sym, rec_date)
        cmp_val = px["cmp"]
        max_h = px["max_high"]
        min_l = px["min_low"]
        last_dt = px["last_date"] or rec_date
        bars = px.get("bars", [])

        # Evaluate trade lifecycle
        status = "OPEN"
        exit_price = None
        exit_date = None

        # Check subsequent days chronologically for trigger
        if len(bars) > 1:
            for b in bars[1:]:
                # Did it hit target 2?
                if b["high"] >= t2:
                    status = "🎯 TARGET 2 HIT"
                    exit_price = t2
                    exit_date = b["date"]
                    break
                # Did it hit target 1?
                elif b["high"] >= t1 and status == "OPEN":
                    status = "🎯 TARGET 1 HIT"
                    exit_price = t1
                    exit_date = b["date"]
                    # continue checking if it also reached target 2 later
                    for b2 in bars[bars.index(b):]:
                        if b2["high"] >= t2:
                            status = "🎯 TARGET 2 HIT"
                            exit_price = t2
                            exit_date = b2["date"]
                            break
                    break
                # Did it hit stop loss?
                elif b["low"] <= sl:
                    status = "🛑 STOP LOSS HIT"
                    exit_price = sl
                    exit_date = b["date"]
                    break

        # Calculate current valuation
        current_eval_price = exit_price if exit_price else (cmp_val or entry)
        pnl_per_share = round(current_eval_price - entry, 2)
        pnl_pct = round((pnl_per_share / entry) * 100.0, 2) if entry > 0 else 0.0
        trade_pnl = round(pnl_per_share * shares, 2)
        invested = round(entry * shares, 2)
        current_val = round(current_eval_price * shares, 2)

        if "TARGET" in status:
            wins += 1
        elif "STOP LOSS" in status:
            losses += 1
        else:
            active_count += 1
            if pnl_per_share >= 0:
                status = "🟢 OPEN (PROFIT)"
            else:
                status = "🔴 OPEN (DRAWDOWN)"

        total_invested += invested
        total_current_val += current_val
        total_pnl += trade_pnl

        days_held = (len(bars) - 1) if len(bars) > 1 else 0

        updated_recs.append({
            "Recommendation Date": rec_date,
            "Symbol": sym,
            "Conviction": r.get("conviction_score", 80),
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
            "P&L / Share (INR)": pnl_per_share,
            "P&L (%)": pnl_pct,
            "Total P&L (INR)": trade_pnl,
            "Status": status,
            "Days Held": days_held,
            "Last Data Date": last_dt,
            "Notes": r.get("notes", ""),
        })

    # Sort: newest recommendation date first, then by conviction
    updated_recs.sort(key=lambda x: (x["Recommendation Date"], x["Conviction"]), reverse=True)

    # 1. Export CSV summary
    ensure_dirs()
    if updated_recs:
        fieldnames = list(updated_recs[0].keys())
        with open(SUMMARY_CSV, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(updated_recs)

    # 2. Export Markdown report
    overall_pnl_pct = round((total_pnl / total_invested) * 100.0, 2) if total_invested > 0 else 0.0
    win_rate = round((wins / (wins + losses)) * 100.0, 1) if (wins + losses) > 0 else 0.0

    md_lines = [
        "# 📈 Recommendations & Performance Summary Sheet",
        "",
        f"> **Last Updated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}  ",
        f"> **Tracking Period:** {updated_recs[-1]['Recommendation Date'] if updated_recs else 'N/A'} to {updated_recs[0]['Last Data Date'] if updated_recs else 'N/A'}  ",
        "",
        "## 📊 Portfolio Performance KPI",
        "",
        "| Metric | Value |",
        "|:---|:---|",
        f"| **Total Tracked Recommendations** | **{len(updated_recs)}** |",
        f"| **Closed / Resolved Trades** | **{wins + losses}** ({wins} Target Hit, {losses} Stop Loss Hit) |",
        f"| **Active / Open Trades** | **{active_count}** |",
        f"| **Win Rate (Resolved)** | **{win_rate}%** |",
        f"| **Total Capital Invested** | **₹{total_invested:,.2f}** |",
        f"| **Net Realized & Unrealized P&L** | **₹{total_pnl:+,.2f} ({overall_pnl_pct:+.2f}%)** |",
        "",
        "---",
        "",
        "## 📑 Detailed Stock Performance Matrix",
        "",
        "| Date | Symbol | Entry (₹) | SL (₹) | Target (₹) | CMP (₹) | P&L / Share | P&L (%) | Total P&L (₹) | Status | Days Held |",
        "|:---:|:---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---|:---:|",
    ]

    for u in updated_recs:
        pnl_s = f"{u['P&L / Share (INR)']:+.2f}"
        pnl_p = f"{u['P&L (%)']:+.2f}%"
        tot_p = f"₹{u['Total P&L (INR)']:+,.2f}"
        md_lines.append(
            f"| {u['Recommendation Date']} | **{u['Symbol']}** | ₹{u['Entry Price']:.2f} | ₹{u['Stop Loss']:.2f} | "
            f"₹{u['Target 1']:.2f} | ₹{u['CMP'] or u['Entry Price']:.2f} | {pnl_s} | **{pnl_p}** | **{tot_p}** | "
            f"{u['Status']} | {u['Days Held']} |"
        )

    md_lines.extend([
        "",
        "---",
        "",
        "### 💡 Status Legend",
        "- 🎯 **TARGET 1 / 2 HIT:** Price touched or surpassed profit target.",
        "- 🛑 **STOP LOSS HIT:** Trade closed at defensive stop level to preserve capital.",
        "- 🟢 **OPEN (PROFIT):** Position active and trading in positive territory.",
        "- 🔴 **OPEN (DRAWDOWN):** Position active, currently within risk tolerance band.",
        "",
        f"*Auto-generated by `pipeline/tools/portfolio_tracker.py`. File stored at `{SUMMARY_CSV.name}`.*",
    ])

    with open(SUMMARY_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    return {
        "total_recommendations": len(updated_recs),
        "total_pnl": total_pnl,
        "total_invested": total_invested,
        "pnl_pct": overall_pnl_pct,
        "win_rate": win_rate,
        "active_count": active_count,
        "wins": wins,
        "losses": losses,
        "csv_path": str(SUMMARY_CSV),
        "md_path": str(SUMMARY_MD),
    }


def main():
    print("=" * 72)
    print("  UPDATING RECOMMENDATIONS & PERFORMANCE SUMMARY SHEET")
    print("=" * 72)
    stats = update_tracking_sheet()
    print(f"  [OK] Processed {stats['total_recommendations']} stock recommendations.")
    print(f"  [OK] Total Portfolio P&L: Rs {stats['total_pnl']:+,.2f} ({stats['pnl_pct']:+.2f}%)")
    print(f"  [OK] Win Rate on Resolved Trades: {stats['win_rate']}% ({stats['wins']} Wins / {stats['losses']} Losses)")
    print(f"  [OK] Active Positions: {stats['active_count']}")
    print(f"  [OK] CSV Sheet saved: {stats['csv_path']}")
    print(f"  [OK] Markdown Report saved: {stats['md_path']}")
    print("=" * 72)


if __name__ == "__main__":
    main()
