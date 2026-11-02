# pipeline: NSE and BSE data fetchers

Stage B of the `nse-data-integrity-engine` skill. Standard library only (Python 3.9+).

## What it does
1. Works out the trading date (weekends and `config/holidays.txt` are skipped; with no `--date` it picks the latest completed session, after 18:30 IST).
2. Downloads the equity Bhavcopy with delivery data, plus the UDiFF file for ISINs. Retries 3 times with backoff, honours `Retry-After`, stops at once on HTTP 403.
3. Validates before trusting: file date equals the trading date, row count is plausible against the prior day, OHLC is sane, delivery is not above volume, the two NSE files agree with each other.
4. Flags `possible_corporate_action` when today's PREV_CLOSE differs from yesterday's stored close.
5. Writes everything to `data/` and, with `--emit-evidence`, partial gate input (`cmp` and `volume` records from `nse_bhavcopy`).

A row that fails a check is marked unusable and produces no evidence. Nothing is ever estimated or filled in.

## Run (from the workspace root)
```powershell
py pipeline\fetchers\nse_bhavcopy.py --date 2026-10-30 --emit-evidence data\evidence_bhavcopy.json
py pipeline\fetchers\nse_bhavcopy.py --poll --until 21:00        # evening run: waits for the file
py pipeline\fetchers\nse_bhavcopy.py --date 2026-10-30 --delivery-file "C:\path\sec_bhavdata_full_30102026.csv" --no-udiff   # manual file
```
Exit codes: `0` OK or non-trading day, `2` not published (EOD fields MISSING), `3` FAILED.

## Outputs (`data/`)
| Path | Content |
|---|---|
| `raw/YYYY-MM-DD/` | Raw downloads (cache; reruns do not re-download, use `--refresh` to force) |
| `bhavcopy/YYYY-MM-DD.csv` | Normalised rows, with `usable` and `flags` columns |
| `bhavcopy/YYYY-MM-DD.manifest.json` | Row counts, SHA-256 of each file, prior file used |
| `bhavcopy/YYYY-MM-DD.health.json` | Source-health fragment for `gate_check.py --health` |
| `evidence_bhavcopy.json` | Partial gate input (only with `--emit-evidence`) |

## Important
- Bhavcopy alone grades CMP and volume `SINGLE_OFFICIAL`, which cannot reach BUY NOW. The gate needs a second independent origin (BSE or a vendor). That is the next fetcher.
- Not covered yet: F&O Bhavcopy and open interest, NSE API, BSE, vendors, indicators.
- If NSE returns 403 the tool stops and does not retry. Download the file manually from NSE and use `--delivery-file`. Do not try to work around the block.
- On a corporate network, SSL inspection or a proxy can cause fetch errors. The message is printed in full.
- `data/` grows daily. Keep it out of OneDrive sync if you can.

## BSE, comparison and gate input
```powershell
py pipeline\fetchers\bse_bhavcopy.py --date 2026-10-30 --file "C:\Users\you\Downloads\BhavCopy_BSE_CM_0_0_0_20261030_F_0000.CSV"
py pipeline\tools\compare_exchanges.py --date 2026-10-30        # how closely NSE and BSE closes agree
py pipeline\tools\build_gate_input.py --date 2026-10-30         # cmp + volume evidence for every stock, with a summary
py pipeline\tools\backfill_compare.py --from 2026-10-19 --to 2026-10-30   # fetch many days of both exchanges and compare day by day
py pipeline\tools\price_basis_check.py --date 2026-10-30        # optional: vendor close vs NSE close/last (needs a vendor key)
```
BSE can corroborate **price only** (and only with 100+ BSE trades). BSE volume never verifies NSE volume.
The BSE download URL folder is unconfirmed; if automatic download fails, save the file by hand and use `--file`.

## Holidays and bad downloads
- `config/holidays.txt` lists weekday market holidays (partial: complete it from NSE's official circular). Listed days and weekends are skipped.
- If a server answers a missing file with a web page, the run reports `not published`, not a data error, and polling continues.
- NSE answers a holiday with the PREVIOUS day's complete file under the new day's name (seen on 2026-10-20, Dussehra). A file dated earlier than requested is treated as `not published` (polling continues) and is never used or cached. A file dated later than requested, or with mixed dates, is rejected as FAILED.
- A rejected download is moved to `<name>.rejected` in `data/raw/<date>/` so the next run fetches it again, and the reason is saved as `REJECTED_nse.txt` or `REJECTED_bse.txt`. A file you supply yourself is never moved.

## Tests
```powershell
py -m unittest discover -s pipeline\tests -v     # 133 tests, no internet needed
```
Network behaviour (404 before publishing, 5xx, 403, rate limiting) is simulated by a local server.

## If NSE changes a file
The parser stops with `missing columns [...]. Columns found: [...]` rather than guessing. Update `DELIVERY_REQUIRED` / `UDIFF_REQUIRED` and the `URLS` at the top of `nse_bhavcopy.py`.
