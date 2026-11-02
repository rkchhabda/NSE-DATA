#!/usr/bin/env python3
"""
nse_bhavcopy.py - download, parse, validate and normalise NSE equity Bhavcopy.

Produces the official EOD close, volume and delivery % for every EQ stock, and
partial evidence records ({source, value, as_of}) that scripts/gate_check.py
(nse-data-integrity-engine skill) can consume.

Typical use (from the workspace root):
    py pipeline\\fetchers\\nse_bhavcopy.py --date 2026-10-29
    py pipeline\\fetchers\\nse_bhavcopy.py --poll --until 21:00      # evening run
    py pipeline\\fetchers\\nse_bhavcopy.py --date 2026-10-29 ^
          --delivery-file C:\\Downloads\\sec_bhavdata_full_29102026.csv   # manual file

Exit codes: 0 OK or non-trading day, 2 not published (MISSING), 3 FAILED.

Standard library only. Never estimates or fills a value: a row that fails a
check is marked unusable and produces no evidence.

NOTE (verify): file URLs and column names below were correct when written.
NSE has changed formats before (UDiFF, July 2024). If a column is missing the
parser stops and lists the columns it found rather than guessing.
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

IST = timezone(timedelta(hours=5, minutes=30))  # India has no DST; avoids tzdata dependency
SOURCE = "nse_bhavcopy"
EPS = 0.01          # one paisa: tolerance for float / rounding noise
MAX_PRIOR_GAP_DAYS = 4

URLS = {  # verify against NSE before relying on these
    "delivery": "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv",
    "udiff": "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip",
}
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}
_OPENER = None  # tests inject an opener that bypasses system proxies

DELIVERY_REQUIRED = ["SYMBOL", "SERIES", "DATE1", "PREV_CLOSE", "OPEN_PRICE",
                     "HIGH_PRICE", "LOW_PRICE", "CLOSE_PRICE", "TTL_TRD_QNTY"]
UDIFF_REQUIRED = ["TCKRSYMB", "SCTYSRS", "ISIN", "TRADDT", "CLSPRIC", "TTLTRADGVOL"]

ERROR_FLAGS = {"missing_price", "bad_ohlc", "close_outside_range", "open_outside_range",
               "bad_volume", "deliv_gt_volume", "bad_deliv_pct",
               "udiff_close_mismatch", "udiff_volume_mismatch"}
OUT_COLS = ["symbol", "series", "date", "isin", "prev_close", "open", "high", "low", "last",
            "close", "avg_price", "volume", "turnover_lacs", "trades", "deliv_qty",
            "deliv_pct", "usable", "flags"]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class FetchError(Exception):
    pass


class NotPublished(FetchError):
    """HTTP 404: file not (yet) on the server."""


class Blocked(FetchError):
    """HTTP 403: the server is refusing us. Do not retry or work around."""


class FetchFailed(FetchError):
    """Network or server error persisted after all retries."""


class DataError(Exception):
    pass


class SchemaError(DataError):
    pass


class WrongDate(DataError):
    pass


class Truncated(DataError):
    pass


# --------------------------------------------------------------------------- #
# Dates and calendar
# --------------------------------------------------------------------------- #
def now_ist():
    return datetime.now(IST)


def parse_date(s):
    s = str(s).strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date {s!r}")


def load_holidays(path):
    """One YYYY-MM-DD per line; '#' starts a comment. Missing file = no holidays."""
    days = set()
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    days.add(parse_date(line))
    return days


def is_trading_day(d, holidays):
    return d.weekday() < 5 and d not in holidays


def default_trading_date(now, holidays):
    """Today if it is a trading day and past 18:30 IST, else the previous trading day."""
    d = now.date()
    if is_trading_day(d, holidays) and now.timetz().replace(tzinfo=None) >= dtime(18, 30):
        return d
    d -= timedelta(days=1)
    while not is_trading_day(d, holidays):
        d -= timedelta(days=1)
    return d


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _backoff(attempt):
    return 2 ** attempt + random.random()  # about 2s, 4s, 8s plus jitter


def _retry_after(err):
    try:
        return min(60.0, float(err.headers.get("Retry-After", "")))
    except (TypeError, ValueError, AttributeError):
        return None


def fetch_bytes(url, retries=3, timeout=30, sleep=time.sleep):
    """GET with retry. 404 -> NotPublished at once; 403 -> Blocked at once (no hammering)."""
    last = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers=HEADERS)
        wait = _backoff(attempt)
        try:
            opener = _OPENER or urllib.request.build_opener()
            with opener.open(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotPublished(url)
            if e.code == 403:
                raise Blocked(f"HTTP 403 from {url}: the server refused automated access")
            last = f"HTTP {e.code}"
            wait = _retry_after(e) or wait
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = str(e)
        if attempt < retries:
            sleep(wait)
    raise FetchFailed(f"{url}: {last} (after {retries} attempts)")


def fetch_with_poll(url, poll, deadline, interval, now, sleep, retries=3):
    while True:
        try:
            return fetch_bytes(url, retries=retries, sleep=sleep)
        except NotPublished:
            if not poll or now() >= deadline:
                raise
            sleep(interval)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def decode(raw):
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _num(v):
    if v is None:
        return None
    v = str(v).strip().replace(",", "")
    if v in ("", "-", "NA", "N/A", "nan"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _read_csv(text, required, label):
    reader = csv.reader(io.StringIO(text))
    try:
        header = [h.strip().upper() for h in next(reader)]
    except StopIteration:
        raise SchemaError(f"{label}: file is empty")
    missing = [c for c in required if c not in header]
    if missing:
        raise SchemaError(f"{label}: missing columns {missing}. Columns found: {header}")
    idx = {h: i for i, h in enumerate(header)}
    rows = []
    for rec in reader:
        if not rec or all(not c.strip() for c in rec):
            continue
        rows.append({h: (rec[i].strip() if i < len(rec) else "") for h, i in idx.items()})
    return rows


def parse_delivery_csv(text):
    rows = []
    for r in _read_csv(text, DELIVERY_REQUIRED, "delivery file"):
        try:
            d = parse_date(r["DATE1"])
        except ValueError:
            raise DataError(f"delivery file: bad date {r['DATE1']!r} for {r['SYMBOL']}")
        rows.append({
            "symbol": r["SYMBOL"].upper(), "series": r["SERIES"].upper(), "date": d.isoformat(),
            "prev_close": _num(r["PREV_CLOSE"]), "open": _num(r["OPEN_PRICE"]),
            "high": _num(r["HIGH_PRICE"]), "low": _num(r["LOW_PRICE"]),
            "last": _num(r.get("LAST_PRICE")), "close": _num(r["CLOSE_PRICE"]),
            "avg_price": _num(r.get("AVG_PRICE")), "volume": _num(r["TTL_TRD_QNTY"]),
            "turnover_lacs": _num(r.get("TURNOVER_LACS")), "trades": _num(r.get("NO_OF_TRADES")),
            "deliv_qty": _num(r.get("DELIV_QTY")), "deliv_pct": _num(r.get("DELIV_PER")),
        })
    return rows


def parse_udiff_zip(raw):
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise DataError("UDiFF file is not a valid zip")
    names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
    if not names:
        raise DataError("UDiFF zip contains no CSV")
    text = decode(zf.read(names[0]))
    out = []
    for r in _read_csv(text, UDIFF_REQUIRED, "UDiFF file"):
        try:
            d = parse_date(r["TRADDT"])
        except ValueError:
            raise DataError(f"UDiFF file: bad date {r['TRADDT']!r}")
        out.append({"symbol": r["TCKRSYMB"].upper(), "series": r["SCTYSRS"].upper(),
                    "isin": r["ISIN"].upper() or None, "date": d.isoformat(),
                    "close": _num(r["CLSPRIC"]), "volume": _num(r["TTLTRADGVOL"])})
    return out


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_file(rows, expected_date, min_rows, prior_count=None):
    """Whole-file checks. Raises WrongDate / Truncated; these make the source FAILED."""
    if not rows:
        raise Truncated("file has no data rows")
    dates = sorted({r["date"] for r in rows})
    if dates != [expected_date]:
        raise WrongDate(f"file dates {dates} do not equal expected {expected_date}")
    floor = min_rows
    if prior_count:
        floor = max(floor, int(0.8 * prior_count))
    if len(rows) < floor:
        raise Truncated(f"only {len(rows)} rows (expected at least {floor}); possible partial download")


def check_row(r):
    """Per-row sanity checks. Returns flags; any flag in ERROR_FLAGS makes the row unusable."""
    flags = []
    o, h, l, c = r["open"], r["high"], r["low"], r["close"]
    v = r["volume"]
    if v is None or v < 0:
        flags.append("bad_volume")
    if c is None or h is None or l is None:
        flags.append("missing_price")
    elif v == 0:
        flags.append("no_trades")  # O/H/L can be zero on a no-trade day; do not range-check
    else:
        if h < l - EPS:
            flags.append("bad_ohlc")
        if not (l - EPS <= c <= h + EPS):
            flags.append("close_outside_range")
        if o is not None and not (l - EPS <= o <= h + EPS):
            flags.append("open_outside_range")
    if r["deliv_qty"] is not None and v is not None and r["deliv_qty"] > v + 0.5:
        flags.append("deliv_gt_volume")
    if r["deliv_pct"] is not None and not (0 <= r["deliv_pct"] <= 100):
        flags.append("bad_deliv_pct")
    return flags


def merge_and_check(delivery_rows, udiff_rows, series_keep):
    """Join the two files by (symbol, series), add ISIN, cross-check, flag, filter series."""
    ud = {(u["symbol"], u["series"]): u for u in (udiff_rows or [])}
    out = []
    for r in delivery_rows:
        if series_keep and r["series"] not in series_keep:
            continue
        r = dict(r)
        flags = check_row(r)
        u = ud.get((r["symbol"], r["series"]))
        r["isin"] = u["isin"] if u else None
        if u:  # same origin, two files: they must agree exactly
            if None not in (r["close"], u["close"]) and abs(r["close"] - u["close"]) > EPS:
                flags.append("udiff_close_mismatch")
            if None not in (r["volume"], u["volume"]) and abs(r["volume"] - u["volume"]) > 0.5:
                flags.append("udiff_volume_mismatch")
        r["flags"] = flags
        out.append(r)
    return out


def load_prior(out_dir, trading_date):
    """Latest normalised file strictly before trading_date and within MAX_PRIOR_GAP_DAYS."""
    folder = os.path.join(out_dir, "bhavcopy")
    if not os.path.isdir(folder):
        return None, {}
    best = None
    for name in os.listdir(folder):
        stem = name[:-4]
        if name.endswith(".csv") and len(stem) == 10:
            try:
                d = date.fromisoformat(stem)
            except ValueError:
                continue
            if d < trading_date and (best is None or d > best):
                best = d
    if best is None or (trading_date - best).days > MAX_PRIOR_GAP_DAYS:
        return best, {}
    prior = {}
    with open(os.path.join(folder, f"{best.isoformat()}.csv"), encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            prior[(row["symbol"], row["series"])] = _num(row["close"])
    return best, prior


def flag_corporate_actions(rows, prior, tol_pct=0.05):
    """Today's PREV_CLOSE differing from yesterday's stored close means an ex-date adjustment."""
    n = 0
    for r in rows:
        pc, prev = r["prev_close"], prior.get((r["symbol"], r["series"]))
        if pc is None or prev is None:
            continue
        if abs(pc - prev) > max(EPS, abs(prev) * tol_pct / 100.0):
            r["flags"].append("possible_corporate_action")
            n += 1
    return n


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def atomic_write(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(tmp, mode, **({} if mode == "wb" else {"encoding": "utf-8", "newline": ""})) as f:
        f.write(data)
    os.replace(tmp, path)


def write_normalised(path, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(OUT_COLS)
    for r in sorted(rows, key=lambda x: (x["symbol"], x["series"])):
        r["usable"] = not (set(r["flags"]) & ERROR_FLAGS)
        values = []
        for c in OUT_COLS:
            if c == "flags":
                values.append(";".join(r["flags"]))
            elif c == "usable":
                values.append(str(r["usable"]))
            else:
                v = r.get(c)
                values.append("" if v is None else v)
        w.writerow(values)
    atomic_write(path, buf.getvalue())


def to_evidence(r, trading_date):
    """Partial gate input for one stock. None if the row is unusable (never fill in)."""
    if set(r["flags"]) & ERROR_FLAGS or r["close"] is None or r["volume"] is None:
        return None
    rec = lambda v: [{"source": SOURCE, "value": v, "as_of": trading_date}]
    return {
        "symbol": r["symbol"], "isin": r.get("isin"), "trading_date": trading_date,
        "evidence": {"cmp": rec(r["close"]), "volume": rec(r["volume"])},
        "extras": {"series": r["series"], "prev_close": r["prev_close"],
                   "delivery_pct": r["deliv_pct"], "delivery_qty": r["deliv_qty"],
                   "flags": r["flags"],
                   "needs_adjustment_check": "possible_corporate_action" in r["flags"]},
    }


def sha256(b):
    return hashlib.sha256(b).hexdigest()


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run(trading_date, out_dir="data", series=("EQ",), min_rows=1000, poll=False,
        until="21:00", interval=600, refresh=False, no_udiff=False,
        delivery_file=None, udiff_file=None, holidays=None, force=False,
        emit_evidence=None, health_out=None,
        now=now_ist, sleep=time.sleep):
    """Returns (exit_code, summary_dict)."""
    iso = trading_date.isoformat()
    holidays = holidays or set()
    health = {"run_date": iso, "sources": {}, "fallbacks_used": [], "validation_failures": []}
    summary = {"date": iso, "status": None, "messages": []}
    health_path = health_out or os.path.join(out_dir, "bhavcopy", f"{iso}.health.json")

    def finish(code, status, bhav_health, msg=None):
        summary["status"] = status
        if msg:
            summary["messages"].append(msg)
            if bhav_health == "FAILED":
                health["validation_failures"].append(f"Bhavcopy {iso}: {msg}")
        health["sources"]["bhavcopy"] = bhav_health
        if bhav_health != "NOT RUN":
            atomic_write(health_path, json.dumps(health, indent=2))
            summary["health_file"] = health_path
        return code, summary

    if not force and not is_trading_day(trading_date, holidays):
        return finish(0, "NON_TRADING_DAY", "NOT RUN",
                      f"{iso} is a weekend or listed holiday; nothing fetched")

    hh, mm = (int(x) for x in until.split(":"))
    n = now()
    deadline = datetime.combine(n.date(), dtime(hh, mm), tzinfo=IST)
    raw_dir = os.path.join(out_dir, "raw", iso)
    ddmmyyyy, yyyymmdd = trading_date.strftime("%d%m%Y"), trading_date.strftime("%Y%m%d")
    manifest = {"date": iso, "fetched_at_ist": n.isoformat(timespec="seconds"), "files": {}}

    def get(kind, fname, local):
        """Return raw bytes from a local file, the cache, or the network."""
        if local:
            with open(local, "rb") as f:
                data = f.read()
            manifest["files"][kind] = {"source": "local file", "path": local, "sha256": sha256(data)}
            return data
        cache = os.path.join(raw_dir, fname)
        if os.path.exists(cache) and not refresh:
            with open(cache, "rb") as f:
                data = f.read()
            manifest["files"][kind] = {"source": "cache", "path": cache, "sha256": sha256(data)}
            return data
        url = URLS[kind].format(ddmmyyyy=ddmmyyyy, yyyymmdd=yyyymmdd)
        data = fetch_with_poll(url, poll if kind == "delivery" else False,
                               deadline, interval, now, sleep)
        atomic_write(cache, data)
        manifest["files"][kind] = {"source": url, "path": cache, "sha256": sha256(data)}
        return data

    # ---- primary file: delivery Bhavcopy (close, volume, delivery %)
    try:
        raw = get("delivery", f"sec_bhavdata_full_{ddmmyyyy}.csv", delivery_file)
    except NotPublished:
        return finish(2, "MISSING", "FAILED",
                      "Bhavcopy not published (HTTP 404) by the polling deadline; EOD fields are MISSING. "
                      "Do not substitute another source for the official close.")
    except Blocked as e:
        return finish(3, "FAILED", "FAILED", f"{e}. Stopped; not retrying. Download the file manually "
                      "and pass it with --delivery-file.")
    except FetchError as e:
        return finish(3, "FAILED", "FAILED", str(e))
    except OSError as e:
        return finish(3, "FAILED", "FAILED", f"cannot read file: {e}")

    prior_date, prior = load_prior(out_dir, trading_date)
    prior_count = None
    if prior_date:
        pm = os.path.join(out_dir, "bhavcopy", f"{prior_date.isoformat()}.manifest.json")
        if os.path.exists(pm):
            with open(pm, encoding="utf-8") as f:
                prior_count = json.load(f).get("rows_total")

    try:
        delivery_rows = parse_delivery_csv(decode(raw))
        validate_file(delivery_rows, iso, min_rows, prior_count)
    except DataError as e:
        atomic_write(os.path.join(raw_dir, "REJECTED.txt"), f"{type(e).__name__}: {e}\n")
        return finish(3, "FAILED", "FAILED", f"{type(e).__name__}: {e}")

    # ---- optional second file: UDiFF (adds ISIN and cross-checks the first)
    udiff_rows = None
    if not no_udiff:
        try:
            udiff_rows = parse_udiff_zip(get("udiff", f"BhavCopy_NSE_CM_{yyyymmdd}.csv.zip", udiff_file))
            if sorted({u["date"] for u in udiff_rows}) != [iso]:
                raise WrongDate("UDiFF file date does not match")
        except (FetchError, DataError, OSError) as e:
            udiff_rows = None
            summary["messages"].append(f"UDiFF file unavailable ({type(e).__name__}: {e}); "
                                       "ISIN missing and no cross-check this run")
            health["validation_failures"].append(f"UDiFF {iso}: {e}")
            manifest["files"].pop("udiff", None)

    rows = merge_and_check(delivery_rows, udiff_rows, set(series))
    n_corp = flag_corporate_actions(rows, prior) if prior else 0
    if not prior:
        summary["messages"].append("no prior-day file within 4 days: corporate-action check skipped")

    norm_path = os.path.join(out_dir, "bhavcopy", f"{iso}.csv")
    write_normalised(norm_path, rows)
    unusable = [r for r in rows if set(r["flags"]) & ERROR_FLAGS]
    mismatch = [r for r in rows if {"udiff_close_mismatch", "udiff_volume_mismatch"} & set(r["flags"])]
    if unusable:
        health["validation_failures"].append(
            f"Bhavcopy {iso}: {len(unusable)} rows failed sanity checks and were excluded "
            f"(e.g. {', '.join(r['symbol'] for r in unusable[:5])})")
    if mismatch:
        health["validation_failures"].append(
            f"Bhavcopy {iso}: delivery file and UDiFF disagree for {len(mismatch)} symbols: check parser")

    manifest.update({"rows_total": len(delivery_rows), "rows_kept": len(rows),
                     "rows_unusable": len(unusable), "possible_corporate_actions": n_corp,
                     "prior_file": prior_date.isoformat() if prior_date else None,
                     "isin_available": udiff_rows is not None, "series": sorted(series)})
    atomic_write(os.path.join(out_dir, "bhavcopy", f"{iso}.manifest.json"), json.dumps(manifest, indent=2))

    evidence = [e for e in (to_evidence(r, iso) for r in rows) if e]
    if emit_evidence:
        atomic_write(emit_evidence, json.dumps({"stocks": evidence}, indent=2))
        summary["evidence_file"] = emit_evidence

    summary.update({"rows_total": len(delivery_rows), "rows_kept": len(rows),
                    "rows_usable": len(evidence), "rows_unusable": len(unusable),
                    "possible_corporate_actions": n_corp, "normalised_file": norm_path,
                    "isin_available": udiff_rows is not None})
    return finish(0, "OK", "OK")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description="Fetch, validate and normalise NSE Bhavcopy.")
    ap.add_argument("--date", help="trading date YYYY-MM-DD (default: latest completed session)")
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--series", default="EQ", help="comma-separated series to keep (default EQ)")
    ap.add_argument("--min-rows", type=int, default=1000)
    ap.add_argument("--poll", action="store_true", help="retry while the file is not yet published")
    ap.add_argument("--until", default="21:00", help="polling deadline, IST HH:MM (default 21:00)")
    ap.add_argument("--interval", type=int, default=600, help="seconds between polls (default 600)")
    ap.add_argument("--refresh", action="store_true", help="ignore cached raw files")
    ap.add_argument("--no-udiff", action="store_true", help="skip the UDiFF file (no ISIN)")
    ap.add_argument("--delivery-file", help="use a manually downloaded sec_bhavdata_full CSV")
    ap.add_argument("--udiff-file", help="use a manually downloaded UDiFF zip")
    ap.add_argument("--holidays", default=os.path.join("pipeline", "config", "holidays.txt"))
    ap.add_argument("--force", action="store_true", help="skip the weekend/holiday check")
    ap.add_argument("--emit-evidence", help="write partial gate-input JSON to this path")
    ap.add_argument("--health-out", help="write the source-health JSON to this path")
    a = ap.parse_args(argv)

    holidays = load_holidays(a.holidays)
    d = parse_date(a.date) if a.date else default_trading_date(now_ist(), holidays)
    code, s = run(d, out_dir=a.out_dir, series={x.strip().upper() for x in a.series.split(",")},
                  min_rows=a.min_rows, poll=a.poll, until=a.until, interval=a.interval,
                  refresh=a.refresh, no_udiff=a.no_udiff, delivery_file=a.delivery_file,
                  udiff_file=a.udiff_file, holidays=holidays, force=a.force,
                  emit_evidence=a.emit_evidence, health_out=a.health_out)
    print(f"Bhavcopy {s['date']}: {s['status']}")
    for k in ("rows_total", "rows_kept", "rows_usable", "rows_unusable",
              "possible_corporate_actions", "isin_available", "normalised_file",
              "evidence_file", "health_file"):
        if k in s:
            print(f"  {k}: {s[k]}")
    for m in s["messages"]:
        print(f"  note: {m}")
    return code


if __name__ == "__main__":
    sys.exit(main())
