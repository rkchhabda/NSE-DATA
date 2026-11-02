"""
Tests for pipeline/fetchers/bse_bhavcopy.py and pipeline/tools/compare_exchanges.py.
The BSE header and five data rows below are REAL lines from BSE's 30-Oct-2026 UDiFF file;
the NSE rows are real lines from the normalised NSE output of the same day. No internet needed.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.request
import zipfile
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "fetchers"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import nse_bhavcopy as nb  # noqa: E402
import bse_bhavcopy as bb  # noqa: E402
import compare_exchanges as ce  # noqa: E402
from test_nse_bhavcopy import Clock, LocalServer, ist, jload, write_text  # noqa: E402

D = date(2026, 10, 30)
HEADER = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,"
          "StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,"
          "SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,"
          "Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4")
REAL = [
    "2026-10-30,2026-10-30,CM,BSE,STK,500002,INE117A01022,ABB,A,,,,,ABB INDIA LIMITED,6486.80,6486.80,6386.00,6403.00,6403.00,6530.00,,6412.94,,,9368,60218127.00,2047,F1,1,,,,,",
    "2026-10-30,2026-10-30,CM,BSE,STK,500003,INE208C01025,AEGISLOG,A,,,,,AEGIS LOGISTICS LTD.,1178.75,1178.75,1082.00,1097.45,1097.45,1180.85,,1096.73,,,65121,73602867.00,3070,F1,1,,,,,",
    "2026-10-30,2026-10-30,CM,BSE,STK,500209,INE009A01021,INFY,A,,,,,INFOSYS LTD.,1148.00,1183.95,1148.00,1182.00,1182.00,1143.45,,1184.99,,,459395,538606644.00,11199,F1,1,,,,,",
    "2026-10-30,2026-10-30,CM,BSE,STK,500325,INE002A01018,RELIANCE,A,,,,,RELIANCE INDUSTRIES LTD.,1171.00,1172.40,1152.50,1163.55,1163.55,1168.75,,1164.88,,,1280860,1484882042.00,36671,F1,1,,,,,",
    "2026-10-30,2026-10-30,CM,BSE,STK,500390,INE036A01016,RELINFRA,T,,,,,RELIANCE INFRASTRUCTURE LTD.,48.45,48.45,46.55,46.58,46.93,47.50,,46.57,,,1058595,51257824.00,287,F1,1,,,,,",
]
NSE_HEAD = "symbol,series,date,isin,prev_close,open,high,low,last,close,avg_price,volume,turnover_lacs,trades,deliv_qty,deliv_pct,usable,flags"
NSE_ROWS = [
    "INFY,EQ,2026-10-30,INE009A01021,1146.5,1149.1,1185.0,1147.9,1185.0,1185.0,1174.76,13030713.0,153080.21,184733.0,7575174.0,58.13,True,",
    "RELIANCE,EQ,2026-10-30,INE002A01018,1167.4,1170.4,1174.6,1152.5,1164.9,1164.9,1160.33,14753650.0,171191.72,207450.0,8073955.0,54.73,True,",
    "TCS,EQ,2026-10-30,INE467B01029,2478.0,2495.2,2527.7,2494.2,2502.6,2502.6,2513.04,3016983.0,75818.09,119512.0,1420735.0,47.09,True,",
]


def bse_csv(rows=REAL):
    return "\n".join([HEADER] + list(rows)) + "\n"


def bse_row(sym, isin, close=100.0, prev=99.0, vol=1000, o=99.5, h=101.0, l=98.5, last=None, trades=50, series="A",
            date_s="2026-10-30"):
    last = close if last is None else last
    return (f"{date_s},{date_s},CM,BSE,STK,5{abs(hash(sym)) % 10**5:05d},{isin},{sym},{series},,,,,{sym} LTD,"
            f"{o},{h},{l},{close},{last},{prev},,{close},,,{vol},{vol * close:.2f},{trades},F1,1,,,,,")


class Parse(unittest.TestCase):
    def test_real_rows(self):
        rows = {r["symbol"]: r for r in bb.parse_bse_csv(bse_csv())}
        i = rows["INFY"]
        self.assertEqual((i["isin"], i["series"], i["scrip_code"], i["date"]), ("INE009A01021", "A", "500209", "2026-10-30"))
        self.assertEqual((i["close"], i["prev_close"], i["volume"], i["trades"]), (1182.0, 1143.45, 459395.0, 11199.0))
        self.assertEqual(rows["RELINFRA"]["series"], "T")
        self.assertNotEqual(rows["RELINFRA"]["last"], rows["RELINFRA"]["close"])
        self.assertEqual(len(rows), 5)

    def test_missing_column_lists_found(self):
        with self.assertRaises(nb.SchemaError) as cm:
            bb.parse_bse_csv(bse_csv().replace("ClsPric", "Close"))
        self.assertIn("CLSPRIC", str(cm.exception))
        self.assertIn("Columns found", str(cm.exception))

    def test_bad_date(self):
        with self.assertRaises(nb.DataError):
            bb.parse_bse_csv(bse_csv([REAL[0].replace("2026-10-30", "garbage", 1)]))


class Checks(unittest.TestCase):
    def rows(self, lines):
        return {r["symbol"]: r for r in bb.check_rows(bb.parse_bse_csv(bse_csv(lines)))}

    def test_real_rows_are_clean(self):
        for r in self.rows(REAL).values():
            self.assertEqual(r["flags"], [], r["symbol"])

    def test_no_trade_row_is_not_an_error_but_gives_no_evidence(self):
        r = self.rows([bse_row("ZERO", "INE000Z01010", vol=0, o=0, h=0, l=0, trades=0)])["ZERO"]
        self.assertEqual(r["flags"], ["no_trades"])
        self.assertIsNone(bb.to_price_evidence(r))

    def test_duplicate_isin_flagged_and_unusable(self):
        rows = self.rows([bse_row("AAA", "INE000D01010"), bse_row("BBB", "INE000D01010")])
        for r in rows.values():
            self.assertIn("duplicate_isin", r["flags"])
            self.assertIsNone(bb.to_price_evidence(r))

    def test_missing_isin(self):
        r = self.rows([bse_row("NOISIN", "")])["NOISIN"]
        self.assertIn("no_isin", r["flags"])

    def test_high_below_low(self):
        r = self.rows([bse_row("BAD", "INE000B01010", h=90.0, l=98.5)])["BAD"]
        self.assertIn("bad_ohlc", r["flags"])
        self.assertIsNone(bb.to_price_evidence(r))

    def test_evidence_record(self):
        r = self.rows(REAL)["INFY"]
        self.assertEqual(bb.to_price_evidence(r), {"source": "bse_bhavcopy", "value": 1182.0, "as_of": "2026-10-30"})


class RunBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = self.tmp.name
        self.clock = Clock(ist(2026, 10, 30, 19, 0))
        self.file = os.path.join(self.out, "bse_in.csv")
        write_text(self.file, bse_csv())

    def tearDown(self):
        self.tmp.cleanup()

    def go(self, **kw):
        kw.setdefault("min_rows", 3)
        return bb.run(D, out_dir=self.out, now=self.clock.now, sleep=self.clock.sleep, **kw)


class RunFile(RunBase):
    def test_manual_file(self):
        code, s = self.go(file=self.file)
        self.assertEqual((code, s["status"], s["rows_total"], s["rows_unusable"]), (0, "OK", 5, 0))
        self.assertTrue(os.path.exists(os.path.join(self.out, "bse", "2026-10-30.csv")))
        self.assertEqual(jload(s["health_file"])["sources"]["bse_bhavcopy"], "OK")
        man = jload(os.path.join(self.out, "bse", "2026-10-30.manifest.json"))
        self.assertEqual(len(man["files"]["bse"]["sha256"]), 64)

    def test_zip_input(self):
        zp = os.path.join(self.out, "in.zip")
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("BhavCopy_BSE_CM_0_0_0_20261030_F_0000.CSV", bse_csv())
        code, s = self.go(file=zp)
        self.assertEqual((code, s["rows_total"]), (0, 5))

    def test_wrong_date_rejected(self):
        write_text(self.file, bse_csv([r.replace("2026-10-30", "2026-10-29") for r in REAL]))
        code, s = self.go(file=self.file)
        self.assertEqual((code, s["status"]), (3, "FAILED"))
        self.assertFalse(os.path.exists(os.path.join(self.out, "bse", "2026-10-30.csv")))

    def test_truncation_vs_prior_day(self):
        many = [bse_row(f"S{i}", f"INE{i:06d}010") for i in range(20)]
        write_text(self.file, bse_csv(many))
        write_text(self.file, bse_csv([r.replace("2026-10-30", "2026-10-29") for r in many]))
        self.assertEqual(bb.run(date(2026, 10, 29), out_dir=self.out, file=self.file, min_rows=3,
                                now=self.clock.now, sleep=self.clock.sleep)[0], 0)
        write_text(self.file, bse_csv(REAL))  # 5 rows < 80% of 20
        code, s = self.go(file=self.file)
        self.assertEqual(code, 3)
        self.assertIn("partial download", " ".join(s["messages"]))

    def test_weekend_runs_nothing(self):
        code, s = bb.run(date(2026, 10, 31), out_dir=self.out, file=self.file, now=self.clock.now, sleep=self.clock.sleep)
        self.assertEqual((code, s["status"]), (0, "NON_TRADING_DAY"))
        self.assertFalse(os.path.exists(os.path.join(self.out, "bse")))

    def test_missing_file_path(self):
        code, s = self.go(file=os.path.join(self.out, "nope.csv"))
        self.assertEqual((code, s["status"]), (3, "FAILED"))

    def test_cli(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = bb.main(["--date", "2026-10-30", "--out-dir", self.out, "--file", self.file, "--min-rows", "3",
                            "--holidays", os.path.join(self.out, "none.txt")])
        self.assertEqual(code, 0)
        self.assertIn("BSE Bhavcopy 2026-10-30: OK", buf.getvalue())


class RunNetwork(RunBase):
    def setUp(self):
        super().setUp()
        self.srv = LocalServer()
        self._opener = nb._OPENER
        nb._OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.url = self.srv.base + "/BhavCopy_BSE_CM_0_0_0_{yyyymmdd}_F_0000.CSV"
        self.path = "/BhavCopy_BSE_CM_0_0_0_20261030_F_0000.CSV"

    def tearDown(self):
        self.srv.stop()
        nb._OPENER = self._opener
        super().tearDown()

    def test_download_then_cache(self):
        self.srv.routes[self.path] = [(200, bse_csv().encode(), {})]
        code, s = self.go(url=self.url)
        self.assertEqual((code, s["status"]), (0, "OK"))
        hits = self.srv.hits[self.path]
        self.go(url=self.url)
        self.assertEqual(self.srv.hits[self.path], hits)  # cached

    def test_polls_until_published(self):
        self.srv.routes[self.path] = [(404, b"", {}), (200, bse_csv().encode(), {})]
        code, s = self.go(url=self.url, poll=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.clock.sleeps, [600])

    def test_not_published_by_deadline(self):
        self.clock.t = ist(2026, 10, 30, 20, 55)
        code, s = self.go(url=self.url, poll=True, until="21:00")
        self.assertEqual((code, s["status"]), (2, "MISSING"))
        self.assertEqual(jload(s["health_file"])["sources"]["bse_bhavcopy"], "FAILED")

    def test_web_page_is_missing_and_not_cached(self):
        self.srv.routes[self.path] = [(200, b"<html>no such file</html>", {})]
        code, s = self.go(url=self.url)
        self.assertEqual((code, s["status"]), (2, "MISSING"))
        self.assertFalse(os.path.exists(os.path.join(self.out, "raw", "2026-10-30", self.path.lstrip("/"))))

    def test_older_days_file_is_missing_and_never_used(self):
        old_day = bse_csv([r.replace("2026-10-30", "2026-10-29") for r in REAL])
        self.srv.routes[self.path] = [(200, old_day.encode(), {})]
        code, s = self.go(url=self.url)
        self.assertEqual((code, s["status"]), (2, "MISSING"))
        self.assertIn("2026-10-29", " ".join(s["messages"]))
        self.assertFalse(os.path.exists(os.path.join(self.out, "bse", "2026-10-30.csv")))

    def test_polling_waits_through_an_older_days_file(self):
        old_day = bse_csv([r.replace("2026-10-30", "2026-10-29") for r in REAL]).encode()
        self.srv.routes[self.path] = [(200, old_day, {}), (200, bse_csv().encode(), {})]
        code, s = self.go(url=self.url, poll=True)
        self.assertEqual((code, s["status"]), (0, "OK"))
        self.assertEqual(self.clock.sleeps, [600])

    def test_rejected_download_is_quarantined_with_its_own_reason_file(self):
        bad = bse_csv().replace("ClsPric", "Close")
        self.srv.routes[self.path] = [(200, bad.encode(), {}), (200, bse_csv().encode(), {})]
        self.assertEqual(self.go(url=self.url)[0], 3)
        raw = os.path.join(self.out, "raw", "2026-10-30")
        self.assertTrue(os.path.exists(os.path.join(raw, self.path.lstrip("/") + ".rejected")))
        self.assertTrue(os.path.exists(os.path.join(raw, "REJECTED_bse.txt")))
        self.assertFalse(os.path.exists(os.path.join(raw, self.path.lstrip("/"))))
        code, s = self.go(url=self.url)                 # refetches instead of reusing the bad file
        self.assertEqual((code, s["status"]), (0, "OK"))
        self.assertEqual(self.srv.hits[self.path], 2)

    def test_403_stops_and_suggests_manual_file(self):
        self.srv.routes[self.path] = [(403, b"", {})]
        code, s = self.go(url=self.url)
        self.assertEqual((code, s["status"]), (3, "FAILED"))
        self.assertIn("--file", " ".join(s["messages"]))
        self.assertEqual(self.srv.hits[self.path], 1)

    def test_bse_referer_is_actually_sent(self):
        self.srv.routes[self.path] = [(200, bse_csv().encode(), {})]
        self.go(url=self.url)
        self.assertEqual(self.srv.headers_seen[0]["Referer"], "https://www.bseindia.com/")

    def test_nse_default_headers_unchanged(self):
        self.srv.routes["/x"] = [(200, b"ok", {})]
        nb.fetch_bytes(self.srv.base + "/x", sleep=self.clock.sleep)
        self.assertEqual(self.srv.headers_seen[0]["Referer"], "https://www.nseindia.com/")


class Compare(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = self.tmp.name
        self.nse = os.path.join(self.out, "bhavcopy")
        os.makedirs(self.nse)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, nse_rows=NSE_ROWS, bse_lines=REAL):
        write_text(os.path.join(self.nse, "2026-10-30.csv"), "\n".join([NSE_HEAD] + nse_rows) + "\n")
        src = os.path.join(self.out, "b.csv")
        write_text(src, bse_csv(bse_lines))
        self.assertEqual(bb.run(D, out_dir=self.out, file=src, min_rows=1, now=Clock(ist(2026, 10, 30, 19, 0)).now)[0], 0)

    def cmp(self):
        nse, bse = ce.load(os.path.join(self.nse, "2026-10-30.csv"), True), ce.load(os.path.join(self.out, "bse", "2026-10-30.csv"), False)
        return {r["symbol"]: r for r in ce.compare(nse, bse)}

    def test_diffs_match_hand_calculation(self):
        self.build()
        r = self.cmp()
        self.assertEqual(set(r), {"INFY", "RELIANCE"})  # TCS not on BSE file; ABB/AEGISLOG not in NSE file
        self.assertAlmostEqual(r["INFY"]["diff_pct"], 3.0 / 1185.0 * 100, places=6)       # about 0.2532%
        self.assertAlmostEqual(r["RELIANCE"]["diff_pct"], 1.35 / 1164.9 * 100, places=6)  # about 0.1159%
        self.assertLess(r["INFY"]["vol_ratio"], 0.04)  # BSE volume is a tiny share of NSE volume

    def test_non_eq_nse_series_excluded(self):
        be = NSE_ROWS + ["RELINFRA,BE,2026-10-30,INE036A01016,47.0,48,49,46,46.9,46.9,47,1000.0,5.0,10.0,500.0,50.0,True,"]
        self.build(nse_rows=be)
        self.assertNotIn("RELINFRA", self.cmp())

    def test_bse_no_trade_rows_excluded(self):
        zero = bse_row("INFY", "INE009A01021", vol=0, o=0, h=0, l=0, trades=0)
        self.build(bse_lines=[zero, REAL[3]])
        self.assertEqual(set(self.cmp()), {"RELIANCE"})

    def test_duplicate_isin_on_bse_excluded(self):
        self.build(bse_lines=[REAL[2], bse_row("INFY2", "INE009A01021"), REAL[3]])
        self.assertEqual(set(self.cmp()), {"RELIANCE"})

    def test_load_drops_ambiguous_isin_itself(self):
        # two usable rows sharing an ISIN must never be compared, even if the upstream file allowed it
        head = "isin,symbol,series,close,volume,trades,usable,flags"
        path = os.path.join(self.out, "dup.csv")
        write_text(path, "\n".join([head, "INE1,A,EQ,100,10,5,True,", "INE1,B,EQ,101,10,5,True,",
                                    "INE2,C,EQ,50,10,5,True,"]) + "\n")
        self.assertEqual(set(ce.load(path, True)), {"INE2"})

    def test_summary_thresholds(self):
        self.build()
        s = ce.summarise(list(self.cmp().values()), 0)
        self.assertEqual(s["n"], 2)
        self.assertEqual(s["within"][0.10], 0.0)
        self.assertEqual(s["within"][0.25], 50.0)   # RELIANCE only
        self.assertEqual(s["within"][0.50], 100.0)
        self.assertEqual(ce.summarise(list(self.cmp().values()), 20000)["n"], 1)  # only RELIANCE has >= 20,000 BSE trades
        self.assertEqual(ce.summarise(list(self.cmp().values()), 50000)["n"], 0)

    def test_percentile(self):
        self.assertEqual(ce.percentile([1, 2, 3, 4, 5], 50), 3)
        self.assertEqual(ce.percentile([1, 2, 3, 4, 5], 100), 5)
        self.assertAlmostEqual(ce.percentile([0, 10], 25), 2.5)

    def test_cli_report_and_csv(self):
        self.build()
        outcsv = os.path.join(self.out, "rep", "x.csv")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ce.main(["--date", "2026-10-30", "--out-dir", self.out, "--out", outcsv])
        text = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("matched by ISIN: 2", text)
        self.assertIn("BSE volume cannot stand in for NSE volume", text)
        self.assertIn("not compared", text)  # TCS requested by default but absent from BSE file
        self.assertTrue(os.path.exists(outcsv))

    def test_missing_input_files(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(ce.main(["--date", "2026-10-30", "--out-dir", self.out]), 1)
        self.assertIn("not found", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
