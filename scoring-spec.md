# Scoring Specification

The authoritative definition of how `scripts/gate_check.py` validates metrics and scores confidence. If you change a number here, change the matching constant in `CONFIG`, `TOL`, or `PRIORITY` at the top of the script, then re-run `assets/sample_input.json` as a regression check.

## Contents
1. Source classes and independence
2. Like-for-like comparison rules
3. Tolerances
4. Metric validation statuses
5. Confidence score formula
6. Caps and staleness
7. Sufficiency rules (history, backtest)
8. Conviction penalties
9. Design decisions you may want to change

---

## 1. Source classes and independence

| Source | Origin group | Class |
|---|---|---|
| nse_bhavcopy, nse_api, nse_shareholding | `nse` | official |
| bse_api, bse_filings | `bse` | official |
| company_filings | `filings` | official |
| nsdl | `nsdl` | official |
| twelve_data | `twelve_data` | vendor |
| alpha_vantage | `alpha_vantage` | vendor |
| fmp | `fmp` | vendor |
| screener, tickertape, trendlyne | one group each | vendor |
| yahoo | `yahoo` | unofficial |
| internal (own calculation from verified OHLC) | `internal` | computed |

**Independence is by origin group.** Bhavcopy and the NSE API are both NSE, so together they count as one group. Agreement between them proves your parser works; it does not prove NSE is right. Two distinct groups are needed for verification.

If two records from the *same* group disagree, keep the higher-priority one and log a warning. It almost always means a parser bug or an intraday-vs-close mix-up.

## 2. Like-for-like comparison rules

Only compare values that are comparable. Before validating, confirm:
- same security (matched by ISIN), same exchange (NSE vs NSE; cross-exchange NSE vs BSE needs the wider tolerance)
- same trading date (`as_of` must equal `trading_date`; for price and volume a missing `as_of` is treated as stale)
- same adjustment basis (all adjusted or all unadjusted for splits/bonus)
- same definition (consolidated vs standalone for fundamentals; NSE-only vs combined volume)

Records that fail these are dropped and listed under `dropped`, never silently compared.

## 3. Tolerances

A pair agrees if `|a - b| <= max(abs_floor, pct% x |b|)`.

| Metric | pct | abs floor | Notes |
|---|---|---|---|
| CMP, EOD close | 0.10 | 0 | Same-exchange closes should match almost exactly |
| CMP, live (`cmp_mode: live`) | 0.30 | 0 | Also require timestamps within ~5 min of each other |
| Volume | 2.0 | 0 | Same exchange only. Vendors that report combined NSE+BSE volume will not match |
| RSI | 0 | 1.0 pt | Wilder smoothing and warm-up length cause small differences |
| MACD (line/signal/hist) | 3.0 | 0.05 | Floor handles near-zero histograms |
| ATR | 2.0 | 0 | |
| EMA / SMA | 0.5 | 0 | |
| PE | 5.0 | 0.5 | Moves with price; compare same-day |
| PB | 5.0 | 0.1 | |
| Debt/Equity | 10.0 | 0.05 | |
| Promoter / pledge / FII / DII holding | 0 | 0.5 pt | |
| Other fundamentals (ROE, ROCE, margins, growth) | 10.0 | 1.0 | Providers define ROE/ROCE slightly differently; wide on purpose |

Tolerances are deliberately tight for exchange-sourced price data and looser for derived ratios whose definitions differ between providers.

## 4. Metric validation statuses

Metrics are grouped into two kinds.

**Anchored metrics** (CMP, volume, indicators). Verification needs an *anchor* in the agreeing cluster: an official source for price and volume, or your own internal calculation for indicators.

**Unanchored metrics** (fundamentals, ownership). Two independent groups agreeing is enough, since filings are rarely machine-readable and two aggregators agreeing is the practical standard.

The primary is the highest-priority record. The *cluster* is the primary plus every other-group record within tolerance of it. Records outside tolerance are *outliers*.

| Status | Condition | Score |
|---|---|---|
| VERIFIED | Cluster has 2+ groups; for anchored metrics, includes an anchor and a non-Yahoo corroborator; no outliers (unanchored metrics also need one official source) | 100 |
| VERIFIED_PARTIAL | Meets the VERIFIED test, but some other source disagreed (outlier), or an unanchored cluster has no official source (for example Screener + Tickertape) | 90 |
| SINGLE_OFFICIAL | Only one group, official | 70 |
| COMPUTED_ONLY | Only your own internal calculation, no vendor match (indicators). Counts as verified only if `allow_computed_only_indicators` is on | 70 |
| WEAK | 2+ groups but no anchor, or the only corroborator is Yahoo | 55 |
| SINGLE_VENDOR | Only one group, vendor | 40 |
| SINGLE_UNOFFICIAL | Only one group, Yahoo | 30 |
| CONFLICT | Primary disagrees with every other source | 15 |
| MISSING | No usable records | 0 |

For CMP, volume, and indicators, `VERIFIED` and `VERIFIED_PARTIAL` count as verified at the gate; everything else does not. `SINGLE_OFFICIAL` is acceptable only for metrics where a single official source is the norm (delivery %, shareholding pattern), and those are non-gating.

## 5. Confidence score formula

```
component_score = mean(metric_scores over the expected metrics)   # missing expected metric counts as 0
raw = sum(weight_i x component_i) / 100
```

| Component | Weight | Contents |
|---|---|---|
| cmp | 25 | CMP |
| volume | 15 | Volume |
| indicators | 15 | rsi14, macd, atr14, ema20, ema50, sma200 (override per stock with `required_indicators`) |
| fundamentals | 15 | roe, roce, debt_equity, revenue_growth, profit_growth, ebitda_margin, pe, pb |
| history | 10 | Adjusted OHLC integrity (section 7) |
| ownership | 10 | promoter_holding, promoter_pledge, fii_holding, dii_holding |
| backtest_data | 10 | Bars and signals sufficiency (section 7) |

## 6. Caps and staleness

Caps apply after the weighted sum so one strong area cannot mask a critical failure.

| Condition | Cap |
|---|---|
| Any CONFLICT in CMP, volume, or an indicator | 49 |
| CMP, volume, or any required indicator not VERIFIED | 79 |
| History not adjusted for corporate actions | 59 |

**Fundamental staleness.** If `fundamentals_meta.age_days > 30` or `newer_result_filed` is true, multiply the *fundamentals component* by 0.9. The original prompt said to reduce the overall score by 10%, which punishes price and volume integrity for an unrelated problem. Quarterly fundamentals are legitimately more than 30 days old for much of the quarter; the better trigger is a newer filed result than the one in your dataset.

**Bands.** 90-100 fully validated; 80-89 minor gaps; 60-79 several missing; 40-59 major missing; below 40 low confidence.

## 7. Sufficiency rules

**History** (feeds indicators and backtests):
- `adjusted` must be true; otherwise history score is capped at 30 and the confidence cap above applies
- `gap_count` must be 0 (gaps = missing trading days, not holidays); otherwise history score is at most 60
- `bars >= 260` for indicators (SMA200 needs 200 plus warm-up); otherwise at most 50

**Backtest data** ("Missing Backtest Data" in the original rules):
- `bars >= 750` (about 3 years) **and** `signals >= 30` historical occurrences of the setup: score 100, counts as present
- One of the two: score 50, counts as missing
- Neither: score 0

Thirty signals is a minimum for a backtest to say anything at all, not a mark of statistical confidence. Raise it if you can.

## 8. Conviction penalties

Tiered, not cumulative:

| Data Confidence | Conviction penalty |
|---|---|
| >= 80 | 0 |
| 60-79 | -5 |
| 40-59 | -10 |
| < 40 | -20 |

Adjusted conviction is floored at 0.

## 9. Design decisions you may want to change

- **Yahoo cannot corroborate for gating.** Relax by adding `"unofficial"` to the allowed corroborator classes in `validate_metric`.
- **Indicators need a vendor match.** Computing from verified OHLC alone is not enough for BUY NOW. Set `CONFIG["allow_computed_only_indicators"] = True` to relax.
- **Two NSE sources are one origin.** If you consider NSE the sole ground truth for NSE-listed prices, you could treat NSE API + Bhavcopy as sufficient. That makes BUY NOW easier to reach and removes protection against an NSE-side or parser-side error.
