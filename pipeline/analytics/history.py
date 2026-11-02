"""
history.py - compact daily price store, trading calendar, gap detection and split/bonus adjustment.

Store layout (under data/history/):
  YYYY-MM-DD.csv.gz   one file per OPEN trading day: isin,symbol,open,high,low,close,prev_close,volume
  calendar.json       {"YYYY-MM-DD": "open" | "closed"}  (a weekday absent from it is UNKNOWN)

A day is "closed" only when NSE answered with no new file for a past date (404, a web page, or the
previous day's file: what NSE does on a holiday). Nothing is ever filled in for a closed or unknown day.

Adjustment uses NSE's own reference price: on an ex-date NSE sets that day's PREV_CLOSE to the
adjusted previous close. So  r = prev_close(t) / close(t-1)  is the factor the exchange applied.
  - |r - 1| > large_threshold  -> split / bonus / demerger: all EARLIER prices are multiplied by r
    and EARLIER volumes divided by r, so the series is continuous.
  - smaller deviations (dividends, small rights issues) are NOT applied: vendors' default charts
    leave them unadjusted too, and applying them would make indicators disagree with vendors.
r is only trusted when t-1 is the immediately preceding OPEN day for that stock. If the stock has no
bar on that day, the pair is a GAP: nothing is inferred and the gap is counted (it blocks BUY NOW).
"""
import csv
import gzip
import json
import os
from array import array

COLS = ["isin", "symbol", "open", "high", "low", "close", "prev_close", "volume"]
FIELDS = ("open", "high", "low", "close", "prev_close", "volume")


# ----------------------------------------------------------------- store
def day_path(hist_dir, iso):
    return os.path.join(hist_dir, f"{iso}.csv.gz")


def write_day(hist_dir, iso, rows):
    os.makedirs(hist_dir, exist_ok=True)
    tmp = day_path(hist_dir, iso) + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        for r in sorted(rows, key=lambda x: x["isin"]):
            w.writerow([r[c] for c in COLS])
    os.replace(tmp, day_path(hist_dir, iso))


def read_day(hist_dir, iso):
    out = []
    with gzip.open(day_path(hist_dir, iso), "rt", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            out.append({"isin": r["isin"], "symbol": r["symbol"], **{k: float(r[k]) for k in FIELDS}})
    return out


def load_calendar(hist_dir):
    p = os.path.join(hist_dir, "calendar.json")
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_calendar(hist_dir, cal):
    os.makedirs(hist_dir, exist_ok=True)
    p = os.path.join(hist_dir, "calendar.json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(cal.items())), f, indent=1)
    os.replace(tmp, p)


# ----------------------------------------------------------------- series
class Series:
    """One stock's bars as parallel arrays (compact in memory)."""
    __slots__ = ("isin", "symbol", "idx", "o", "h", "l", "c", "pc", "v")

    def __init__(self, isin, symbol):
        self.isin, self.symbol = isin, symbol
        self.idx = array("l")                        # position of each bar in the open-day list
        self.o, self.h, self.l, self.c, self.pc, self.v = (array("d") for _ in range(6))

    def add(self, i, r):
        self.idx.append(i)
        self.o.append(r["open"]); self.h.append(r["high"]); self.l.append(r["low"])
        self.c.append(r["close"]); self.pc.append(r["prev_close"]); self.v.append(r["volume"])


def open_days(cal, up_to_iso=None):
    return sorted(d for d, s in cal.items() if s == "open" and (up_to_iso is None or d <= up_to_iso))


def load_series(hist_dir, days, only_isins=None):
    """{isin: Series} over the given open days (already sorted)."""
    out = {}
    for i, d in enumerate(days):
        for r in read_day(hist_dir, d):
            if only_isins is not None and r["isin"] not in only_isins:
                continue
            s = out.get(r["isin"])
            if s is None:
                s = out[r["isin"]] = Series(r["isin"], r["symbol"])
            s.symbol = r["symbol"]                    # latest symbol wins
            s.add(i, r)
    return out


# ----------------------------------------------------------------- adjustment
def analyse(series, large_threshold=0.10, minor_floor=0.0005):
    """Find events and gaps for one Series.

    Returns {"events": [(bar_index, r)], "minor": [(bar_index, r)], "gaps": missing_open_days}.
    bar_index j means the factor r applies between bar j-1 and bar j.
    """
    events, minor, gaps = [], [], 0
    for j in range(1, len(series.idx)):
        step = series.idx[j] - series.idx[j - 1]
        if step != 1:
            gaps += step - 1                         # open days with no bar for this stock
            continue                                 # cannot infer anything across a gap
        prev_close = series.c[j - 1]
        if prev_close <= 0 or series.pc[j] <= 0:
            continue
        r = series.pc[j] / prev_close
        dev = abs(r - 1.0)
        if dev > large_threshold:
            events.append((j, r))
        elif dev > minor_floor:
            minor.append((j, r))
    return {"events": events, "minor": minor, "gaps": gaps}


def adjusted_arrays(series, events):
    """Adjusted (open, high, low, close, volume) lists. Bars before an event are scaled by r."""
    n = len(series.idx)
    by_bar = dict(events)
    o, h, l, c, v = ([0.0] * n for _ in range(5))
    f = 1.0
    for j in range(n - 1, -1, -1):
        o[j], h[j], l[j], c[j] = series.o[j] * f, series.h[j] * f, series.l[j] * f, series.c[j] * f
        v[j] = series.v[j] / f
        if j in by_bar:
            f *= by_bar[j]
    return o, h, l, c, v
