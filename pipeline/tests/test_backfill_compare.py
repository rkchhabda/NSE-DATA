"""Tests for pipeline/tools/backfill_compare.py: a simulated NSE and BSE serve several days of data
with known gaps, so every number in the output can be checked by hand. No internet needed."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
import urllib.request
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "fetchers"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import bse_bhavcopy as bb  # noqa: E402
import backfill_compare as bf  # noqa: E402
import nse_bhavcopy as nb  # noqa: E402
from test_bse_bhavcopy import bse_row  # noqa: E402
from test_nse_bhavcopy import Clock, LocalServer, delivery_csv, ist, row, udiff_zip, write_text  # noqa: E402

# symbol, isin, NSE close, BSE close, BSE trades
STOCKS = [("AAA", "INE000A01011", 100.0, 100.10, 500),    # 0.10% gap
          ("BBB", "INE000B01011", 200.0, 200.80, 500),    # 0.40% gap
          ("CCC", "INE000C01011", 300.0, 302.40, 500),    # 0.80% gap -> conflict
          ("DDD", "INE000D01011", 400.0, 440.00, 5)]      # 10% gap but only 5 BSE trades -> ignored


def nse_files(d, stocks=STOCKS):
    dt = d.strftime("%d-%b-%Y")
    rows = [row(sym, dt=dt, prev=c, o=c, h=c * 1.01, l=c * 0.99, last=c, c=c, avg=c, vol=100000, dq="50000", dp="50.00") for sym, _, c, _, _ in stocks]
    udiff = udiff_zip([(sym, "EQ", isin, c, 100000) for sym, isin, c, _, _ in stocks], dt=d.isoformat())
    return delivery_csv(rows).encode(), udiff


def bse_file(d, stocks=STOCKS):
    head = open_header()
    lines = [bse_row(sym, isin, close=b, prev=b, vol=1000, o=b, h=b * 1.01, l=b * 0.99, last=b, trades=t, date_s=d.isoformat())
             for sym, isin, _, b, t in stocks]
    return ("\n".join([head] + lines) + "\n").encode()


def open_header():
    from test_bse_bhavcopy import HEADER
    return HEADER


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = self.tmp.name
        self.srv = LocalServer()
        self._urls, self._bse, self._opener = dict(nb.URLS), bb.URL, nb._OPENER
        nb.URLS.update({"delivery": self.srv.base + "/sec_{ddmmyyyy}.csv", "udiff": self.srv.base + "/udiff_{yyyymmdd}.zip"})
        bb.URL = self.srv.base + "/bse_{yyyymmdd}.CSV"
        nb._OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.clock = Clock(ist(2026, 11, 1, 10, 0))

    def tearDown(self):
        self.srv.stop()
        nb.URLS.clear()
        nb.URLS.update(self._urls)
        bb.URL, nb._OPENER = self._bse, self._opener
        self.tmp.cleanup()

    def serve_day(self, d, nse=True, bse=True, stocks=STOCKS):
        if nse:
            deliv, udiff = nse_files(d, stocks)
            self.srv.routes[f"/sec_{d.strftime('%d%m%Y')}.csv"] = [(200, deliv, {})]
            self.srv.routes[f"/udiff_{d.strftime('%Y%m%d')}.zip"] = [(200, udiff, {})]
        if bse:
            self.srv.routes[f"/bse_{d.strftime('%Y%m%d')}.CSV"] = [(200, bse_file(d, stocks), {})]

    def go(self, start, end, extra=()):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = bf.main(["--from", start, "--to", end, "--out-dir", self.out, "--min-rows", "3",
                            "--holidays", os.path.join(self.out, "none.txt"), *extra],
                           sleep=self.clock.sleep, now=self.clock.now)
        return code, buf.getvalue()


class Backfill(Base):
    def test_three_days_pooled_numbers(self):
        for d in (date(2026, 10, 28), date(2026, 10, 29), date(2026, 10, 30)):
            self.serve_day(d)
        code, out = self.go("2026-10-28", "2026-10-30")
        self.assertEqual(code, 0)
        for iso in ("2026-10-28", "2026-10-29", "2026-10-30"):
            line = next(l for l in out.splitlines() if l.startswith(iso))
            self.assertIn("fetched", line)
            cols = line.split()
            self.assertEqual(cols[3:5], ["4", "3"])         # matched 4; active 3 (DDD has 5 BSE trades)
            self.assertEqual(cols[8], "1")                  # one conflict (CCC at 0.80%)
        self.assertIn("Days compared: 3 | stock-days with >= 100 BSE trades: 9", out)
        self.assertIn("pooled 66.7%", out)                  # AAA and BBB agree, CCC does not
        self.assertIn("day-to-day range 66.7% to 66.7%", out)
        self.assertIn("Stocks compared on 3+ days: 3 | in conflict on at least half of their days: 1 (33.3%)", out)
        self.assertIn("CCC", out.split("Persistent conflicts")[1])
        self.assertNotIn("DDD", out.split("Persistent conflicts")[1])   # thin BSE trading is ignored, never a conflict

    def test_persistent_means_conflict_on_at_least_half_of_days(self):
        def stocks(e, f):   # EEE conflicts when e is True, FFF when f is True, GGG never
            return [("EEE", "INE000E01011", 100.0, 100.80 if e else 100.10, 500),
                    ("FFF", "INE000F01011", 100.0, 100.80 if f else 100.10, 500),
                    ("GGG", "INE000G01011", 100.0, 100.10, 500)]
        plan = {date(2026, 10, 28): (True, True), date(2026, 10, 29): (True, False), date(2026, 10, 30): (False, False)}
        for d, (e, f) in plan.items():           # EEE conflicts on 2 of 3 days, FFF on 1 of 3
            self.serve_day(d, stocks=stocks(e, f))
        code, out = self.go("2026-10-28", "2026-10-30")
        self.assertEqual(code, 0)
        self.assertIn("Stocks compared on 3+ days: 3 | in conflict on at least half of their days: 1 (33.3%)", out)
        tail = out.split("Persistent conflicts")[1]
        self.assertIn("EEE", tail)
        self.assertNotIn("FFF", tail)
        self.assertNotIn("GGG", tail)

    def test_exactly_half_the_days_counts_as_persistent(self):
        def stocks(h):
            return [("HHH", "INE000H01011", 100.0, 100.80 if h else 100.10, 500),
                    ("III", "INE000I01011", 100.0, 100.10, 500)]
        for d, h in {date(2026, 10, 27): True, date(2026, 10, 28): False,
                     date(2026, 10, 29): True, date(2026, 10, 30): False}.items():   # HHH conflicts on 2 of 4 days
            self.serve_day(d, stocks=stocks(h))
        code, out = self.go("2026-10-27", "2026-10-30", extra=("--min-rows", "1"))   # only 2 stocks in this fixture
        self.assertEqual(code, 0)
        self.assertIn("in conflict on at least half of their days: 1 (50.0%)", out)
        self.assertIn("HHH", out.split("Persistent conflicts")[1])

    def test_unpublished_day_does_not_stop_the_run(self):
        self.serve_day(date(2026, 10, 28))
        self.serve_day(date(2026, 10, 30))     # 29 Oct is served nowhere: both files answer 404
        code, out = self.go("2026-10-28", "2026-10-30")
        self.assertEqual(code, 0)
        self.assertIn("not published", next(l for l in out.splitlines() if l.startswith("2026-10-29")))
        self.assertIn("Days compared: 2", out)

    def test_weekend_is_skipped_without_network(self):
        self.serve_day(date(2026, 10, 30))
        self.serve_day(date(2026, 11, 2))
        code, out = self.go("2026-10-30", "2026-11-02")
        dates = [l.split()[0] for l in out.splitlines() if l.startswith("2026-")]
        self.assertEqual(dates, ["2026-10-30", "2026-11-02"])   # Saturday and Sunday never appear
        self.assertNotIn("/sec_31102026.csv", self.srv.hits)

    def test_both_exchanges_failures_are_reported_and_kept_separately(self):
        d = date(2026, 10, 30)
        self.srv.routes["/sec_30102026.csv"] = [(200, b"not,a,bhavcopy\n1,2,3\n", {})]
        self.srv.routes["/bse_20261030.CSV"] = [(200, b"also,not\n1,2\n", {})]
        code, out = self.go("2026-10-30", "2026-10-30")
        line = next(l for l in out.splitlines() if l.startswith("2026-10-30"))
        self.assertIn("NSE:", line)
        self.assertIn("BSE:", line)                    # neither message hides the other
        raw = os.path.join(self.out, "raw", "2026-10-30")
        self.assertTrue(os.path.exists(os.path.join(raw, "REJECTED_nse.txt")))
        self.assertTrue(os.path.exists(os.path.join(raw, "REJECTED_bse.txt")))

    def test_closed_day_exactly_as_the_servers_really_answer(self):
        # 20 Oct 2026 (Dussehra): NSE returned the previous day's file under the new name, BSE a web page
        self.srv.routes["/sec_30102026.csv"] = [(200, nse_files(date(2026, 10, 29))[0], {})]
        self.srv.routes["/bse_20261030.CSV"] = [(200, b"<!DOCTYPE html><html lang='en'><head>", {})]
        code, out = self.go("2026-10-30", "2026-10-30")
        line = next(l for l in out.splitlines() if l.startswith("2026-10-30"))
        self.assertEqual(line.split()[1:5], ["not", "published", "not", "published"])
        raw = os.path.join(self.out, "raw", "2026-10-30")
        self.assertEqual(os.listdir(raw) if os.path.isdir(raw) else [], [])   # nothing kept, nothing to poison later runs
        self.assertFalse(os.path.exists(os.path.join(self.out, "bhavcopy", "2026-10-30.csv")))

    def test_web_page_on_a_closed_day_shows_not_published(self):
        self.srv.routes["/sec_30102026.csv"] = [(200, b"<html>closed</html>", {})]
        self.srv.routes["/bse_20261030.CSV"] = [(200, b"<html>closed</html>", {})]
        code, out = self.go("2026-10-30", "2026-10-30")
        line = next(l for l in out.splitlines() if l.startswith("2026-10-30"))
        self.assertEqual(line.split()[1:5], ["not", "published", "not", "published"])

    def test_listed_holiday_is_skipped(self):
        self.serve_day(date(2026, 10, 29))
        hol = os.path.join(self.out, "h.txt")
        write_text(hol, "2026-10-29\n")
        code, out = self.go("2026-10-29", "2026-10-29", extra=("--holidays", hol))
        self.assertEqual(self.srv.hits, {})
        self.assertIn("holiday", out)

    def test_second_run_uses_files_on_disk(self):
        self.serve_day(date(2026, 10, 30))
        self.go("2026-10-30", "2026-10-30")
        hits = dict(self.srv.hits)
        code, out = self.go("2026-10-30", "2026-10-30")
        self.assertEqual(self.srv.hits, hits)
        self.assertIn("cached", out)
        self.go("2026-10-30", "2026-10-30", extra=("--refresh",))
        for path in ("/sec_30102026.csv", "/udiff_20261030.zip", "/bse_20261030.CSV"):   # each file downloaded again
            self.assertGreater(self.srv.hits[path], hits[path], path)

    def test_pause_before_every_download(self):
        for d in (date(2026, 10, 29), date(2026, 10, 30)):
            self.serve_day(d)
        self.go("2026-10-29", "2026-10-30", extra=("--pause", "3"))
        self.assertEqual(self.clock.sleeps.count(3.0), 4)   # NSE and BSE, on each of two days

    def test_403_stops_everything(self):
        self.srv.routes["/sec_28102026.csv"] = [(403, b"", {})]
        self.serve_day(date(2026, 10, 29))
        code, out = self.go("2026-10-28", "2026-10-29")
        self.assertEqual(code, 3)
        self.assertIn("Stopped", out)
        self.assertNotIn("/sec_29102026.csv", self.srv.hits)    # did not carry on to the next day
        self.assertEqual(self.srv.hits["/sec_28102026.csv"], 1)

    def test_no_data_at_all(self):
        code, out = self.go("2026-10-28", "2026-10-28")
        self.assertEqual(code, 1)
        self.assertIn("No day had comparable data", out)

    def test_bad_ranges(self):
        self.assertEqual(self.go("2026-10-30", "2026-10-28")[0], 1)
        self.assertEqual(self.go("2026-01-01", "2026-12-31")[0], 1)


class DayStats(unittest.TestCase):
    def test_numbers(self):
        res = [{"diff_pct": g, "bse_trades": t, "isin": str(i), "symbol": str(i)}
               for i, (g, t) in enumerate([(0.1, 500), (0.4, 500), (0.8, 500), (10.0, 5)])]
        st = bf.day_stats(res, 100, 0.5)
        self.assertEqual((st["matched"], st["active"], st["conflicts"]), (4, 3, 1))
        self.assertAlmostEqual(st["w_tol"], 200 / 3)
        self.assertIsNone(bf.day_stats(res, 10000, 0.5))


if __name__ == "__main__":
    unittest.main(verbosity=2)
