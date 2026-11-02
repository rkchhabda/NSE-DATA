#!/usr/bin/env python3
"""
nse_fo_bhavcopy.py - download, parse, validate and normalise NSE F&O (Derivatives) Bhavcopy.

Extracts contract-level derivatives data, aggregates per-symbol metrics (Total Futures OI,
OI change %, Buildup Classification, Call/Put Walls, Put-Call Ratio), and emits structured
JSON and CSV records for downstream gating and screening.

Usage:
    py pipeline\\fetchers\\nse_fo_bhavcopy.py --date 2026-10-30
    py pipeline\\fetchers\\nse_fo_bhavcopy.py --poll --until 21:00
    py pipeline\\fetchers\\nse_fo_bhavcopy.py --date 2026-10-30 --file "path/to/fo.zip"

Exit codes:
    0 : Success or non-trading day (skipped cleanly)
    2 : Not published (file not yet available on server)
    3 : Failed (data schema error, network error after retries, corrupt file)
"""

import argparse
import csv
import hashlib
import io
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

IST = timezone(timedelta(hours=5, minutes=30))
SOURCE = "nse_fo_bhavcopy"

URLS = {
    # Primary: standardized UDiFF FO Bhavcopy (zipped CSV)
    "udiff": "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip",
    # Fallback: classic derivatives Bhavcopy
    "classic": "https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{yyyy}/{mmm}/fo{dd}{mmm}{yyyy}bhav.csv.zip",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

_OPENER = None  # Tests inject an opener that bypasses system proxies / redirects to mock server


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class FetchError(Exception):
    pass


class NotPublished(FetchError):
    """HTTP 404: file not (yet) published on the server."""


class StaleFile(NotPublished):
    """Server returned a file dated earlier than requested (e.g. holiday behavior)."""


class Blocked(FetchError):
    """HTTP 403: server refused automated access."""


class FetchFailed(FetchError):
    """Network or server error persisted after all retries."""


class DataError(Exception):
    pass


class SchemaError(DataError):
    pass


class WrongDate(DataError):
    pass


# --------------------------------------------------------------------------- #
# Date & Calendar Utilities
# --------------------------------------------------------------------------- #
def now_ist() -> datetime:
    return datetime.now(IST)


def parse_date(s: str) -> date:
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date format: {s!r}")


def load_holidays(path: Optional[str]) -> Set[date]:
    holidays = set()
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    try:
                        holidays.add(parse_date(line))
                    except ValueError:
                        continue
    return holidays


def is_trading_day(d: date, holidays: Set[date]) -> bool:
    return d.weekday() < 5 and d not in holidays


def default_trading_date(now: datetime, holidays: Set[date]) -> date:
    d = now.date()
    if is_trading_day(d, holidays) and now.timetz().replace(tzinfo=None) >= dtime(18, 30):
        return d
    d -= timedelta(days=1)
    while not is_trading_day(d, holidays):
        d -= timedelta(days=1)
    return d


# --------------------------------------------------------------------------- #
# Network & Retrieval
# --------------------------------------------------------------------------- #
def _backoff(attempt: int) -> float:
    return 2 ** attempt + random.random()


def _retry_after(err: urllib.error.HTTPError) -> Optional[float]:
    try:
        return min(60.0, float(err.headers.get("Retry-After", "")))
    except (TypeError, ValueError, AttributeError):
        return None


def _looks_like_html(body: bytes) -> bool:
    head = body[:250].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith((b"<!doctype", b"<html", b"<head", b"<body"))


def fetch_bytes(url: str, retries: int = 3, timeout: int = 30, sleep=time.sleep) -> bytes:
    last_err = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers=HEADERS)
        wait = _backoff(attempt)
        try:
            opener = _OPENER or urllib.request.build_opener()
            with opener.open(req, timeout=timeout) as resp:
                body = resp.read()
            if _looks_like_html(body):
                raise NotPublished(f"{url} returned HTML instead of archive file.")
            return body
        except urllib.error.HTTPError as e:
            code = e.code
            retry_after = _retry_after(e)
            e.close()
            if code == 404:
                raise NotPublished(url)
            if code == 403:
                raise Blocked(f"HTTP 403 from {url}: NSE server refused automated access.")
            last_err = f"HTTP {code}"
            wait = retry_after or wait
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_err = str(e)
        if attempt < retries:
            sleep(wait)
    raise FetchFailed(f"{url}: {last_err} (after {retries} attempts)")


def download_fo_bytes(d: date, raw_dir: str, retries: int = 3, sleep=time.sleep) -> Tuple[bytes, str]:
    """Tries UDiFF FO first, then falls back to classic FO URL."""
    yyyymmdd = d.strftime("%Y%m%d")
    ddmmyyyy = d.strftime("%d%m%Y")
    dd = d.strftime("%d")
    mmm = d.strftime("%b").upper()
    yyyy = d.strftime("%Y")

    os.makedirs(raw_dir, exist_ok=True)
    cached_zip = os.path.join(raw_dir, f"fo_{yyyymmdd}.zip")
    if os.path.exists(cached_zip):
        with open(cached_zip, "rb") as f:
            return f.read(), "cached_zip"

    # Try UDiFF
    udiff_url = URLS["udiff"].format(yyyymmdd=yyyymmdd)
    try:
        data = fetch_bytes(udiff_url, retries=retries, sleep=sleep)
        with open(cached_zip, "wb") as f:
            f.write(data)
        return data, udiff_url
    except NotPublished:
        pass  # Fall back to classic

    classic_url = URLS["classic"].format(yyyy=yyyy, mmm=mmm, dd=dd)
    try:
        data = fetch_bytes(classic_url, retries=retries, sleep=sleep)
        with open(cached_zip, "wb") as f:
            f.write(data)
        return data, classic_url
    except NotPublished:
        raise NotPublished(f"F&O Bhavcopy not published for {d.isoformat()} at UDiFF or classic endpoints.")


# --------------------------------------------------------------------------- #
# Parsing & Normalization
# --------------------------------------------------------------------------- #
def extract_csv_from_zip(zip_bytes: bytes) -> str:
    """Extracts first CSV found in zip archive and returns as decoded string."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            csv_names = [n for n in z.namelist() if n.lower().endswith(".csv")]
            if not csv_names:
                raise SchemaError("No CSV file found inside F&O zip archive.")
            raw_csv = z.read(csv_names[0])
            return raw_csv.decode("utf-8-sig", errors="replace")
    except zipfile.BadZipFile:
        # Might be plain CSV already
        try:
            return zip_bytes.decode("utf-8-sig", errors="replace")
        except Exception:
            raise SchemaError("Corrupt F&O archive: not a valid zip or CSV file.")


def _clean_str(val: Optional[str]) -> str:
    return str(val or "").strip()


def _clean_float(val: Optional[str], default: float = 0.0) -> float:
    try:
        s = str(val or "").strip().replace(",", "")
        return float(s) if s else default
    except (ValueError, TypeError):
        return default


def parse_fo_records(csv_text: str, expected_date: date) -> List[Dict]:
    """
    Parses either UDiFF or classic F&O CSV rows into uniform contract dictionaries.
    Standardized keys:
        - date (ISO YYYY-MM-DD)
        - symbol (str)
        - instrument (FUTSTK, OPTSTK, FUTIDX, OPTIDX)
        - expiry_date (date)
        - strike_price (float)
        - option_type (XX, CE, PE)
        - open, high, low, close, settle (float)
        - contracts (int)
        - open_interest (float)
        - chg_in_oi (float)
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    fieldnames = [f.strip() for f in (reader.fieldnames or [])]
    if not fieldnames:
        raise SchemaError("Empty F&O CSV: no headers found.")

    # Determine schema type
    is_udiff = "TckrSymb" in fieldnames or "FinInstrmTp" in fieldnames
    is_classic = "INSTRUMENT" in fieldnames and "SYMBOL" in fieldnames

    if not (is_udiff or is_classic):
        raise SchemaError(f"Unrecognised F&O Bhavcopy schema. Headers found: {fieldnames[:10]}...")

    records = []
    file_dates = set()

    for row in reader:
        # Normalize keys by stripping spaces
        r = {k.strip(): v.strip() if isinstance(v, str) else v for k, v in row.items() if k}

        if is_udiff:
            dt_raw = r.get("TradDt") or r.get("BizDt") or ""
            sym = r.get("TckrSymb") or ""
            inst = r.get("FinInstrmTp") or ""
            xpry_raw = r.get("XpryDt") or ""
            strike = _clean_float(r.get("StrkPric"))
            opt_type = (r.get("OptnTp") or "XX").upper()
            open_pr = _clean_float(r.get("OpnPric"))
            high_pr = _clean_float(r.get("HghPric"))
            low_pr = _clean_float(r.get("LwPric"))
            close_pr = _clean_float(r.get("ClsPric"))
            settle_pr = _clean_float(r.get("SttlmPric")) or close_pr
            vol = int(_clean_float(r.get("TtlTradgVol")))
            oi = _clean_float(r.get("OpnIntrst"))
            chg_oi = _clean_float(r.get("ChngInOpnIntrst"))
        else:
            dt_raw = r.get("TIMESTAMP") or ""
            sym = r.get("SYMBOL") or ""
            inst = r.get("INSTRUMENT") or ""
            xpry_raw = r.get("EXPIRY_DT") or ""
            strike = _clean_float(r.get("STRIKE_PR"))
            opt_type = (r.get("OPTION_TYP") or "XX").upper()
            open_pr = _clean_float(r.get("OPEN"))
            high_pr = _clean_float(r.get("HIGH"))
            low_pr = _clean_float(r.get("LOW"))
            close_pr = _clean_float(r.get("CLOSE"))
            settle_pr = _clean_float(r.get("SETTLE_PR")) or close_pr
            vol = int(_clean_float(r.get("CONTRACTS")))
            oi = _clean_float(r.get("OPEN_INT"))
            chg_oi = _clean_float(r.get("CHG_IN_OI"))

        if not sym or not inst:
            continue

        try:
            row_date = parse_date(dt_raw)
            file_dates.add(row_date)
        except ValueError:
            row_date = expected_date

        try:
            expiry_d = parse_date(xpry_raw)
        except ValueError:
            expiry_d = row_date + timedelta(days=30)

        records.append({
            "date": row_date,
            "symbol": sym,
            "instrument": inst.upper(),
            "expiry_date": expiry_d,
            "strike_price": strike,
            "option_type": opt_type,
            "open": open_pr,
            "high": high_pr,
            "low": low_pr,
            "close": close_pr,
            "settle": settle_pr,
            "contracts": vol,
            "open_interest": oi,
            "chg_in_oi": chg_oi,
        })

    if file_dates and expected_date not in file_dates:
        # Check if it's stale
        max_d = max(file_dates)
        if max_d < expected_date:
            raise StaleFile(f"F&O file contains date {max_d.isoformat()} instead of expected {expected_date.isoformat()}.")
        raise WrongDate(f"F&O file dates {list(file_dates)} do not match {expected_date.isoformat()}.")

    return records


# --------------------------------------------------------------------------- #
# Aggregation & Derived Analytics
# --------------------------------------------------------------------------- #
def classify_buildup(price_chg_pct: float, oi_chg_pct: float) -> str:
    """
    Standard derivatives buildup classification:
      - Long Buildup: Price >= +0.25% & OI >= +2.0%
      - Short Covering: Price >= +0.25% & OI <= -2.0%
      - Short Buildup: Price <= -0.25% & OI >= +2.0%
      - Long Unwinding: Price <= -0.25% & OI <= -2.0%
      - Neutral: within bounds
    """
    if price_chg_pct >= 0.25 and oi_chg_pct >= 2.0:
        return "LONG_BUILDUP"
    elif price_chg_pct >= 0.25 and oi_chg_pct <= -2.0:
        return "SHORT_COVERING"
    elif price_chg_pct <= -0.25 and oi_chg_pct >= 2.0:
        return "SHORT_BUILDUP"
    elif price_chg_pct <= -0.25 and oi_chg_pct <= -2.0:
        return "LONG_UNWINDING"
    return "NEUTRAL"


def aggregate_symbol_derivatives(records: List[Dict], trading_date: date) -> Dict[str, Dict]:
    """
    Groups contracts by underlying symbol and computes:
      - Total futures OI & net change %
      - Near-month future contract metrics (expiry, close, settle, intraday move)
      - Buildup state
      - Put-Call Ratio (PCR) by Open Interest and Traded Volume
      - Call Wall (Max Call OI strike within +/- 15% of price)
      - Put Wall (Max Put OI strike within +/- 15% of price)
    """
    by_symbol: Dict[str, List[Dict]] = {}
    for r in records:
        by_symbol.setdefault(r["symbol"], []).append(r)

    results = {}

    for sym, contracts in by_symbol.items():
        fut_contracts = [c for c in contracts if c["instrument"] in ("FUTSTK", "FUTIDX")]
        opt_contracts = [c for c in contracts if c["instrument"] in ("OPTSTK", "OPTIDX")]

        if not fut_contracts and not opt_contracts:
            continue

        # Sort futures by expiry
        fut_contracts.sort(key=lambda x: x["expiry_date"])
        near_fut = fut_contracts[0] if fut_contracts else None

        # Total Futures OI
        total_fut_oi = sum(c["open_interest"] for c in fut_contracts)
        total_fut_chg_oi = sum(c["chg_in_oi"] for c in fut_contracts)
        prior_fut_oi = total_fut_oi - total_fut_chg_oi
        fut_oi_chg_pct = (total_fut_chg_oi / prior_fut_oi * 100.0) if prior_fut_oi > 0 else 0.0

        # Futures Price change estimation
        near_close = near_fut["close"] if near_fut else 0.0
        near_open = near_fut["open"] if near_fut else 0.0
        near_expiry = near_fut["expiry_date"].isoformat() if near_fut else None

        if near_open > 0:
            fut_price_chg_pct = ((near_close - near_open) / near_open) * 100.0
        else:
            fut_price_chg_pct = 0.0

        buildup = classify_buildup(fut_price_chg_pct, fut_oi_chg_pct)

        # Options Aggregation (focus on near monthly expiry)
        expiries = sorted({c["expiry_date"] for c in opt_contracts})
        near_opt_expiry = expiries[0] if expiries else None
        near_opts = [c for c in opt_contracts if c["expiry_date"] == near_opt_expiry] if near_opt_expiry else []

        call_opts = [c for c in near_opts if c["option_type"] == "CE"]
        put_opts = [c for c in near_opts if c["option_type"] == "PE"]

        total_call_oi = sum(c["open_interest"] for c in call_opts)
        total_put_oi = sum(c["open_interest"] for c in put_opts)
        total_call_vol = sum(c["contracts"] for c in call_opts)
        total_put_vol = sum(c["contracts"] for c in put_opts)

        pcr_oi = round(total_put_oi / total_call_oi, 2) if total_call_oi > 0 else 1.0
        pcr_vol = round(total_put_vol / total_call_vol, 2) if total_call_vol > 0 else 1.0

        # Reference price for strike filtering (+/- 15% window)
        ref_price = near_close if near_close > 0 else (call_opts[0]["close"] if call_opts else 100.0)
        min_strike = ref_price * 0.85
        max_strike = ref_price * 1.15

        # Call Wall (Max Call OI in +/- 15% window)
        filtered_calls = [c for c in call_opts if min_strike <= c["strike_price"] <= max_strike]
        if not filtered_calls:
            filtered_calls = call_opts  # fallback to all if none in window
        call_wall_contract = max(filtered_calls, key=lambda x: x["open_interest"]) if filtered_calls else None
        call_wall = call_wall_contract["strike_price"] if call_wall_contract else None
        call_wall_oi = call_wall_contract["open_interest"] if call_wall_contract else 0.0

        # Put Wall (Max Put OI in +/- 15% window)
        filtered_puts = [c for c in put_opts if min_strike <= c["strike_price"] <= max_strike]
        if not filtered_puts:
            filtered_puts = put_opts
        put_wall_contract = max(filtered_puts, key=lambda x: x["open_interest"]) if filtered_puts else None
        put_wall = put_wall_contract["strike_price"] if put_wall_contract else None
        put_wall_oi = put_wall_contract["open_interest"] if put_wall_contract else 0.0

        is_index = any(c["instrument"] in ("FUTIDX", "OPTIDX") for c in contracts)

        results[sym] = {
            "symbol": sym,
            "is_index": is_index,
            "date": trading_date.isoformat(),
            "near_expiry": near_expiry,
            "near_future_close": round(near_close, 2),
            "total_fut_oi": int(total_fut_oi),
            "total_fut_chg_oi": int(total_fut_chg_oi),
            "fut_oi_change_pct": round(fut_oi_chg_pct, 2),
            "fut_price_change_pct": round(fut_price_chg_pct, 2),
            "buildup": buildup,
            "call_wall": call_wall,
            "call_wall_oi": int(call_wall_oi),
            "put_wall": put_wall,
            "put_wall_oi": int(put_wall_oi),
            "pcr_oi": pcr_oi,
            "pcr_vol": pcr_vol,
        }

    return results


# --------------------------------------------------------------------------- #
# Runner & File Persistence
# --------------------------------------------------------------------------- #
def run_fo_fetch(
    target_date: Optional[date] = None,
    file_path: Optional[str] = None,
    data_dir: str = "data",
    config_dir: str = "pipeline/config",
    poll: bool = False,
    until_str: Optional[str] = None,
    sleep=time.sleep,
) -> int:
    """
    Main orchestration entry point.
    Returns standard exit code (0 = OK, 2 = not published, 3 = failed).
    """
    holidays_file = os.path.join(config_dir, "holidays.txt")
    holidays = load_holidays(holidays_file)
    now = now_ist()

    if target_date is None:
        target_date = default_trading_date(now, holidays)

    if not is_trading_day(target_date, holidays):
        print(f"Date {target_date.isoformat()} is a weekend or holiday. Skipping F&O fetch.")
        return 0

    iso_date = target_date.isoformat()
    raw_dir = os.path.join(data_dir, "raw", iso_date)
    fo_dir = os.path.join(data_dir, "fo")
    os.makedirs(fo_dir, exist_ok=True)

    print(f"Fetching/processing NSE F&O Bhavcopy for {iso_date}...")

    # Load from local file or download
    csv_text = None
    source_desc = None
    sha256 = None

    if file_path:
        if not os.path.exists(file_path):
            print(f"Error: Specified local file does not exist: {file_path}", file=sys.stderr)
            return 3
        with open(file_path, "rb") as f:
            raw_bytes = f.read()
        sha256 = hashlib.sha256(raw_bytes).hexdigest()
        csv_text = extract_csv_from_zip(raw_bytes)
        source_desc = f"local_file:{os.path.basename(file_path)}"
    else:
        # Polling loop if requested
        until_time = None
        if until_str:
            h, m = map(int, until_str.split(":"))
            until_time = now.replace(hour=h, minute=m, second=0, microsecond=0)

        while True:
            try:
                raw_bytes, source_desc = download_fo_bytes(target_date, raw_dir, sleep=sleep)
                sha256 = hashlib.sha256(raw_bytes).hexdigest()
                csv_text = extract_csv_from_zip(raw_bytes)
                break
            except NotPublished as e:
                if not poll:
                    print(f"F&O Bhavcopy not published: {e}", file=sys.stderr)
                    return 2
                cur_now = now_ist()
                if until_time and cur_now >= until_time:
                    print(f"F&O Bhavcopy not published before {until_str}. Polling expired.", file=sys.stderr)
                    return 2
                print(f"F&O Bhavcopy not yet available. Waiting 60s to retry...")
                sleep(60)
            except Blocked as e:
                print(f"Blocked by NSE: {e}", file=sys.stderr)
                return 3
            except (FetchFailed, SchemaError, Exception) as e:
                print(f"F&O fetch failed: {e}", file=sys.stderr)
                return 3

    # Parse records
    try:
        records = parse_fo_records(csv_text, target_date)
    except StaleFile as e:
        print(f"Stale F&O file encountered: {e}", file=sys.stderr)
        return 2
    except (DataError, Exception) as e:
        print(f"F&O Data error: {e}", file=sys.stderr)
        return 3

    # Aggregate by symbol
    aggregated = aggregate_symbol_derivatives(records, target_date)

    # Separate stocks from indices
    indices = {sym: d for sym, d in aggregated.items() if d["is_index"]}
    stocks = {sym: d for sym, d in aggregated.items() if not d["is_index"]}

    # Write output JSON
    out_json = os.path.join(fo_dir, f"{iso_date}.json")
    payload = {
        "date": iso_date,
        "source": source_desc,
        "sha256": sha256,
        "stocks_count": len(stocks),
        "indices_count": len(indices),
        "indices": indices,
        "stocks": stocks,
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    # Write output CSV (flattened for stocks)
    out_csv = os.path.join(fo_dir, f"{iso_date}.csv")
    csv_cols = [
        "symbol", "date", "near_expiry", "near_future_close", "total_fut_oi",
        "total_fut_chg_oi", "fut_oi_change_pct", "fut_price_change_pct",
        "buildup", "call_wall", "call_wall_oi", "put_wall", "put_wall_oi",
        "pcr_oi", "pcr_vol"
    ]
    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_cols, extrasaction="ignore")
        writer.writeheader()
        for sym in sorted(stocks.keys()):
            writer.writerow(stocks[sym])

    # Write health fragment
    health_json = os.path.join(fo_dir, f"{iso_date}.health.json")
    health = {
        "source": SOURCE,
        "date": iso_date,
        "status": "OK",
        "stocks_count": len(stocks),
        "indices_count": len(indices),
        "sha256": sha256,
    }
    with open(health_json, "w", encoding="utf-8") as f:
        json.dump(health, f, indent=2)

    print(f"[OK] F&O Bhavcopy processed successfully for {iso_date}.")
    print(f"     Equities: {len(stocks)} | Indices: {len(indices)}")
    print(f"     Saved: {out_json}")
    print(f"     Saved: {out_csv}")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description="NSE F&O (Derivatives) Bhavcopy Fetcher & Normalizer")
    parser.add_argument("--date", type=str, default=None, help="Trading date (YYYY-MM-DD)")
    parser.add_argument("--file", type=str, default=None, help="Local F&O archive file path")
    parser.add_argument("--data-dir", type=str, default="data", help="Output directory")
    parser.add_argument("--config-dir", type=str, default="pipeline/config", help="Config directory")
    parser.add_argument("--poll", action="store_true", help="Poll until file is published")
    parser.add_argument("--until", type=str, default="21:00", help="Polling cutoff time (HH:MM IST)")
    args = parser.parse_args()

    target_d = parse_date(args.date) if args.date else None
    code = run_fo_fetch(
        target_date=target_d,
        file_path=args.file,
        data_dir=args.data_dir,
        config_dir=args.config_dir,
        poll=args.poll,
        until_str=args.until,
    )
    sys.exit(code)


if __name__ == "__main__":
    main()

