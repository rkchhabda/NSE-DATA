#!/usr/bin/env python3
"""
gate_check.py - deterministic Data Confidence Score + BUY NOW gate.

Reads an evidence JSON (see assets/sample_input.json), validates every critical
metric across independent source origins, computes the Data Confidence Score,
applies the BUY NOW eligibility rule, and renders a report.

Usage:
    python scripts/gate_check.py INPUT.json [--health HEALTH.json]
                                 [--format md|json] [--out FILE]

Standard library only. Thresholds, weights and tolerances live in CONFIG, TOL
and PRIORITY below; references/scoring-spec.md documents them.
"""
import argparse
import json
import sys
from statistics import mean

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
CONFIG = {
    "thresholds": {
        "market_regime": 35, "technical": 75, "fundamental": 65,
        "sentiment": 60, "backtest": 60, "confidence": 80,
    },
    "weights": {
        "cmp": 25, "volume": 15, "indicators": 15, "fundamentals": 15,
        "history": 10, "ownership": 10, "backtest_data": 10,
    },
    "status_scores": {
        "VERIFIED": 100, "VERIFIED_PARTIAL": 90, "VERIFIED_DUAL_OFFICIAL": 90,
        "SINGLE_OFFICIAL": 70, "COMPUTED_ONLY": 70, "WEAK": 55, "SINGLE_VENDOR": 40,
        "SINGLE_UNOFFICIAL": 30, "CONFLICT": 15, "MISSING": 0,
    },
    "expected_indicators": ["rsi14", "macd", "atr14", "ema20", "ema50", "sma200"],
    "expected_fundamentals": ["roe", "roce", "debt_equity", "revenue_growth",
                              "profit_growth", "ebitda_margin", "pe", "pb"],
    "expected_ownership": ["promoter_holding", "promoter_pledge",
                           "fii_holding", "dii_holding"],
    "min_bars_indicators": 260,
    "min_bars_backtest": 750,
    "min_signals_backtest": 30,
    "fundamentals_stale_days": 30,
    "fundamentals_stale_multiplier": 0.9,
    "allow_computed_only_indicators": False,
    # Volume corroboration rule: "A" (strict: independent origin required) or
    # "B" (NSE dual official files sec_bhavdata + udiff verify volume with label VERIFIED_DUAL_OFFICIAL)
    "volume_rule": "B",
    # Cross-exchange price corroboration (chosen from NSE vs BSE closes on 2026-10-30, 2,459 stocks):
    # BSE must have traded at least this many times, and agree with NSE within this percent.
    "cross_exchange_pct": 0.50,
    "min_bse_trades": 100,
    "penalties": [(40, -20), (60, -10), (80, -5)],  # (confidence < x, penalty)
}

# source -> (origin group, class)
SOURCES = {
    "nse_bhavcopy": ("nse", "official"),
    "nse_udiff": ("nse", "official"),
    "nse_api": ("nse", "official"),
    "nse_shareholding": ("nse", "official"),
    "bse_api": ("bse", "official"),
    "bse_bhavcopy": ("bse", "official"),
    "bse_filings": ("bse", "official"),
    "company_filings": ("filings", "official"),
    "nsdl": ("nsdl", "official"),
    "twelve_data": ("twelve_data", "vendor"),
    "alpha_vantage": ("alpha_vantage", "vendor"),
    "fmp": ("fmp", "vendor"),
    "screener": ("screener", "vendor"),
    "tickertape": ("tickertape", "vendor"),
    "trendlyne": ("trendlyne", "vendor"),
    "yahoo": ("yahoo", "unofficial"),
    "internal": ("internal", "computed"),
}

PRIORITY = {
    "price": ["nse_bhavcopy", "nse_api", "bse_bhavcopy", "bse_api", "twelve_data",
              "alpha_vantage", "yahoo"],
    "volume": ["nse_bhavcopy", "nse_udiff", "twelve_data", "alpha_vantage", "yahoo"],
    "indicator": ["internal", "twelve_data", "alpha_vantage", "yahoo"],
    "fundamental": ["company_filings", "screener", "tickertape", "trendlyne", "fmp"],
    "ownership": ["nse_shareholding", "bse_filings", "trendlyne", "tickertape"],
}

# (percent, absolute floor): agree if |a-b| <= max(floor, pct% of |b|)
TOL = {
    "cmp_eod": (0.10, 0.0), "cmp_live": (0.30, 0.0), "volume": (2.0, 0.0),
    "rsi": (0.0, 1.0), "macd": (3.0, 0.05), "atr": (2.0, 0.0),
    "ema": (0.5, 0.0), "sma": (0.5, 0.0),
    "pe": (5.0, 0.5), "pb": (5.0, 0.1), "debt_equity": (10.0, 0.05),
    "promoter_holding": (0.0, 0.5), "promoter_pledge": (0.0, 0.5),
    "fii_holding": (0.0, 0.5), "dii_holding": (0.0, 0.5),
    "default": (10.0, 1.0),
}

HEALTH_SOURCES = [
    ("NSE API", "nse_api"), ("Bhavcopy", "bhavcopy"), ("Screener", "screener"),
    ("Trendlyne", "trendlyne"), ("Tickertape", "tickertape"),
    ("Twelve Data", "twelve_data"), ("Alpha Vantage", "alpha_vantage"),
    ("Yahoo", "yahoo"),
]
DISCLAIMER = ("Screening aid based on rule-based checks of verified data. "
              "Not investment advice.")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def norm(s):
    return str(s or "").strip().lower().replace(" ", "_").replace("-", "_")


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def tol_for(name):
    n = norm(name)
    if n in TOL:
        return TOL[n]
    for prefix in ("rsi", "macd", "atr", "ema", "sma"):
        if n.startswith(prefix):
            return TOL[prefix]
    return TOL["default"]


def within(a, b, tol):
    pct, floor = tol
    return abs(a - b) <= max(floor, abs(b) * pct / 100.0)


def score_of(status):
    return CONFIG["status_scores"][status]


# --------------------------------------------------------------------------- #
# Metric validation
# --------------------------------------------------------------------------- #
def validate_metric(records, category, tol, anchored, anchor_class="official",
                    expected_date=None, strict_dates=False, cross_tol=None,
                    min_trades=None, reject_groups=(), allow_dual_official=False):
    """Validate one metric from a list of {source, value, as_of[, trades]} records.

    cross_tol           (pct, floor) used when one value is from BSE and the other is not.
    min_trades          {source: n}: that source only counts if its record has trades >= n.
                        Too-thin records are ignored (not a conflict) and listed under `ignored`.
    reject_groups       origin groups that may never corroborate this metric (BSE for volume).
    allow_dual_official allow two distinct official files from the same origin group (e.g. Volume Rule B:
                        NSE sec_bhavdata + udiff) to verify with status VERIFIED_DUAL_OFFICIAL.
    """
    warnings, dropped, ignored, clean = [], [], [], []

    for r in records or []:
        src = norm(r.get("source"))
        if src not in SOURCES:
            warnings.append(f"unknown source '{r.get('source')}' ignored")
            continue
        val = r.get("value")
        if not is_num(val):
            warnings.append(f"{src}: non-numeric value ignored")
            continue
        as_of = r.get("as_of")
        if expected_date and (as_of or strict_dates) and as_of != expected_date:
            dropped.append(f"{src}: as_of={as_of} (expected {expected_date})")
            continue
        group, cls = SOURCES[src]
        if group in reject_groups:
            warnings.append(f"{src}: {group.upper()} data cannot corroborate this metric; ignored")
            continue
        if min_trades and src in min_trades:
            t = r.get("trades")
            if not is_num(t) or t < min_trades[src]:
                ignored.append(f"{src}: {'unknown' if not is_num(t) else int(t)} trades "
                               f"(< {min_trades[src]}); too thin to corroborate")
                continue
        clean.append({"source": src, "value": float(val), "group": group, "cls": cls})

    order = PRIORITY.get(category, [])
    clean.sort(key=lambda r: order.index(r["source"]) if r["source"] in order else len(order))

    # one record per origin group
    seen, uniq = {}, []
    dual_official_corroborators = []
    dual_official_mismatch = False
    for r in clean:
        if r["group"] in seen:
            prev = seen[r["group"]]
            if not within(r["value"], prev["value"], tol):
                warnings.append(f"{r['source']} disagrees with {prev['source']} "
                                f"(same origin): check parser or intraday/close mix-up")
                if allow_dual_official and r["cls"] == "official" and prev["cls"] == "official":
                    dual_official_mismatch = True
            else:
                if allow_dual_official and r["cls"] == "official" and prev["cls"] == "official":
                    dual_official_corroborators.append(r["source"])
            continue
        seen[r["group"]] = r
        uniq.append(r)

    base = {"value": None, "primary": None, "validated_by": [], "outliers": [],
            "dropped": dropped, "ignored": ignored, "warnings": warnings}

    if not uniq:
        return {**base, "status": "MISSING", "score": score_of("MISSING")}

    primary = uniq[0]
    cluster = [primary]
    outliers = []
    for r in uniq[1:]:
        cross = cross_tol and ((r["group"] == "bse") != (primary["group"] == "bse"))
        (cluster if within(r["value"], primary["value"], cross_tol if cross else tol) else outliers).append(r)

    base.update({
        "value": primary["value"],
        "primary": primary["source"],
        "validated_by": [r["source"] for r in cluster[1:]],
        "outliers": [f"{r['source']}={r['value']}" for r in outliers],
    })

    if len(cluster) == 1:
        if outliers or dual_official_mismatch:
            status = "CONFLICT"
        elif allow_dual_official and dual_official_corroborators and primary["cls"] == "official":
            status = "VERIFIED_DUAL_OFFICIAL"
            base["validated_by"] = dual_official_corroborators
            base["warnings"].append(f"{category} verified via dual official files "
                                    f"({primary['source']} + {', '.join(dual_official_corroborators)}); "
                                    f"weaker than cross-source")
            return {**base, "status": status, "score": score_of(status)}
        elif primary["cls"] == "official":
            status = "SINGLE_OFFICIAL"
        elif primary["cls"] == "computed":
            status = "COMPUTED_ONLY"
        elif primary["cls"] == "vendor":
            status = "SINGLE_VENDOR"
        else:
            status = "SINGLE_UNOFFICIAL"
        return {**base, "status": status, "score": score_of(status)}

    classes = {r["cls"] for r in cluster}
    corroborators = [r for r in cluster[1:] if r["cls"] != "unofficial"]
    if anchored:
        ok = anchor_class in classes and bool(corroborators)
        if not ok:
            return {**base, "status": "WEAK", "score": score_of("WEAK")}
        status = "VERIFIED_PARTIAL" if outliers else "VERIFIED"
    else:
        has_official = "official" in classes
        status = "VERIFIED" if (has_official and not outliers) else "VERIFIED_PARTIAL"
    return {**base, "status": status, "score": score_of(status)}


def is_verified(res):
    return res["status"] in ("VERIFIED", "VERIFIED_PARTIAL", "VERIFIED_DUAL_OFFICIAL")


def validate_indicators(ind, required, trading_date):
    out = {}
    for name in required:
        spec = (ind or {}).get(name) or {}
        recs = []
        if is_num(spec.get("internal")):
            recs.append({"source": "internal", "value": spec["internal"]})
        recs += spec.get("vendors", [])
        out[name] = validate_metric(recs, "indicator", tol_for(name), True,
                                    "computed", trading_date, False)
    return out


def validate_group(data, expected, category):
    out = {}
    for name in expected:
        out[name] = validate_metric((data or {}).get(name), category,
                                    tol_for(name), False)
    return out


def score_history(h):
    if not h:
        return 0, False, False, ["history missing"]
    notes, score = [], 100
    adjusted = bool(h.get("adjusted"))
    gaps = h.get("gap_count", 0) or 0
    bars = h.get("bars", 0) or 0
    if not adjusted:
        score = min(score, 30)
        notes.append("history not adjusted for corporate actions")
    if gaps > 0:
        score = min(score, 60)
        notes.append(f"{gaps} missing trading days in history")
    if bars < CONFIG["min_bars_indicators"]:
        score = min(score, 50)
        notes.append(f"only {bars} bars (< {CONFIG['min_bars_indicators']})")
    ok = adjusted and gaps == 0 and bars >= CONFIG["min_bars_indicators"]
    return score, ok, adjusted, notes


def score_backtest_data(b):
    if not b:
        return 0, False, ["backtest data missing"]
    bars_ok = (b.get("bars", 0) or 0) >= CONFIG["min_bars_backtest"]
    sig_ok = (b.get("signals", 0) or 0) >= CONFIG["min_signals_backtest"]
    notes = []
    if not bars_ok:
        notes.append(f"backtest bars < {CONFIG['min_bars_backtest']}")
    if not sig_ok:
        notes.append(f"backtest signals < {CONFIG['min_signals_backtest']}")
    if bars_ok and sig_ok:
        return 100, True, notes
    return (50 if (bars_ok or sig_ok) else 0), False, notes


def band(score):
    if score >= 90:
        return "fully validated"
    if score >= 80:
        return "minor gaps"
    if score >= 60:
        return "several missing fields"
    if score >= 40:
        return "major missing fields"
    return "low confidence"


def conviction_penalty(conf):
    for limit, pen in CONFIG["penalties"]:
        if conf < limit:
            return pen
    return 0


def comp_mean(results, expected):
    if not expected:
        return 0.0
    return mean(results[m]["score"] if m in results else 0 for m in expected)


# --------------------------------------------------------------------------- #
# Per-stock evaluation
# --------------------------------------------------------------------------- #
def evaluate(stock):
    symbol = stock.get("symbol", "UNKNOWN")
    td = stock.get("trading_date")
    ev = stock.get("evidence") or {}
    scores = stock.get("scores") or {}
    notes = []

    if not td:
        notes.append("trading_date missing: price/volume cannot be date-validated")

    cmp_tol = TOL["cmp_live"] if stock.get("cmp_mode") == "live" else TOL["cmp_eod"]
    cmp_res = validate_metric(ev.get("cmp"), "price", cmp_tol, True, "official", td, True,
                              cross_tol=(CONFIG["cross_exchange_pct"], 0.0),
                              min_trades={"bse_bhavcopy": CONFIG["min_bse_trades"]})
    vol_allow_dual = (CONFIG.get("volume_rule", "B") == "B")
    vol_res = validate_metric(ev.get("volume"), "volume", TOL["volume"], True,
                              "official", td, True, reject_groups=("bse",),
                              allow_dual_official=vol_allow_dual)

    required_ind = stock.get("required_indicators") or CONFIG["expected_indicators"]
    ind_res = validate_indicators(ev.get("indicators"), required_ind, td)
    fund_res = validate_group(ev.get("fundamentals"),
                              stock.get("required_fundamentals") or CONFIG["expected_fundamentals"],
                              "fundamental")
    own_res = validate_group(ev.get("ownership"), CONFIG["expected_ownership"], "ownership")

    hist_score, hist_ok, hist_adjusted, hist_notes = score_history(ev.get("history"))
    bt_score, bt_ok, bt_notes = score_backtest_data(ev.get("backtest"))
    notes += hist_notes + bt_notes

    # indicator verification (optionally allow computed-only)
    def ind_ok(r):
        return is_verified(r) or (CONFIG["allow_computed_only_indicators"]
                                  and r["status"] == "COMPUTED_ONLY")
    indicators_verified = bool(ind_res) and all(ind_ok(r) for r in ind_res.values())

    # fundamentals staleness
    meta = ev.get("fundamentals_meta") or {}
    stale = ((meta.get("age_days") or 0) > CONFIG["fundamentals_stale_days"]
             or bool(meta.get("newer_result_filed")))
    fund_component = comp_mean(fund_res, list(fund_res))
    if stale:
        fund_component *= CONFIG["fundamentals_stale_multiplier"]
        notes.append("fundamentals stale: component x%.2f" % CONFIG["fundamentals_stale_multiplier"])

    components = {
        "cmp": cmp_res["score"],
        "volume": vol_res["score"],
        "indicators": comp_mean(ind_res, list(ind_res)),
        "fundamentals": fund_component,
        "history": hist_score,
        "ownership": comp_mean(own_res, list(own_res)),
        "backtest_data": bt_score,
    }
    w = CONFIG["weights"]
    raw = sum(components[k] * w[k] for k in w) / sum(w.values())

    caps = []
    cap = 100.0
    critical = [cmp_res, vol_res] + list(ind_res.values())
    if any(r["status"] == "CONFLICT" for r in critical):
        cap = min(cap, 49)
        caps.append("critical metric in CONFLICT: cap 49")
    cmp_verified = is_verified(cmp_res)
    vol_verified = is_verified(vol_res)
    if not (cmp_verified and vol_verified and indicators_verified):
        cap = min(cap, 79)
        caps.append("CMP/volume/indicators not all verified: cap 79")
    if not hist_adjusted:
        cap = min(cap, 59)
        caps.append("history not adjusted: cap 59")
    confidence = round(min(raw, cap), 1)

    # gate
    th = CONFIG["thresholds"]

    def ge(key, label):
        v = scores.get(key)
        return (is_num(v) and v >= th[key]), label

    conds = {}
    for key, label in [("market_regime", "Market Regime >= %d" % th["market_regime"]),
                       ("technical", "Technical >= %d" % th["technical"]),
                       ("fundamental", "Fundamental >= %d" % th["fundamental"]),
                       ("sentiment", "Sentiment >= %d" % th["sentiment"]),
                       ("backtest", "Backtest >= %d" % th["backtest"])]:
        ok, lab = ge(key, label)
        conds[lab] = ok
    conds["Data Confidence >= %d" % th["confidence"]] = confidence >= th["confidence"]
    conds["CMP verified"] = cmp_verified
    conds["Volume verified"] = vol_verified
    conds["Technical indicators verified"] = indicators_verified
    conds["History adjusted, gap-free, sufficient"] = hist_ok
    conds["Backtest data sufficient"] = bt_ok

    failed = [k for k, v in conds.items() if not v]
    data_keys = {"Data Confidence >= %d" % th["confidence"], "CMP verified",
                 "Volume verified", "Technical indicators verified",
                 "History adjusted, gap-free, sufficient", "Backtest data sufficient"}
    data_failed = [k for k in failed if k in data_keys]

    trusted_for_trigger = cmp_verified and vol_verified and indicators_verified
    fallback = (stock.get("setup") or {}).get("fallback_status", "WATCH")
    if not failed:
        status = "BUY NOW"
    elif fallback == "WAIT FOR BREAKOUT" and trusted_for_trigger:
        status = "WAIT FOR BREAKOUT"
    else:
        status = "WATCH"

    if cmp_verified and vol_verified and indicators_verified:
        validation_status = "VERIFIED"
    elif not (cmp_verified and vol_verified):
        validation_status = "UNVERIFIED DATA"
    else:
        validation_status = "PARTIAL (indicators unverified)"

    conv_in = stock.get("conviction_score")
    penalty = conviction_penalty(confidence)
    conviction = None
    if is_num(conv_in):
        conviction = {"input": conv_in, "penalty": penalty,
                      "adjusted": max(0, conv_in + penalty)}

    return {
        "symbol": symbol, "isin": stock.get("isin"), "trading_date": td,
        "status": status,
        "excluded_for_data_quality": bool(data_failed),
        "scores": scores,
        "data_confidence": {
            "score": confidence, "raw": round(raw, 1), "band": band(confidence),
            "components": {k: round(v, 1) for k, v in components.items()},
            "caps_applied": caps,
        },
        "validation_status": validation_status,
        "primary_source": cmp_res["primary"],
        "validation_sources": cmp_res["validated_by"] if cmp_verified else [],
        "agreeing_but_insufficient": [] if cmp_verified else cmp_res["validated_by"],
        "validation": {
            "cmp": cmp_res, "volume": vol_res, "indicators": ind_res,
            "fundamentals": fund_res, "ownership": own_res,
        },
        "conditions": conds,
        "failed_conditions": failed,
        "conviction": conviction,
        "notes": notes,
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def fmt(v):
    return "n/a" if v is None else (f"{v:g}" if isinstance(v, (int, float)) else str(v))


def render_stock(r):
    s = r["scores"]
    dc = r["data_confidence"]
    tag = "" if r["validation_status"] == "VERIFIED" else "  [UNVERIFIED DATA]" \
        if r["validation_status"].startswith("UNVERIFIED") else ""
    lines = [
        f"### {r['symbol']}: {r['status']}{tag}",
        f"- Technical {fmt(s.get('technical'))} | Fundamental {fmt(s.get('fundamental'))} | "
        f"Sentiment {fmt(s.get('sentiment'))} | Backtest {fmt(s.get('backtest'))}",
        f"- Data Confidence {dc['score']} ({dc['band']}) | Validation Status: {r['validation_status']}",
        f"- Primary Source: {fmt(r['primary_source'])} | "
        f"Validation Source(s): {', '.join(r['validation_sources']) or 'none'}"
        + (f" (agree but do not qualify: {', '.join(r['agreeing_but_insufficient'])})"
           if r["agreeing_but_insufficient"] else ""),
    ]
    if r["conviction"]:
        c = r["conviction"]
        lines.append(f"- Conviction {c['input']} -> {c['adjusted']} (data penalty {c['penalty']})")
    if r["failed_conditions"]:
        lines.append("- Failed: " + "; ".join(r["failed_conditions"]))
    for cap in dc["caps_applied"]:
        lines.append(f"- Cap: {cap}")
    for key in ("cmp", "volume"):
        v = r["validation"][key]
        if v["outliers"] or v["dropped"]:
            lines.append(f"- {key}: " + "; ".join(
                [f"outlier {o}" for o in v["outliers"]] + [f"dropped {d}" for d in v["dropped"]]))
    for n in r["notes"]:
        lines.append(f"- Note: {n}")
    return "\n".join(lines)


def render_report(results, health):
    out = ["# Daily Data Quality Report", ""]
    health = health or {}
    out.append(f"Run date: {health.get('run_date', 'n/a')}")
    out += ["", "## Source Health", "", "| Source | Status |", "|---|---|"]
    srcs = health.get("sources", {})
    for label, key in HEALTH_SOURCES:
        out.append(f"| {label} | {srcs.get(key, 'NOT RUN')} |")

    confs = [r["data_confidence"]["score"] for r in results]
    out += ["", "## Data Confidence", ""]
    if confs:
        out.append(f"Stocks evaluated: {len(confs)} | mean {mean(confs):.1f} | "
                   f"min {min(confs):.1f} | below 80: {sum(c < 80 for c in confs)}")
    else:
        out.append("No stocks evaluated.")

    out += ["", "## Validation Failures", ""]
    fails = list(health.get("validation_failures", []))
    for r in results:
        for key in ("cmp", "volume"):
            v = r["validation"][key]
            if v["status"] in ("CONFLICT", "MISSING", "WEAK") or v["dropped"]:
                fails.append(f"{r['symbol']} {key}: {v['status']}"
                             + (f" ({'; '.join(v['dropped'])})" if v["dropped"] else ""))
        for name, v in r["validation"]["indicators"].items():
            if v["status"] in ("CONFLICT", "MISSING", "WEAK"):
                fails.append(f"{r['symbol']} {name}: {v['status']}")
    out += [f"- {f}" for f in fails] or ["- none"]

    out += ["", "## Fallback Sources Used", ""]
    fb = health.get("fallbacks_used", [])
    out += [f"- {f}" for f in fb] or ["- none"]

    out += ["", "## Stocks Excluded Due To Poor Data Quality", ""]
    ex = [r for r in results if r["excluded_for_data_quality"]]
    if ex:
        for r in ex:
            reasons = [c for c in r["failed_conditions"]
                       if c.startswith(("Data", "CMP", "Volume", "Technical indicators",
                                        "History", "Backtest data"))]
            out.append(f"- {r['symbol']} ({r['status']}): " + "; ".join(reasons))
    else:
        out.append("- none")

    out += ["", "## Stocks", ""]
    for r in sorted(results, key=lambda x: (x["status"] != "BUY NOW", -x["data_confidence"]["score"])):
        out += [render_stock(r), ""]
    out += ["---", DISCLAIMER]
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("--health")
    ap.add_argument("--format", choices=["md", "json"], default="md")
    ap.add_argument("--out")
    ap.add_argument("--allow-computed-indicators", action="store_true",
                    help="Allow internally computed indicators (without vendor corroboration) to count as verified")
    args = ap.parse_args()

    if args.allow_computed_indicators:
        CONFIG["allow_computed_only_indicators"] = True

    try:
        with open(args.input, encoding="utf-8") as f:
            data = json.load(f)
        health = None
        if args.health:
            with open(args.health, encoding="utf-8") as f:
                health = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"error: {e}")

    stocks = data["stocks"] if isinstance(data, dict) and "stocks" in data else data
    if not isinstance(stocks, list):
        sys.exit("error: input must be a list of stocks or {\"stocks\": [...]}")

    results = [evaluate(s) for s in stocks]
    text = (json.dumps({"results": results, "disclaimer": DISCLAIMER}, indent=2)
            if args.format == "json" else render_report(results, health))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"wrote {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
