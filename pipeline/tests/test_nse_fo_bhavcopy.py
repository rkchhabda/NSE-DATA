"""
test_nse_fo_bhavcopy.py - Comprehensive unit tests for NSE F&O Bhavcopy Fetcher.
Zero external dependencies required. Simulates network endpoints using local HTTP server.
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
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "fetchers"))
import nse_fo_bhavcopy as fo  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
D = date(2026, 10, 30)  # Friday


def make_zip(filename: str, content: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(filename, content)
    return buf.getvalue()


def sample_udiff_csv(dt_str="2026-10-30", expiry="2026-11-26") -> str:
    headers = [
        "TradDt", "BizDt", "Sgmt", "Src", "FinInstrmTp", "FinInstrmId", "ISIN",
        "TckrSymb", "SctySrs", "XpryDt", "FininstrmActlXpryDt", "StrkPric",
        "OptnTp", "FinInstrmNm", "OpnPric", "HghPric", "LwPric", "ClsPric",
        "SttlmPric", "OpnIntrst", "ChngInOpnIntrst", "TtlTradgVol", "TtlTrfVal"
    ]
    rows = [
        # RELIANCE Futures (Near month: 2026-11-26): Price Up, OI Up -> LONG_BUILDUP
        f"{dt_str},{dt_str},FO,NSE,FUTSTK,101,INE002A01018,RELIANCE,XX,{expiry},{expiry},0.0,XX,FUTSTK,2700.0,2760.0,2695.0,2750.0,2750.0,1000000,100000,5000,137500000",
        # RELIANCE Next month Future
        f"{dt_str},{dt_str},FO,NSE,FUTSTK,102,INE002A01018,RELIANCE,XX,2026-12-31,2026-12-31,0.0,XX,FUTSTK,2715.0,2775.0,2710.0,2765.0,2765.0,200000,20000,1000,27650000",
        # RELIANCE Options (Call at 2800 -> High OI: Call Wall)
        f"{dt_str},{dt_str},FO,NSE,OPTSTK,103,INE002A01018,RELIANCE,XX,{expiry},{expiry},2800.0,CE,OPTSTK,25.0,30.0,20.0,28.0,28.0,500000,50000,2000,5600000",
        # RELIANCE Option (Call at 2900)
        f"{dt_str},{dt_str},FO,NSE,OPTSTK,104,INE002A01018,RELIANCE,XX,{expiry},{expiry},2900.0,CE,OPTSTK,10.0,12.0,8.0,9.0,9.0,200000,10000,800,720000",
        # RELIANCE Options (Put at 2700 -> High OI: Put Wall)
        f"{dt_str},{dt_str},FO,NSE,OPTSTK,105,INE002A01018,RELIANCE,XX,{expiry},{expiry},2700.0,PE,OPTSTK,35.0,38.0,22.0,25.0,25.0,600000,80000,2500,6250000",
        # RELIANCE Option (Put at 2600)
        f"{dt_str},{dt_str},FO,NSE,OPTSTK,106,INE002A01018,RELIANCE,XX,{expiry},{expiry},2600.0,PE,OPTSTK,12.0,15.0,8.0,10.0,10.0,150000,5000,500,500000",
        # TCS Futures: Price Up, OI Down -> SHORT_COVERING
        f"{dt_str},{dt_str},FO,NSE,FUTSTK,201,INE467B01029,TCS,XX,{expiry},{expiry},0.0,XX,FUTSTK,3950.0,4020.0,3940.0,4010.0,4010.0,800000,-60000,4000,160400000",
        # NIFTY Index Futures
        f"{dt_str},{dt_str},FO,NSE,FUTIDX,1,INF969E01018,NIFTY,XX,{expiry},{expiry},0.0,XX,FUTIDX,24200.0,24400.0,24150.0,24350.0,24350.0,5000000,250000,20000,1217500000",
    ]
    return ",".join(headers) + "\n" + "\n".join(rows) + "\n"


def sample_classic_csv(dt_str="30-OCT-2026") -> str:
    headers = [
        "INSTRUMENT", "SYMBOL", "EXPIRY_DT", "STRIKE_PR", "OPTION_TYP",
        "OPEN", "HIGH", "LOW", "CLOSE", "SETTLE_PR", "CONTRACTS",
        "VAL_INLAKH", "OPEN_INT", "CHG_IN_OI", "TIMESTAMP"
    ]
    rows = [
        f"FUTSTK,INFY,26-Nov-2026,0.00,XX,1850.00,1880.00,1845.00,1875.00,1875.00,3500,6562.50,1500000,120000,{dt_str}",
        f"OPTSTK,INFY,26-Nov-2026,1900.00,CE,22.00,28.00,18.00,25.00,25.00,1200,300.00,600000,50000,{dt_str}",
        f"OPTSTK,INFY,26-Nov-2026,1800.00,PE,30.00,32.00,18.00,20.00,20.00,1500,300.00,800000,70000,{dt_str}",
    ]
    return ",".join(headers) + "\n" + "\n".join(rows) + "\n"


class LocalServer:
    def __init__(self):
        self.routes = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                queue = outer.routes.get(self.path)
                if not queue:
                    status, body, headers = 404, b"", {}
                else:
                    status, body, headers = queue[0]
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


class TestNSEFOBhavcopy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = os.path.join(self.tmp.name, "data")
        self.config_dir = os.path.join(self.tmp.name, "config")
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.config_dir, exist_ok=True)
        with open(os.path.join(self.config_dir, "holidays.txt"), "w") as f:
            f.write("# Holiday list\n2026-10-20\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_buildup_classification_matrix(self):
        # Long Buildup: Price >= +0.25% & OI >= +2.0%
        self.assertEqual(fo.classify_buildup(1.5, 5.0), "LONG_BUILDUP")
        # Short Covering: Price >= +0.25% & OI <= -2.0%
        self.assertEqual(fo.classify_buildup(0.8, -3.5), "SHORT_COVERING")
        # Short Buildup: Price <= -0.25% & OI >= +2.0%
        self.assertEqual(fo.classify_buildup(-1.2, 4.0), "SHORT_BUILDUP")
        # Long Unwinding: Price <= -0.25% & OI <= -2.0%
        self.assertEqual(fo.classify_buildup(-0.5, -4.5), "LONG_UNWINDING")
        # Neutral within threshold
        self.assertEqual(fo.classify_buildup(0.1, 1.0), "NEUTRAL")

    def test_udiff_parsing_and_aggregation(self):
        csv_text = sample_udiff_csv()
        records = fo.parse_fo_records(csv_text, D)
        self.assertGreater(len(records), 5)

        agg = fo.aggregate_symbol_derivatives(records, D)
        self.assertIn("RELIANCE", agg)
        rel = agg["RELIANCE"]

        self.assertFalse(rel["is_index"])
        self.assertEqual(rel["near_future_close"], 2750.0)
        self.assertEqual(rel["total_fut_oi"], 1200000)
        self.assertEqual(rel["total_fut_chg_oi"], 120000)
        # 120k / (1.2m - 120k) = 120/1080 = +11.11%
        self.assertAlmostEqual(rel["fut_oi_change_pct"], 11.11, places=1)
        self.assertEqual(rel["buildup"], "LONG_BUILDUP")

        # Options walls: 2800 Call has 500k OI, 2700 Put has 600k OI
        self.assertEqual(rel["call_wall"], 2800.0)
        self.assertEqual(rel["put_wall"], 2700.0)
        # PCR: Put OI = 750k (600k + 150k), Call OI = 700k (500k + 200k) -> 750/700 = 1.07
        self.assertEqual(rel["pcr_oi"], 1.07)

        # Check TCS (Short covering)
        self.assertIn("TCS", agg)
        tcs = agg["TCS"]
        self.assertEqual(tcs["buildup"], "SHORT_COVERING")

        # Check NIFTY (Index flag)
        self.assertIn("NIFTY", agg)
        self.assertTrue(agg["NIFTY"]["is_index"])

    def test_classic_csv_parsing(self):
        csv_text = sample_classic_csv()
        records = fo.parse_fo_records(csv_text, D)
        self.assertEqual(len(records), 3)

        agg = fo.aggregate_symbol_derivatives(records, D)
        self.assertIn("INFY", agg)
        infy = agg["INFY"]
        self.assertEqual(infy["near_future_close"], 1875.0)
        self.assertEqual(infy["total_fut_oi"], 1500000)
        self.assertEqual(infy["call_wall"], 1900.0)
        self.assertEqual(infy["put_wall"], 1800.0)
        self.assertEqual(infy["pcr_oi"], 1.33)

    def test_stale_file_detection(self):
        csv_text = sample_udiff_csv(dt_str="2026-10-29")  # yesterday
        with self.assertRaises(fo.StaleFile):
            fo.parse_fo_records(csv_text, D)

    def test_wrong_date_detection(self):
        csv_text = sample_udiff_csv(dt_str="2026-11-05")  # future date
        with self.assertRaises(fo.WrongDate):
            fo.parse_fo_records(csv_text, D)

    def test_end_to_end_local_zip(self):
        zip_bytes = make_zip("BhavCopy_NSE_FO_0_0_0_20261030_F_0000.csv", sample_udiff_csv())
        local_zip_path = os.path.join(self.tmp.name, "test_fo.zip")
        with open(local_zip_path, "wb") as f:
            f.write(zip_bytes)

        code = fo.run_fo_fetch(
            target_date=D,
            file_path=local_zip_path,
            data_dir=self.data_dir,
            config_dir=self.config_dir,
        )
        self.assertEqual(code, 0)

        out_json = os.path.join(self.data_dir, "fo", "2026-10-30.json")
        out_csv = os.path.join(self.data_dir, "fo", "2026-10-30.csv")
        health_json = os.path.join(self.data_dir, "fo", "2026-10-30.health.json")

        self.assertTrue(os.path.exists(out_json))
        self.assertTrue(os.path.exists(out_csv))
        self.assertTrue(os.path.exists(health_json))

        with open(out_json, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["date"], "2026-10-30")
        self.assertIn("RELIANCE", data["stocks"])
        self.assertEqual(data["stocks"]["RELIANCE"]["buildup"], "LONG_BUILDUP")

    def test_weekend_skipping(self):
        sunday = date(2026, 11, 1)
        code = fo.run_fo_fetch(
            target_date=sunday,
            data_dir=self.data_dir,
            config_dir=self.config_dir,
        )
        self.assertEqual(code, 0)

    def test_network_404_not_published(self):
        srv = LocalServer()
        try:
            old_urls = dict(fo.URLS)
            old_opener = fo._OPENER
            fo.URLS["udiff"] = srv.base + "/missing_udiff_{yyyymmdd}.zip"
            fo.URLS["classic"] = srv.base + "/missing_classic_{yyyy}{mmm}{dd}.zip"
            fo._OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

            # Server returns 404 for missing paths
            code = fo.run_fo_fetch(
                target_date=D,
                data_dir=self.data_dir,
                config_dir=self.config_dir,
                poll=False,
            )
            self.assertEqual(code, 2)  # NotPublished returns code 2
        finally:
            srv.stop()
            fo.URLS.update(old_urls)
            fo._OPENER = old_opener

    def test_network_403_blocked(self):
        srv = LocalServer()
        try:
            old_urls = dict(fo.URLS)
            old_opener = fo._OPENER
            udiff_path = "/udiff_403_20261030.zip"
            srv.routes[udiff_path] = [(403, b"Forbidden", {})]
            fo.URLS["udiff"] = srv.base + "/udiff_403_{yyyymmdd}.zip"
            fo._OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

            code = fo.run_fo_fetch(
                target_date=D,
                data_dir=self.data_dir,
                config_dir=self.config_dir,
                poll=False,
            )
            self.assertEqual(code, 3)  # Blocked halts and returns 3
        finally:
            srv.stop()
            fo.URLS.update(old_urls)
            fo._OPENER = old_opener


if __name__ == "__main__":
    unittest.main()

