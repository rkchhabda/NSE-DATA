# Source Playbook

Per-source extraction notes for the agent writing or running fetchers. Read this before writing code that touches a source. Endpoints and file formats change; the notes marked **verify** are patterns that were correct when written and must be checked against the live source before hard-coding.

## Contents
1. General fetcher rules
2. NSE Bhavcopy (primary EOD)
3. NSE API
4. BSE
5. Twelve Data
6. Alpha Vantage
7. Screener.in
8. Tickertape
9. Trendlyne
10. Financial Modeling Prep
11. Yahoo Finance
12. Symbol mapping
13. Daily timeline

---

## 1. General fetcher rules

- **Write fetchers into the user's workspace** (for example `pipeline/fetchers/`), not into this skill folder. Test each on one symbol before running the universe.
- **Record provenance at fetch time:** source name, fetch timestamp (IST), the `as_of` trading date the value refers to, and the raw response hash or file name. Evidence records are built from this.
- **Retry policy:** 3 attempts, exponential backoff with jitter (about 2s, 4s, 8s), honour `Retry-After`. After the third failure mark the source `FAILED` for the run and continue down the fallback chain.
- **Rate limits:** keep a per-source budget and stop before you hit it. Free tiers (Alpha Vantage's especially) have very small daily and per-minute quotas that change over time; check the current plan limits rather than assuming. The shortlist funnel in `SKILL.md` Stage D exists to stay inside them.
- **Cache raw files** (Bhavcopy ZIPs/CSVs, API JSON) by date so reruns do not re-download and so every score is reproducible later.
- **Respect terms of use.** Several of these sites may not offer a supported public API, and automated access may be restricted by their terms or technical controls. Use official exports, documented APIs, or your own authenticated plan where available. If a source blocks automated access, mark it `FAILED`; do not work around access controls and do not substitute guessed values.
- **Parse by header name, never column position.** Formats change.
- **Fail loudly.** A parser that cannot find an expected column should stop and report, not return NaN-filled rows that look like data.
- **All timestamps in IST.** Convert vendor UTC timestamps explicitly.

## 2. NSE Bhavcopy (primary for EOD close, volume, delivery %)

**Files**
- Equity Bhavcopy with delivery data, commonly `sec_bhavdata_full_DDMMYYYY.csv` under `nsearchives.nseindia.com/products/content/`. Typical columns include previous close, open/high/low/last/close, average price, total traded quantity, turnover, number of trades, delivery quantity, and delivery percentage. Match columns by name. **verify**
- CM-UDiFF Common Bhavcopy: NSE moved to the standardised UDiFF format in July 2024; file names follow the pattern `BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000` (zipped CSV). This variant carries ISIN, which the delivery CSV lacks, so use it for ISIN mapping. **verify**
- F&O Bhavcopy (UDiFF, same naming family for the derivatives segment): open interest and change in open interest. Applies only to F&O-eligible stocks; for others OI is `N/A`, not missing.

**Timing.** Published after market close, generally after about 18:00 IST; the delivery file can come later. Do not run the pipeline at 16:00.

**Checks before trusting a file**
- File date equals the expected trading date
- Row count is plausible versus the previous day (a truncated download is common)
- Series filter: keep `EQ` (and decide explicitly about `BE`, `SM`, and others)
- Previous close here versus your stored prior close: mismatch means a corporate action
- Delivery % lies in 0-100 and delivery quantity does not exceed traded quantity

**Role.** Primary for close, volume, delivery %. Never replaced by vendor data when present.

## 3. NSE API (live and structured data)

Used for market status, live quotes, indices, corporate actions, F&O data, bulk and block deals, and shareholding pattern.

- These are the endpoints the NSE website itself uses. They typically need browser-like headers and a session cookie obtained by loading the NSE homepage first, and they can rate-limit or block automated clients. Treat them as best-effort. **verify**
- Throttle to a few requests per second at most, reuse one session, and cache.
- Same origin as Bhavcopy (`nse` group), so it does not independently validate Bhavcopy. It validates your parsing and gives the live CMP during market hours.

## 4. BSE

- Second official origin (`bse` group). This is what lets two *official* sources corroborate each other.
- Use for dual-listed price checks (NSE vs BSE tolerance is wider than same-exchange), filings, and shareholding.
- Map NSE symbol to BSE scrip code via ISIN, not by name.

## 5. Twelve Data

- Real-time and historical quotes, OHLC, and server-side indicators (RSI, MACD, ATR, EMA, SMA).
- Request the NSE exchange explicitly so you compare NSE with NSE.
- Confirm adjustment settings on historical data match your own adjusted series before comparing indicators.
- Role: corroborator for price, volume, and indicators. Vendor class.

## 6. Alpha Vantage

- Secondary backup for daily OHLC, indicators, and earnings.
- Coverage of Indian tickers and exchange suffix conventions can be uneven; test each symbol format before relying on it.
- Quotas are small. Use only for shortlist validation and as a fallback when Twelve Data fails.

## 7. Screener.in

- Fundamentals: ROE, ROCE, Debt/Equity, revenue and profit growth, EBITDA and operating margin, PE, PB, EV/EBITDA, promoter holding, pledge.
- Note whether figures are consolidated or standalone and carry that into the evidence record; mixing them is a classic false conflict.
- Record the period of the latest reported quarter. If a newer result has been filed since, set `newer_result_filed: true`.

## 8. Tickertape

- Analyst ratings and targets, quality / momentum / valuation scores, growth forecast, ownership trends.
- Cross-validate market cap, PE, and ROE against Screener. Scores here are the vendor's own opinions; use them as sentiment inputs, not as validation of price data.

## 9. Trendlyne

- Analyst consensus, earnings revisions, target prices, shareholding changes, FII/DII trends, bulk deals, insider activity.
- Used to build the Institutional Score (0-100) for the sentiment module and as a corroborating source for ownership.

## 10. Financial Modeling Prep

- Financial statements, cash flow, enterprise value, ratios.
- Use to cross-validate ROE, ROCE, EBITDA margin, and debt. Coverage of Indian small caps can be thin; a gap is `MISSING`, not a reason to fill in a number.

## 11. Yahoo Finance

- Last-resort fallback only. It is unofficial, can silently change behaviour, and is frequently revised after the fact.
- Never replaces Bhavcopy values. Never counts as a validating corroborator for BUY NOW (it yields `WEAK`).
- If it ends up in the evidence at all, it appears in the report under "Fallback Sources Used".

## 12. Symbol mapping

Maintain a symbol master keyed on **ISIN**:

| Field | Example |
|---|---|
| isin | the join key across everything |
| nse_symbol | as used in Bhavcopy |
| bse_code | numeric scrip code |
| yahoo_symbol | NSE symbol with `.NS` suffix |
| twelve_data_symbol | symbol plus exchange parameter |
| status / listing history | renames, mergers, delistings |

Refresh it from the Bhavcopy security files on each run. A stock whose ISIN differs between two sources has not been validated, however close the prices look.

## 13. Daily timeline (IST)

| Time | Action |
|---|---|
| 18:30 | Start. Poll for Bhavcopy every ~10 min |
| to ~21:00 | Polling window. After this, mark EOD fields `MISSING` for the day |
| after Bhavcopy | Delivery and F&O files, corporate-action check, adjusted history rebuild |
| then | Upstream scoring, shortlist, deep validation, gate |
| then | Persist, report, backtests |
| weekly or on result filing | Fundamentals refresh |
