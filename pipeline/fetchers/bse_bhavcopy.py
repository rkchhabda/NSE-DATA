#!/usr/bin/env python3
"""
bse_bhavcopy.py - download, parse, validate and normalise the BSE equity Bhavcopy (UDiFF).

BSE is a second OFFICIAL origin, so its closing price can corroborate NSE's. It cannot
corroborate NSE volume (BSE volume is a different number). Rows with no trades are marked
`no_trades` and must not be used as price evidence: BSE carries the previous close forward.

Usage (from the workspace root):
    py pipeline\\fetchers\\bse_bhavcopy.py --date 2026-10-30
    py pipeline\\fetchers\\bse_bhavcopy.py --date 2026-10-30 --file "C:\\Users\\you\\Downloads\\BhavCopy_BSE_CM_0_0_0_20261030_F_0000.CSV"

Exit codes: 0 OK or non-trading day, 2 not published, 3 FAILED.
Standard library only. Reuses the download, retry and validation code of nse_bhavcopy.py.

NOTE (verify): the file NAME below was confirmed from a real download; the FOLDER in the
URL was not. If the download fails, save the file by hand and use --file.
"""
import argparse
import csv
import io
import json
import os
import sys
import time
import zipfile
from collections import Counter
from datetime import datetime, time as dtime

import nse_bhavcopy as nb

SOURCE = "bse_bhavcopy"
URL = "https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{yyyymmdd}_F_0000.CSV"
HEADERS = dict(nb.HEADERS, Referer="https://www.bseindia.com/")
REQUIRED = ["TRADDT", "ISIN", "TCKRSYMB", "SCTYSRS", "FININSTRMID", "FININSTRMNM", "OPNPRIC",
            "HGHPRIC", "LWPRIC", "CLSPRIC", "LASTPRIC", "PRVSCLSGPRIC", "TTLTRADGVOL",
            "TTLTRFVAL", "TTLNBOFTXSEXCTD"]
ERROR_FLAGS = nb.ERROR_FLAGS | {"duplicate_isin", "no_isin"}
OUT_COLS = ["isin", "symbol", "scrip_code", "series", "date", "name", "prev_close", "open", "high",
            "low", "last", "close", "volume", "turnover", "trades", "usable", "flags"]


def parse_bse_csv(text):
    rows = []
    for r in nb._read_csv(text, REQUIRED, "BSE file"):
        try:
            d = nb.parse_date(r["TRADDT"])
        except ValueError:
            raise nb.DataError(f"BSE file: bad date {r['TRADDT']!r} for {r['TCKRSYMB']}")
        rows.append({
            "isin": r["ISIN"].upper() or None, "symbol": r["TCKRSYMB"].upper(),
            "scrip_code": r["FININSTRMID"], "series": r["SCTYSRS"].upper(), "date": d.isoformat(),
            "name": r["FININSTRMNM"], "prev_close": nb._num(r["PRVSCLSGPRIC"]),
            "open": nb._num(r["OPNPRIC"]), "high": nb._num(r["HGHPRIC"]), "low": nb._num(r["LWPRIC"]),
            "last": nb._num(r["LASTPRIC"]), "close": nb._num(r["CLSPRIC"]),
            "volume": nb._num(r["TTLTRADGVOL"]), "turnover": nb._num(r["TTLTRFVAL"]),
            "trades": nb._num(r["TTLNBOFTXSEXCTD"]),
        })
    return rows


def bse_dates(raw):
    try:
        if raw[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                names = [x for x in z.namelist() if x.lower().endswith(".csv")]
                raw = z.read(names[0]) if names else b""
        return {r["date"] for r in parse_bse_csv(nb.decode(raw))}
    except (nb.DataError, zipfile.BadZipFile):
        return set()


def check_rows(rows):
    """Add a `flags` list to each row. Any flag in ERROR_FLAGS makes the row unusable."""
    counts = Counter(r["isin"] for r in rows if r["isin"])
    for r in rows:
        flags = nb.check_row({**r, "deliv_qty": None, "deliv_pct": None})
        if not r["isin"]:
            flags.append("no_isin")
        elif counts[r["isin"]] > 1:
            flags.append("duplicate_isin")
        r["flags"] = flags
    return rows


def write_normalised(path, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(OUT_COLS)
    for r in sorted(rows, key=lambda x: (x["symbol"], x["series"], x["scrip_code"])):
        r["usable"] = not (set(r["flags"]) & ERROR_FLAGS)
        w.writerow([";".join(r["flags"]) if c == "flags" else str(r["usable"]) if c == "usable"
                    else ("" if r.get(c) is None else r.get(c)) for c in OUT_COLS])
    nb.atomic_write(path, buf.getvalue())


def to_price_evidence(r):
    """BSE close as a price evidence record, or None if the row cannot corroborate anything."""
    if set(r["flags"]) & ERROR_FLAGS or "no_trades" in r["flags"] or r["close"] is None:
        return None
    return {"source": SOURCE, "value": r["close"], "as_of": r["date"]}


def _prior_count(out_dir, trading_date):
    folder = os.path.join(out_dir, "bse")
    best = None
    if os.path.isdir(folder):
        for n in os.listdir(folder):
            if n.endswith(".manifest.json") and len(n) == len("2026-10-30.manifest.json"):
                try:
                    d = datetime.strptime(n[:10], "%Y-%m-%d").date()
                except ValueError:
                    continue
                if d < trading_date and (best is None or d > best):
                    best = d
    if best is None or (trading_date - best).days > nb.MAX_PRIOR_GAP_DAYS:
        return None
    with open(os.path.join(folder, f"{best.isoformat()}.manifest.json"), encoding="utf-8") as f:
        return json.load(f).get("rows_total")


def run(trading_date, out_dir="data", file=None, min_rows=1000, poll=False, until="21:00",
        interval=600, refresh=False, holidays=None, force=False, url=None,
        now=nb.now_ist, sleep=time.sleep):
    iso = trading_date.isoformat()
    health = {"run_date": iso, "sources": {}, "fallbacks_used": [], "validation_failures": []}
    summary = {"date": iso, "status": None, "messages": []}
    health_path = os.path.join(out_dir, "bse", f"{iso}.health.json")

    def finish(code, status, h, msg=None):
        summary["status"] = status
        if msg:
            summary["messages"].append(msg)
            if h == "FAILED":
                health["validation_failures"].append(f"BSE Bhavcopy {iso}: {msg}")
        health["sources"][SOURCE] = h
        if h != "NOT RUN":
            nb.atomic_write(health_path, json.dumps(health, indent=2))
            summary["health_file"] = health_path
        return code, summary

    if not force and not nb.is_trading_day(trading_date, holidays or set()):
        return finish(0, "NON_TRADING_DAY", "NOT RUN", f"{iso} is a weekend or listed holiday; nothing fetched")

    hh, mm = (int(x) for x in until.split(":"))
    n = now()
    deadline = datetime.combine(n.date(), dtime(hh, mm), tzinfo=nb.IST)
    raw_dir = os.path.join(out_dir, "raw", iso)
    fname = f"BhavCopy_BSE_CM_0_0_0_{trading_date.strftime('%Y%m%d')}_F_0000.CSV"
    manifest = {"date": iso, "fetched_at_ist": n.isoformat(timespec="seconds"), "files": {}}

    try:
        if file:
            with open(file, "rb") as f:
                raw = f.read()
            manifest["files"]["bse"] = {"source": "local file", "path": file, "sha256": nb.sha256(raw)}
        else:
            cache = os.path.join(raw_dir, fname)
            if os.path.exists(cache) and not refresh:
                with open(cache, "rb") as f:
                    raw = f.read()
                manifest["files"]["bse"] = {"source": "cache", "path": cache, "sha256": nb.sha256(raw)}
            else:
                u = (url or URL).format(yyyymmdd=trading_date.strftime("%Y%m%d"))
                raw = nb.fetch_with_poll(u, poll, deadline, interval, now, sleep, headers=HEADERS,
                                        check=nb.fresh_check_for(bse_dates, iso))
                nb.atomic_write(cache, raw)
                manifest["files"]["bse"] = {"source": u, "path": cache, "sha256": nb.sha256(raw)}
    except nb.NotPublished as e:
        detail = f" Detail: {e}." if isinstance(e, nb.StaleFile) else ""
        return finish(2, "MISSING", "FAILED",
                      "BSE file not published (HTTP 404, a web page, or an older day's file) by the polling deadline." + detail)
    except nb.Blocked as e:
        return finish(3, "FAILED", "FAILED", f"{e}. Stopped; not retrying. Save the file by hand and pass --file.")
    except nb.FetchError as e:
        return finish(3, "FAILED", "FAILED", str(e))
    except OSError as e:
        return finish(3, "FAILED", "FAILED", f"cannot read file: {e}")

    try:
        if raw[:2] == b"PK":  # zipped
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                names = [x for x in z.namelist() if x.lower().endswith(".csv")]
                if not names:
                    raise nb.DataError("zip contains no CSV")
                raw = z.read(names[0])
        rows = parse_bse_csv(nb.decode(raw))
        nb.validate_file(rows, iso, min_rows, _prior_count(out_dir, trading_date))
    except (nb.DataError, zipfile.BadZipFile) as e:
        nb.quarantine(manifest["files"].get("bse"))
        nb.atomic_write(os.path.join(raw_dir, "REJECTED_bse.txt"), f"{type(e).__name__}: {e}\n")
        return finish(3, "FAILED", "FAILED", f"{type(e).__name__}: {e}")

    check_rows(rows)
    write_normalised(os.path.join(out_dir, "bse", f"{iso}.csv"), rows)
    bad = [r for r in rows if set(r["flags"]) & ERROR_FLAGS]
    no_trade = sum("no_trades" in r["flags"] for r in rows)
    if bad:
        health["validation_failures"].append(
            f"BSE Bhavcopy {iso}: {len(bad)} rows failed sanity checks and were excluded "
            f"(e.g. {', '.join(r['symbol'] for r in bad[:5])})")
    manifest.update({"rows_total": len(rows), "rows_unusable": len(bad), "rows_no_trades": no_trade})
    nb.atomic_write(os.path.join(out_dir, "bse", f"{iso}.manifest.json"), json.dumps(manifest, indent=2))
    summary.update({"rows_total": len(rows), "rows_unusable": len(bad), "rows_no_trades": no_trade,
                    "normalised_file": os.path.join(out_dir, "bse", f"{iso}.csv")})
    return finish(0, "OK", "OK")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fetch, validate and normalise the BSE equity Bhavcopy.")
    ap.add_argument("--date", help="trading date YYYY-MM-DD (default: latest completed session)")
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--file", help="use a manually downloaded BSE UDiFF CSV (or zip)")
    ap.add_argument("--min-rows", type=int, default=1000)
    ap.add_argument("--poll", action="store_true")
    ap.add_argument("--until", default="21:00")
    ap.add_argument("--interval", type=int, default=600)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--url", help="override the download URL template ({yyyymmdd})")
    ap.add_argument("--holidays", default=os.path.join("pipeline", "config", "holidays.txt"))
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    holidays = nb.load_holidays(a.holidays)
    d = nb.parse_date(a.date) if a.date else nb.default_trading_date(nb.now_ist(), holidays)
    code, s = run(d, out_dir=a.out_dir, file=a.file, min_rows=a.min_rows, poll=a.poll, until=a.until,
                  interval=a.interval, refresh=a.refresh, holidays=holidays, force=a.force, url=a.url)
    print(f"BSE Bhavcopy {s['date']}: {s['status']}")
    for k in ("rows_total", "rows_unusable", "rows_no_trades", "normalised_file", "health_file"):
        if k in s:
            print(f"  {k}: {s[k]}")
    for m in s["messages"]:
        print(f"  note: {m}")
    return code


if __name__ == "__main__":
    sys.exit(main())
