"""Tests for pipeline/tools/build_gate_input.py, using the real NSE/BSE lines of 2026-10-30."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "fetchers"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import bse_bhavcopy as bb  # noqa: E402
import build_gate_input as bg  # noqa: E402
from test_bse_bhavcopy import NSE_HEAD, NSE_ROWS, REAL, bse_csv, bse_row  # noqa: E402
from test_nse_bhavcopy import Clock, ist, write_text  # noqa: E402

GATE_DIRS = [os.path.join(os.path.dirname(ROOT), ".agents", "skills", "nse-data-integrity-engine", "scripts"),
             "/home/claude/nse-data-integrity-engine/scripts"]
GATE = next((d for d in GATE_DIRS if os.path.exists(os.path.join(d, "gate_check.py"))), None)
DATE = "2026-10-30"


class Build(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def prep(self, nse_rows=NSE_ROWS, bse_lines=REAL):
        os.makedirs(os.path.join(self.out, "bhavcopy"))
        write_text(os.path.join(self.out, "bhavcopy", f"{DATE}.csv"), "\n".join([NSE_HEAD] + nse_rows) + "\n")
        src = os.path.join(self.out, "b.csv")
        write_text(src, bse_csv(bse_lines))
        from datetime import date
        self.assertEqual(bb.run(date(2026, 10, 30), out_dir=self.out, file=src, min_rows=1,
                                now=Clock(ist(2026, 10, 30, 19, 0)).now)[0], 0)

    def stocks(self, **kw):
        self.prep(**kw)
        rows = lambda p: bg.read_rows(os.path.join(self.out, p, f"{DATE}.csv"))
        return {s["symbol"]: s for s in bg.build(rows("bhavcopy"), rows("bse"), DATE)}

    def test_cmp_gets_bse_record_with_trades_and_volume_stays_nse_only(self):
        s = self.stocks()
        infy = s["INFY"]
        self.assertEqual([r["source"] for r in infy["evidence"]["cmp"]], ["nse_bhavcopy", "bse_bhavcopy"])
        self.assertEqual(infy["evidence"]["cmp"][1]["value"], 1182.0)
        self.assertEqual(infy["evidence"]["cmp"][1]["trades"], 11199.0)
        self.assertEqual([r["source"] for r in infy["evidence"]["volume"]], ["nse_bhavcopy", "nse_udiff"])  # NSE only, never BSE
        self.assertTrue(infy["extras"]["volume_two_files"])
        self.assertEqual(infy["evidence"]["volume"][0]["value"], 13030713.0)

    def test_stock_without_isin_gets_a_single_volume_record(self):
        no_isin = ["NOISIN,EQ,2026-10-30,,10,10,11,9,10,10,10,1000.0,1.0,5.0,500.0,50.0,True,"]
        s = self.stocks(nse_rows=NSE_ROWS + no_isin)
        self.assertEqual([r["source"] for r in s["NOISIN"]["evidence"]["volume"]], ["nse_bhavcopy"])
        self.assertFalse(s["NOISIN"]["extras"]["volume_two_files"])

    def test_unmatched_stock_is_nse_only(self):
        tcs = self.stocks()["TCS"]  # not in the BSE fixture
        self.assertEqual(len(tcs["evidence"]["cmp"]), 1)
        self.assertFalse(tcs["extras"]["bse_matched"])

    def test_non_eq_and_unusable_nse_rows_skipped(self):
        extra = ["BESTOCK,BE,2026-10-30,INE999A01010,10,10,11,9,10,10,10,1000.0,1.0,5.0,500.0,50.0,True,",
                 "BADROW,EQ,2026-10-30,INE998A01010,10,10,11,9,10,10,10,1000.0,1.0,5.0,500.0,50.0,False,bad_ohlc"]
        s = self.stocks(nse_rows=NSE_ROWS + extra)
        self.assertNotIn("BESTOCK", s)
        self.assertNotIn("BADROW", s)

    def test_bse_no_trade_and_duplicate_rows_not_attached(self):
        zero = bse_row("INFY", "INE009A01021", vol=0, o=0, h=0, l=0, trades=0)
        s = self.stocks(bse_lines=[zero, REAL[3]])
        self.assertFalse(s["INFY"]["extras"]["bse_matched"])
        self.assertTrue(s["RELIANCE"]["extras"]["bse_matched"])

    def test_corporate_action_flag_carried_through(self):
        flagged = ["INFY,EQ,2026-10-30,INE009A01021,1146.5,1149.1,1185.0,1147.9,1185.0,1185.0,1174.76,13030713.0,153080.21,184733.0,7575174.0,58.13,True,possible_corporate_action"]
        s = self.stocks(nse_rows=flagged)
        self.assertTrue(s["INFY"]["extras"]["needs_adjustment_check"])

    @unittest.skipUnless(GATE, "gate_check.py not found")
    def test_gate_summary_on_real_numbers(self):
        s = self.stocks()
        gate = bg.load_gate(GATE)
        cmp_c, vol_c = bg.summarise(list(s.values()), gate)
        self.assertEqual(cmp_c["VERIFIED"], 2)          # INFY (0.25%) and RELIANCE (0.12%)
        self.assertEqual(cmp_c["SINGLE_OFFICIAL"], 1)   # TCS: no BSE record
        self.assertEqual(vol_c["NSE_TWO_FILES"], 3)     # NSE two-file check; BSE volume is never used

    @unittest.skipUnless(GATE, "gate_check.py not found")
    def test_cli_writes_file_and_prints_summary(self):
        self.prep()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = bg.main(["--date", DATE, "--out-dir", self.out, "--gate-scripts", GATE])
        text = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("3 stocks written", text)
        self.assertIn("VERIFIED", text)
        with open(os.path.join(self.out, f"gate_input_{DATE}.json"), encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["stocks"]), 3)

    def test_cli_without_gate_still_writes(self):
        self.prep()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = bg.main(["--date", DATE, "--out-dir", self.out, "--gate-scripts", os.path.join(self.out, "nowhere")])
        self.assertEqual(code, 0)
        self.assertIn("skipping the validation summary", buf.getvalue())

    def test_missing_inputs(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(bg.main(["--date", DATE, "--out-dir", self.out]), 1)
        self.assertIn("not found", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
