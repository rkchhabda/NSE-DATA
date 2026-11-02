"""Tests for pipeline/tools/price_basis_check.py using a simulated vendor (no internet, no key)."""
import contextlib
import csv
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
import price_basis_check as pb  # noqa: E402

DATE = "2026-10-30"
# symbol, series, usable, close, last, volume
ROWS = [("AAA", "EQ", "True", 100.0, 101.0, 9000000), ("BBB", "EQ", "True", 200.0, 198.0, 8000000),
        ("CCC", "EQ", "True", 300.0, 300.1, 7000000),   # last ~ close: excluded
        ("DDD", "BE", "True", 50.0, 52.0, 6000000),     # wrong series: excluded
        ("EEE", "EQ", "False", 80.0, 84.0, 5000000),    # unusable: excluded
        ("FFF", "EQ", "True", 400.0, 396.0, 1000)]
BY_SYM = {r[0]: r for r in ROWS}


def write_csv(path):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "series", "usable", "close", "last", "volume"])
        w.writerows(ROWS)


class Vendor:
    def __init__(self, mode):
        self.mode, self.hits, self.queries = mode, 0, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits += 1
                q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
                outer.queries.append(q)
                _, _, _, close, last, vol = BY_SYM[q["symbol"]]
                m = outer.mode
                if m == "error":
                    body = {"code": 403, "message": "plan does not include NSE", "status": "error"}
                elif m == "nodate":
                    body = {"values": [{"datetime": "2026-10-29", "close": "1", "volume": "1"}], "status": "ok"}
                else:
                    c = {"official": close, "last": last, "neither": close * 1.05}[m]
                    body = {"values": [{"datetime": DATE, "close": f"{c:.4f}", "volume": str(vol)}], "status": "ok"}
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.01), daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Tool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.csv = os.path.join(self.tmp.name, "n.csv")
        write_csv(self.csv)
        self._key = os.environ.get(pb.KEY_ENV)
        os.environ[pb.KEY_ENV] = "SECRET-KEY-123"
        self.vendors = []

    def tearDown(self):
        for v in self.vendors:
            v.stop()
        if self._key is None:
            os.environ.pop(pb.KEY_ENV, None)
        else:
            os.environ[pb.KEY_ENV] = self._key
        self.tmp.cleanup()

    def run_tool(self, mode, extra=()):
        v = Vendor(mode)
        self.vendors.append(v)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = pb.main(["--date", DATE, "--csv", self.csv, "--base-url", v.base,
                            "--pause", "0", "--min-diff", "0.3", *extra])
        return code, out.getvalue(), v

    def test_candidate_selection(self):
        picks = pb.pick_candidates(self.csv, 10, 0.3)
        self.assertEqual([p["symbol"] for p in picks], ["AAA", "BBB", "FFF"])  # sorted by volume; CCC/DDD/EEE excluded

    def test_classify(self):
        self.assertEqual(pb.classify(100.0, 100.0, 101.0, 0.1), "OFFICIAL")
        self.assertEqual(pb.classify(101.0, 100.0, 101.0, 0.1), "LAST")
        self.assertEqual(pb.classify(105.0, 100.0, 101.0, 0.1), "NEITHER")
        self.assertEqual(pb.classify(100.0, 100.0, 100.05, 0.1), "BOTH")

    def test_vendor_uses_official_close(self):
        code, out, v = self.run_tool("official")
        self.assertEqual(code, 0)
        self.assertIn("behaves like the OFFICIAL close", out)
        self.assertEqual(v.hits, 3)
        self.assertEqual(v.queries[0]["exchange"], "NSE")

    def test_vendor_uses_last_price(self):
        code, out, _ = self.run_tool("last")
        self.assertEqual(code, 0)
        self.assertIn("behaves like the LAST traded price", out)

    def test_vendor_neither_is_inconclusive(self):
        code, out, _ = self.run_tool("neither")
        self.assertIn("inconclusive", out)

    def test_stops_at_first_vendor_error(self):
        code, out, v = self.run_tool("error")
        self.assertEqual(code, 2)
        self.assertEqual(v.hits, 1)  # does not keep spending requests
        self.assertIn("plan does not include NSE", out)

    def test_missing_bar_for_date(self):
        code, out, v = self.run_tool("nodate")
        self.assertEqual(code, 2)
        self.assertIn("no bar for", out)
        self.assertEqual(v.hits, 3)

    def test_key_never_printed(self):
        for mode in ("official", "error"):
            _, out, _ = self.run_tool(mode)
            self.assertNotIn("SECRET-KEY-123", out)

    def test_missing_key_stops_before_any_request(self):
        os.environ.pop(pb.KEY_ENV)
        code, out, v = self.run_tool("official")
        self.assertEqual(code, 1)
        self.assertEqual(v.hits, 0)
        self.assertIn(pb.KEY_ENV, out)

    def test_missing_csv(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = pb.main(["--date", DATE, "--csv", os.path.join(self.tmp.name, "nope.csv")])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
