"""
Tests for pipeline/fetchers/nse_bhavcopy.py. No internet needed: network behaviour
(404 before publishing, 5xx, 403 block, Retry-After) is simulated by a local server.

Run from the workspace root:
    py -m unittest discover -s pipeline\\tests -v
"""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "fetchers"))
import nse_bhavcopy as nb  # noqa: E402

D = date(2026, 10, 29)  # a Thursday
HEADER = ("SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, "
          "CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER")


def row(sym, series="EQ", dt="29-Oct-2026", prev=3990.0, o=4000.0, h=4030.0, l=3985.0,
        last=4012.0, c=4012.5, avg=4010.0, vol=1200000, turn=48121.2, trades=90000,
        dq="780000", dp="65.00"):
    # real NSE files pad values with a leading space; the parser must cope
    return f"{sym}, {series}, {dt}, {prev}, {o}, {h}, {l}, {last}, {c}, {avg}, {vol}, {turn}, {trades}, {dq}, {dp}"


def delivery_csv(rows, header=HEADER):
    return "\n".join([header] + rows) + "\n"


def good_rows(n_filler=3, dt="29-Oct-2026"):
    rows = [row("TCS", dt=dt), row("INFY", dt=dt, prev=1500, o=1505, h=1520, l=1498, last=1512,
                                   c=1515.25, vol=2500000, dq="1000000", dp="40.00")]
    rows += [row(f"FILL{i}", dt=dt) for i in range(n_filler)]
    return rows


def udiff_zip(rows, dt="2026-10-29"):
    head = "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,ClsPric,TtlTradgVol"
    lines = [head] + [f"{dt},{dt},CM,NSE,STK,1,{isin},{sym},{ser},{c},{v}" for sym, ser, isin, c, v in rows]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("BhavCopy_NSE_CM_0_0_0_20261029_F_0000.csv", "\n".join(lines) + "\n")
    return buf.getvalue()


class Clock:
    def __init__(self, t):
        self.t, self.sleeps = t, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += timedelta(seconds=s)


def jload(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def ist(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=nb.IST)


class LocalServer:
    """Serves queued responses per path: [(status, body, headers), ...]; the last one repeats."""

    def __init__(self):
        self.routes, self.hits, self.headers_seen = {}, {}, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits[self.path] = outer.hits.get(self.path, 0) + 1
                outer.headers_seen.append(dict(self.headers))
                queue = outer.routes.get(self.path)
                if not queue:
                    status, body, headers = 404, b"", {}
                else:
                    i = min(outer.hits[self.path] - 1, len(queue) - 1)
                    status, body, headers = queue[i]
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.01), daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class NetBase(unittest.TestCase):
    def setUp(self):
        self.srv = LocalServer()
        self._urls, self._opener = dict(nb.URLS), nb._OPENER
        nb.URLS.update({"delivery": self.srv.base + "/sec_{ddmmyyyy}.csv",
                        "udiff": self.srv.base + "/udiff_{yyyymmdd}.zip"})
        nb._OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.tmp = tempfile.TemporaryDirectory()
        self.out = self.tmp.name
        self.clock = Clock(ist(2026, 10, 29, 19, 0))

    def tearDown(self):
        self.srv.stop()
        nb.URLS.clear()
        nb.URLS.update(self._urls)
        nb._OPENER = self._opener
        self.tmp.cleanup()

    def serve(self, delivery=None, udiff=None):
        if delivery is not None:
            self.srv.routes["/sec_29102026.csv"] = delivery if isinstance(delivery, list) else [(200, delivery.encode(), {})]
        if udiff is not None:
            self.srv.routes["/udiff_20261029.zip"] = udiff if isinstance(udiff, list) else [(200, udiff, {})]

    def run_day(self, **kw):
        kw.setdefault("min_rows", 3)
        return nb.run(D, out_dir=self.out, now=self.clock.now, sleep=self.clock.sleep, **kw)

    def norm_rows(self):
        import csv
        with open(os.path.join(self.out, "bhavcopy", "2026-10-29.csv"), encoding="utf-8") as f:
            return {(r["symbol"], r["series"]): r for r in csv.DictReader(f)}


# --------------------------------------------------------------------------- #
class Calendar(unittest.TestCase):
    def test_ist_offset_without_tzdata(self):
        self.assertEqual(nb.now_ist().utcoffset(), timedelta(hours=5, minutes=30))

    def test_default_date(self):
        h = set()
        self.assertEqual(nb.default_trading_date(ist(2026, 10, 30, 8, 37), h), date(2026, 10, 29))  # Fri morning
        self.assertEqual(nb.default_trading_date(ist(2026, 10, 30, 18, 29), h), date(2026, 10, 29))
        self.assertEqual(nb.default_trading_date(ist(2026, 10, 30, 18, 30), h), date(2026, 10, 30))  # Fri evening
        self.assertEqual(nb.default_trading_date(ist(2026, 10, 31, 12, 0), h), date(2026, 10, 30))   # Saturday
        self.assertEqual(nb.default_trading_date(ist(2026, 11, 2, 10, 0), h), date(2026, 10, 30))    # Monday morning

    def test_shipped_holiday_file_is_valid(self):
        h = nb.load_holidays(os.path.join(os.path.dirname(HERE), "config", "holidays.txt"))
        for d in (date(2026, 10, 20), date(2026, 11, 10), date(2026, 11, 24), date(2026, 12, 25)):
            self.assertIn(d, h)
        self.assertTrue(all(d.weekday() < 5 for d in h))       # weekends are skipped anyway
        self.assertEqual(nb.default_trading_date(ist(2026, 10, 21, 10, 0), h), date(2026, 10, 19))  # skips Dussehra

    def test_holiday_skipped(self):
        self.assertEqual(nb.default_trading_date(ist(2026, 11, 2, 10, 0), {date(2026, 10, 30)}),
                         date(2026, 10, 29))

    def test_holiday_file_parsing(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "h.txt")
            write_text(p, "# comment\n2026-11-08  # Diwali\n\n")
            self.assertEqual(nb.load_holidays(p), {date(2026, 11, 8)})
        self.assertEqual(nb.load_holidays("/nonexistent"), set())


class Parsing(unittest.TestCase):
    def test_parses_padded_values_and_dashes(self):
        rows = nb.parse_delivery_csv(delivery_csv([row("TCS"), row("X", dq="-", dp="-")]))
        self.assertEqual(rows[0]["symbol"], "TCS")
        self.assertEqual(rows[0]["date"], "2026-10-29")
        self.assertEqual(rows[0]["close"], 4012.5)
        self.assertEqual(rows[0]["deliv_pct"], 65.0)
        self.assertIsNone(rows[1]["deliv_qty"])
        self.assertIsNone(rows[1]["deliv_pct"])

    def test_missing_column_lists_what_was_found(self):
        bad = HEADER.replace("CLOSE_PRICE", "CLS")
        with self.assertRaises(nb.SchemaError) as cm:
            nb.parse_delivery_csv(delivery_csv([row("TCS")], header=bad))
        self.assertIn("CLOSE_PRICE", str(cm.exception))
        self.assertIn("Columns found", str(cm.exception))

    def test_empty_file(self):
        with self.assertRaises(nb.SchemaError):
            nb.parse_delivery_csv("")

    def test_udiff(self):
        rows = nb.parse_udiff_zip(udiff_zip([("TCS", "EQ", "INE467B01029", 4012.5, 1200000)]))
        self.assertEqual(rows[0]["isin"], "INE467B01029")
        self.assertEqual(rows[0]["date"], "2026-10-29")

    def test_udiff_bad_zip(self):
        with self.assertRaises(nb.DataError):
            nb.parse_udiff_zip(b"not a zip")


class FileValidation(unittest.TestCase):
    def rows(self, **kw):
        return nb.parse_delivery_csv(delivery_csv(good_rows(**kw)))

    def test_wrong_date_rejected(self):
        rows = nb.parse_delivery_csv(delivery_csv(good_rows(dt="28-Oct-2026")))
        with self.assertRaises(nb.WrongDate):
            nb.validate_file(rows, "2026-10-29", 3)

    def test_mixed_dates_rejected(self):
        rows = nb.parse_delivery_csv(delivery_csv(good_rows() + [row("OLD", dt="28-Oct-2026")]))
        with self.assertRaises(nb.WrongDate):
            nb.validate_file(rows, "2026-10-29", 3)

    def test_truncation_vs_minimum_and_prior(self):
        rows = self.rows()  # 5 rows
        with self.assertRaises(nb.Truncated):
            nb.validate_file(rows, "2026-10-29", 100)
        with self.assertRaises(nb.Truncated):
            nb.validate_file(rows, "2026-10-29", 3, prior_count=10)  # < 80% of yesterday
        nb.validate_file(rows, "2026-10-29", 3, prior_count=6)       # fine

    def test_empty(self):
        with self.assertRaises(nb.Truncated):
            nb.validate_file([], "2026-10-29", 1)


class RowChecks(unittest.TestCase):
    def flags(self, **kw):
        r = nb.parse_delivery_csv(delivery_csv([row("T", **kw)]))[0]
        return nb.check_row(r)

    def test_clean_row(self):
        self.assertEqual(self.flags(), [])

    def test_high_below_low(self):
        self.assertIn("bad_ohlc", self.flags(h=3900.0, l=3985.0))

    def test_close_outside_range(self):
        self.assertIn("close_outside_range", self.flags(c=4100.0))

    def test_open_outside_range(self):
        self.assertIn("open_outside_range", self.flags(o=3000.0))

    def test_no_trade_day_is_not_an_error(self):
        f = self.flags(o=0, h=0, l=0, last=3990, c=3990, vol=0, dq="0", dp="0")
        self.assertEqual(f, ["no_trades"])
        self.assertFalse(set(f) & nb.ERROR_FLAGS)

    def test_delivery_exceeds_volume(self):
        self.assertIn("deliv_gt_volume", self.flags(vol=1000, dq="5000"))

    def test_delivery_pct_out_of_range(self):
        self.assertIn("bad_deliv_pct", self.flags(dp="101"))

    def test_missing_close(self):
        self.assertIn("missing_price", self.flags(c="-"))


class MergeAndSeries(unittest.TestCase):
    def setUp(self):
        self.d = nb.parse_delivery_csv(delivery_csv([row("TCS"), row("SME1", series="BE")]))

    def test_series_filter(self):
        self.assertEqual([r["symbol"] for r in nb.merge_and_check(self.d, None, {"EQ"})], ["TCS"])
        self.assertEqual(len(nb.merge_and_check(self.d, None, {"EQ", "BE"})), 2)

    def test_isin_added(self):
        u = nb.parse_udiff_zip(udiff_zip([("TCS", "EQ", "INE467B01029", 4012.5, 1200000)]))
        r = nb.merge_and_check(self.d, u, {"EQ"})[0]
        self.assertEqual(r["isin"], "INE467B01029")
        self.assertEqual(r["flags"], [])

    def test_cross_file_disagreement_flagged_and_unusable(self):
        u = nb.parse_udiff_zip(udiff_zip([("TCS", "EQ", "INE467B01029", 4100.0, 1200000)]))
        r = nb.merge_and_check(self.d, u, {"EQ"})[0]
        self.assertIn("udiff_close_mismatch", r["flags"])
        self.assertIsNone(nb.to_evidence(r, "2026-10-29"))

    def test_volume_disagreement_flagged(self):
        u = nb.parse_udiff_zip(udiff_zip([("TCS", "EQ", "INE467B01029", 4012.5, 999)]))
        self.assertIn("udiff_volume_mismatch", nb.merge_and_check(self.d, u, {"EQ"})[0]["flags"])


class CorporateActions(unittest.TestCase):
    def test_prev_close_mismatch_flagged(self):
        rows = nb.merge_and_check(nb.parse_delivery_csv(delivery_csv([row("TCS", prev=2000.0)])), None, {"EQ"})
        n = nb.flag_corporate_actions(rows, {("TCS", "EQ"): 4000.0})
        self.assertEqual(n, 1)
        self.assertIn("possible_corporate_action", rows[0]["flags"])

    def test_matching_prev_close_not_flagged(self):
        rows = nb.merge_and_check(nb.parse_delivery_csv(delivery_csv([row("TCS", prev=3990.0)])), None, {"EQ"})
        self.assertEqual(nb.flag_corporate_actions(rows, {("TCS", "EQ"): 3990.0}), 0)

    def test_unknown_prior_symbol_skipped(self):
        rows = nb.merge_and_check(nb.parse_delivery_csv(delivery_csv([row("NEWLIST")])), None, {"EQ"})
        self.assertEqual(nb.flag_corporate_actions(rows, {}), 0)


class Evidence(unittest.TestCase):
    def test_shape_matches_gate_input(self):
        r = nb.merge_and_check(nb.parse_delivery_csv(delivery_csv([row("TCS")])), None, {"EQ"})[0]
        e = nb.to_evidence(r, "2026-10-29")
        self.assertEqual(e["evidence"]["cmp"], [{"source": "nse_bhavcopy", "value": 4012.5, "as_of": "2026-10-29"}])
        self.assertEqual(e["evidence"]["volume"][0]["value"], 1200000.0)
        self.assertEqual(e["extras"]["delivery_pct"], 65.0)
        self.assertFalse(e["extras"]["needs_adjustment_check"])

    def test_unusable_row_gives_no_evidence(self):
        r = nb.merge_and_check(nb.parse_delivery_csv(delivery_csv([row("T", c=9999.0)])), None, {"EQ"})[0]
        self.assertIsNone(nb.to_evidence(r, "2026-10-29"))


class Http(NetBase):
    def url(self, path="/x"):
        return self.srv.base + path

    def test_404_is_not_published_and_not_retried(self):
        with self.assertRaises(nb.NotPublished):
            nb.fetch_bytes(self.url(), sleep=self.clock.sleep)
        self.assertEqual(self.srv.hits["/x"], 1)

    def test_retries_5xx_then_succeeds(self):
        self.srv.routes["/x"] = [(500, b"", {}), (502, b"", {}), (200, b"ok", {})]
        self.assertEqual(nb.fetch_bytes(self.url(), sleep=self.clock.sleep), b"ok")
        self.assertEqual(len(self.clock.sleeps), 2)
        self.assertTrue(2 <= self.clock.sleeps[0] < 3.1)   # about 2s plus jitter
        self.assertTrue(4 <= self.clock.sleeps[1] < 5.1)   # about 4s plus jitter

    def test_gives_up_after_three_attempts(self):
        self.srv.routes["/x"] = [(500, b"", {})]
        with self.assertRaises(nb.FetchFailed):
            nb.fetch_bytes(self.url(), sleep=self.clock.sleep)
        self.assertEqual(self.srv.hits["/x"], 3)

    def test_403_stops_immediately(self):
        self.srv.routes["/x"] = [(403, b"", {})]
        with self.assertRaises(nb.Blocked):
            nb.fetch_bytes(self.url(), sleep=self.clock.sleep)
        self.assertEqual(self.srv.hits["/x"], 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_retry_after_is_honoured(self):
        self.srv.routes["/x"] = [(503, b"", {"Retry-After": "7"}), (200, b"ok", {})]
        nb.fetch_bytes(self.url(), sleep=self.clock.sleep)
        self.assertEqual(self.clock.sleeps, [7.0])

    def test_web_page_answered_with_200_is_not_a_data_file(self):
        for page in (b"<!DOCTYPE html><html><body>Not found</body></html>", b"\xef\xbb\xbf  <HTML><body>x</body></HTML>"):
            self.srv.routes["/y"] = [(200, page, {})]
            self.srv.hits.pop("/y", None)
            with self.assertRaises(nb.NotPublished):
                nb.fetch_bytes(self.srv.base + "/y", sleep=self.clock.sleep)
            self.assertEqual(self.srv.hits["/y"], 1)   # an answer, not an outage: not retried

    def test_csv_that_merely_contains_angle_brackets_is_data(self):
        self.srv.routes["/z"] = [(200, b"A,B\n<1>,2\n", {})]
        self.assertEqual(nb.fetch_bytes(self.srv.base + "/z", sleep=self.clock.sleep), b"A,B\n<1>,2\n")

    def test_connection_refused_is_fetch_failed(self):
        self.srv.stop()
        with self.assertRaises(nb.FetchFailed):
            nb.fetch_bytes(self.url(), sleep=self.clock.sleep)
        self.srv = LocalServer()  # so tearDown can stop something


class Polling(NetBase):
    def test_polls_until_published(self):
        self.serve(delivery=[(404, b"", {}), (404, b"", {}), (200, delivery_csv(good_rows()).encode(), {})])
        code, s = self.run_day(poll=True, no_udiff=True)
        self.assertEqual((code, s["status"]), (0, "OK"))
        self.assertEqual(self.srv.hits["/sec_29102026.csv"], 3)
        self.assertEqual(self.clock.sleeps, [600, 600])

    def test_gives_up_at_deadline(self):
        self.clock.t = ist(2026, 10, 29, 20, 40)
        self.serve(delivery=[(404, b"", {})])
        code, s = self.run_day(poll=True, until="21:00", no_udiff=True)
        self.assertEqual((code, s["status"]), (2, "MISSING"))
        self.assertEqual(self.srv.hits["/sec_29102026.csv"], 3)  # 20:40, 20:50, 21:00
        h = jload(s["health_file"])
        self.assertEqual(h["sources"]["bhavcopy"], "FAILED")
        self.assertTrue(any("not published" in f for f in h["validation_failures"]))

    def test_without_poll_single_attempt(self):
        self.serve(delivery=[(404, b"", {})])
        code, s = self.run_day(no_udiff=True)
        self.assertEqual(code, 2)
        self.assertEqual(self.srv.hits["/sec_29102026.csv"], 1)


class EndToEnd(NetBase):
    def full_serve(self):
        self.serve(delivery=delivery_csv(good_rows()),
                   udiff=udiff_zip([("TCS", "EQ", "INE467B01029", 4012.5, 1200000),
                                    ("INFY", "EQ", "INE009A01021", 1515.25, 2500000)]))

    def test_happy_path_outputs(self):
        self.full_serve()
        ev = os.path.join(self.out, "ev.json")
        code, s = self.run_day(emit_evidence=ev)
        self.assertEqual((code, s["status"]), (0, "OK"))
        self.assertEqual((s["rows_total"], s["rows_kept"], s["rows_usable"]), (5, 5, 5))
        self.assertTrue(s["isin_available"])
        rows = self.norm_rows()
        self.assertEqual(rows[("TCS", "EQ")]["isin"], "INE467B01029")
        self.assertEqual(rows[("TCS", "EQ")]["usable"], "True")
        self.assertEqual(rows[("FILL0", "EQ")]["isin"], "")  # no UDiFF row: stays blank, never invented
        stocks = {x["symbol"]: x for x in jload(ev)["stocks"]}
        self.assertEqual(stocks["INFY"]["evidence"]["cmp"][0]["value"], 1515.25)
        man = jload(os.path.join(self.out, "bhavcopy", "2026-10-29.manifest.json"))
        self.assertEqual(len(man["files"]["delivery"]["sha256"]), 64)
        self.assertEqual(jload(s["health_file"])["sources"]["bhavcopy"], "OK")

    def test_second_run_uses_cache(self):
        self.full_serve()
        self.run_day()
        before = dict(self.srv.hits)
        code, s = self.run_day()
        self.assertEqual(code, 0)
        self.assertEqual(self.srv.hits, before)
        self.run_day(refresh=True)
        self.assertGreater(self.srv.hits["/sec_29102026.csv"], before["/sec_29102026.csv"])

    def test_udiff_unavailable_is_not_fatal(self):
        self.serve(delivery=delivery_csv(good_rows()))  # udiff route absent -> 404
        code, s = self.run_day()
        self.assertEqual((code, s["status"]), (0, "OK"))
        self.assertFalse(s["isin_available"])
        self.assertTrue(any("UDiFF" in m for m in s["messages"]))

    def test_older_days_file_is_missing_not_failed_and_never_used(self):
        # what NSE really does on a holiday: a complete file for the PREVIOUS day under today's name
        self.serve(delivery=delivery_csv(good_rows(dt="28-Oct-2026")))
        code, s = self.run_day(no_udiff=True)
        self.assertEqual((code, s["status"]), (2, "MISSING"))
        self.assertIn("2026-10-28", " ".join(s["messages"]))
        self.assertFalse(os.path.exists(os.path.join(self.out, "bhavcopy", "2026-10-29.csv")))
        self.assertFalse(os.path.exists(os.path.join(self.out, "raw", "2026-10-29", "sec_bhavdata_full_29102026.csv")))

    def test_file_dated_later_than_expected_is_rejected(self):
        self.serve(delivery=delivery_csv(good_rows(dt="30-Oct-2026")))
        code, s = self.run_day(no_udiff=True)
        self.assertEqual((code, s["status"]), (3, "FAILED"))
        self.assertTrue(os.path.exists(os.path.join(self.out, "raw", "2026-10-29", "REJECTED_nse.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.out, "bhavcopy", "2026-10-29.csv")))

    def test_file_mixing_old_and_new_dates_is_rejected(self):
        self.serve(delivery=delivery_csv(good_rows(dt="29-Oct-2026") + [row("OLD", dt="28-Oct-2026")]))
        self.assertEqual(self.run_day(no_udiff=True)[0], 3)

    def test_polling_waits_through_an_older_days_file(self):
        stale = delivery_csv(good_rows(dt="28-Oct-2026")).encode()
        self.serve(delivery=[(200, stale, {}), (200, stale, {}), (200, delivery_csv(good_rows()).encode(), {})])
        code, s = self.run_day(poll=True, no_udiff=True)
        self.assertEqual((code, s["status"]), (0, "OK"))
        self.assertEqual(self.clock.sleeps, [600, 600])
        self.assertEqual(self.norm_rows()[("TCS", "EQ")]["date"], "2026-10-29")   # the fresh file, not the stale one

    def test_stale_file_left_in_cache_by_an_older_version_is_replaced(self):
        raw = os.path.join(self.out, "raw", "2026-10-29")
        os.makedirs(raw)
        write_text(os.path.join(raw, "sec_bhavdata_full_29102026.csv"), delivery_csv(good_rows(dt="28-Oct-2026")))
        self.serve(delivery=delivery_csv(good_rows()))
        self.assertEqual(self.run_day(no_udiff=True)[0], 3)             # cached stale file rejected and moved aside
        self.assertTrue(os.path.exists(os.path.join(raw, "sec_bhavdata_full_29102026.csv.rejected")))
        code, s = self.run_day(no_udiff=True)                           # next run fetches the real file
        self.assertEqual((code, s["status"]), (0, "OK"))

    def test_schema_change_fails_with_columns(self):
        self.serve(delivery=delivery_csv(good_rows(), header=HEADER.replace("TTL_TRD_QNTY", "VOLUME")))
        code, s = self.run_day(no_udiff=True)
        self.assertEqual(code, 3)
        self.assertIn("Columns found", " ".join(s["messages"]))

    def test_403_reports_blocked_and_manual_fallback(self):
        self.serve(delivery=[(403, b"", {})])
        code, s = self.run_day(no_udiff=True)
        self.assertEqual((code, s["status"]), (3, "FAILED"))
        self.assertIn("--delivery-file", " ".join(s["messages"]))
        self.assertEqual(self.srv.hits["/sec_29102026.csv"], 1)

    def test_cross_file_mismatch_excludes_row_and_reports(self):
        self.serve(delivery=delivery_csv(good_rows()),
                   udiff=udiff_zip([("TCS", "EQ", "INE467B01029", 9999.0, 1200000)]))
        ev = os.path.join(self.out, "ev.json")
        code, s = self.run_day(emit_evidence=ev)
        self.assertEqual(code, 0)
        self.assertEqual(s["rows_unusable"], 1)
        self.assertNotIn("TCS", [x["symbol"] for x in jload(ev)["stocks"]])
        h = jload(s["health_file"])
        self.assertTrue(any("disagree" in f for f in h["validation_failures"]))

    def test_manual_file_needs_no_network(self):
        p = os.path.join(self.out, "manual.csv")
        write_text(p, delivery_csv(good_rows()))
        self.srv.stop()
        code, s = self.run_day(delivery_file=p, no_udiff=True)
        self.assertEqual((code, s["status"]), (0, "OK"))
        self.srv = LocalServer()

    def test_corporate_action_across_days(self):
        d1 = delivery_csv(good_rows(dt="28-Oct-2026"))
        p1 = os.path.join(self.out, "d1.csv")
        write_text(p1, d1)
        c, s = nb.run(date(2026, 10, 28), out_dir=self.out, min_rows=3, no_udiff=True, delivery_file=p1,
                      now=self.clock.now, sleep=self.clock.sleep)
        self.assertEqual(c, 0)
        # day 2: TCS closes 4012.50 yesterday, but today's PREV_CLOSE is 2006.25 -> 1:2 split
        rows2 = [row("TCS", prev=2006.25, o=2005, h=2030, l=1995, last=2010, c=2012.0),
                 row("INFY", prev=1515.25, o=1505, h=1520, l=1498, last=1512, c=1518.0,
                     vol=2500000, dq="1000000", dp="40.00")]
        rows2 += [row(f"FILL{i}", prev=4012.5) for i in range(3)]  # unchanged: prev == yesterday's close
        p2 = os.path.join(self.out, "d2.csv")
        write_text(p2, delivery_csv(rows2))
        ev = os.path.join(self.out, "ev.json")
        c, s = self.run_day(no_udiff=True, delivery_file=p2, emit_evidence=ev)
        self.assertEqual(c, 0)
        self.assertEqual(s["possible_corporate_actions"], 1)
        tcs = {x["symbol"]: x for x in jload(ev)["stocks"]}["TCS"]
        self.assertTrue(tcs["extras"]["needs_adjustment_check"])

    def test_truncation_against_prior_day(self):
        p1 = os.path.join(self.out, "d1.csv")
        write_text(p1, delivery_csv(good_rows(n_filler=18, dt="28-Oct-2026")))  # 20 rows
        nb.run(date(2026, 10, 28), out_dir=self.out, min_rows=3, no_udiff=True, delivery_file=p1,
               now=self.clock.now, sleep=self.clock.sleep)
        p2 = os.path.join(self.out, "d2.csv")
        write_text(p2, delivery_csv(good_rows(n_filler=3)))  # 5 rows < 16
        code, s = self.run_day(no_udiff=True, delivery_file=p2)
        self.assertEqual((code, s["status"]), (3, "FAILED"))
        self.assertIn("partial download", " ".join(s["messages"]))

    def test_holiday_style_web_page_is_missing_not_failed_and_not_cached(self):
        self.serve(delivery=[(200, b"<html><body>No such file</body></html>", {})])
        code, s = self.run_day(no_udiff=True)
        self.assertEqual((code, s["status"]), (2, "MISSING"))
        self.assertFalse(os.path.exists(os.path.join(self.out, "raw", "2026-10-29", "sec_bhavdata_full_29102026.csv")))

    def test_polling_continues_past_a_web_page(self):
        self.serve(delivery=[(200, b"<html>wait</html>", {}), (200, delivery_csv(good_rows()).encode(), {})])
        code, s = self.run_day(poll=True, no_udiff=True)
        self.assertEqual((code, s["status"]), (0, "OK"))

    def test_rejected_download_is_quarantined_so_the_next_run_refetches(self):
        bad = delivery_csv(good_rows(), header=HEADER.replace("TTL_TRD_QNTY", "VOLUME"))
        self.serve(delivery=[(200, bad.encode(), {}), (200, delivery_csv(good_rows()).encode(), {})])
        self.assertEqual(self.run_day(no_udiff=True)[0], 3)
        raw = os.path.join(self.out, "raw", "2026-10-29")
        self.assertFalse(os.path.exists(os.path.join(raw, "sec_bhavdata_full_29102026.csv")))
        self.assertTrue(os.path.exists(os.path.join(raw, "sec_bhavdata_full_29102026.csv.rejected")))
        code, s = self.run_day(no_udiff=True)          # must not reuse the bad file
        self.assertEqual((code, s["status"]), (0, "OK"))
        self.assertEqual(self.srv.hits["/sec_29102026.csv"], 2)

    def test_rejected_udiff_is_quarantined_too(self):
        self.serve(delivery=delivery_csv(good_rows()), udiff=b"PK-not-really-a-zip")
        code, s = self.run_day()
        self.assertEqual(code, 0)                       # UDiFF is optional
        raw = os.path.join(self.out, "raw", "2026-10-29")
        self.assertFalse(os.path.exists(os.path.join(raw, "BhavCopy_NSE_CM_20261029.csv.zip")))
        self.assertTrue(os.path.exists(os.path.join(raw, "BhavCopy_NSE_CM_20261029.csv.zip.rejected")))

    def test_users_own_file_is_never_moved_when_rejected(self):
        p = os.path.join(self.out, "mine.csv")
        write_text(p, delivery_csv(good_rows(dt="28-Oct-2026")))     # wrong date
        code, s = self.run_day(no_udiff=True, delivery_file=p)
        self.assertEqual(code, 3)
        self.assertTrue(os.path.exists(p))
        self.assertFalse(os.path.exists(p + ".rejected"))

    def test_weekend_runs_nothing(self):
        code, s = nb.run(date(2026, 10, 31), out_dir=self.out, now=self.clock.now, sleep=self.clock.sleep)
        self.assertEqual((code, s["status"]), (0, "NON_TRADING_DAY"))
        self.assertEqual(self.srv.hits, {})
        self.assertEqual(os.listdir(self.out), [])

    def test_listed_holiday_runs_nothing(self):
        code, s = self.run_day(holidays={D})
        self.assertEqual(s["status"], "NON_TRADING_DAY")
        self.assertEqual(self.srv.hits, {})

    def test_cli_main(self):
        self.full_serve()
        code = nb.main(["--date", "2026-10-29", "--out-dir", self.out, "--min-rows", "3",
                        "--holidays", os.path.join(self.out, "none.txt")])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(self.out, "bhavcopy", "2026-10-29.csv")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
