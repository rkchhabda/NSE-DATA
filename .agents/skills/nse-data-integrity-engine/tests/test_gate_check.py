"""
Automated tests for scripts/gate_check.py.

Run from the skill folder:
    python -m unittest discover -s tests -v          (Windows: py -m unittest discover -s tests -v)

Each test pins one rule from SKILL.md / references/scoring-spec.md. If you change a
threshold, weight or tolerance on purpose, update the matching test in the same commit.
"""
import copy
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import gate_check as g  # noqa: E402

TD = "2026-10-29"


def load_sample():
    with open(os.path.join(ROOT, "assets", "sample_input.json"), encoding="utf-8") as f:
        return {s["symbol"]: s for s in json.load(f)["stocks"]}


def clean():
    return copy.deepcopy(load_sample()["SAMPLE_CLEAN"])


def rec(src, val, as_of=TD):
    return {"source": src, "value": val, "as_of": as_of}


class GateRules(unittest.TestCase):
    # ---- the five shipped scenarios ------------------------------------
    def test_sample_scenarios(self):
        expected = {
            "SAMPLE_CLEAN": "BUY NOW",
            "SAMPLE_BREAKOUT": "WAIT FOR BREAKOUT",
            "SAMPLE_YAHOO_ONLY": "WATCH",
            "SAMPLE_CONFLICT": "WATCH",
            "SAMPLE_STALE_UNADJ": "WATCH",
        }
        for sym, stock in load_sample().items():
            self.assertEqual(g.evaluate(stock)["status"], expected[sym], sym)

    # ---- source independence --------------------------------------------
    def test_yahoo_corroboration_is_weak(self):
        s = clean()
        s["evidence"]["cmp"] = [rec("nse_bhavcopy", 100), rec("yahoo", 100)]
        r = g.evaluate(s)
        self.assertEqual(r["validation"]["cmp"]["status"], "WEAK")
        self.assertNotEqual(r["status"], "BUY NOW")

    def test_nse_api_plus_bhavcopy_is_one_origin(self):
        s = clean()
        s["evidence"]["cmp"] = [rec("nse_bhavcopy", 100), rec("nse_api", 100)]
        self.assertEqual(g.evaluate(s)["validation"]["cmp"]["status"], "SINGLE_OFFICIAL")

    def test_nse_plus_bse_is_verified(self):
        s = clean()
        s["evidence"]["cmp"] = [rec("nse_bhavcopy", 100), rec("bse_api", 100.05)]
        self.assertEqual(g.evaluate(s)["validation"]["cmp"]["status"], "VERIFIED")

    def test_bse_bhavcopy_is_a_second_official_origin(self):
        s = clean()
        s["evidence"]["cmp"] = [rec("nse_bhavcopy", 100), {"source": "bse_bhavcopy", "value": 100.05, "as_of": TD, "trades": 300}]
        self.assertEqual(g.evaluate(s)["validation"]["cmp"]["status"], "VERIFIED")

    def test_bse_bhavcopy_and_bse_api_are_one_origin(self):
        s = clean()
        s["evidence"]["cmp"] = [rec("bse_bhavcopy", 100), rec("bse_api", 100)]
        self.assertEqual(g.evaluate(s)["validation"]["cmp"]["status"], "SINGLE_OFFICIAL")

    # ---- cross-exchange rule (NSE vs BSE) ---------------------------------
    def bse(self, nse, bse, trades=500):
        s = clean()
        s["evidence"]["cmp"] = [rec("nse_bhavcopy", nse),
                                {"source": "bse_bhavcopy", "value": bse, "as_of": TD, "trades": trades}]
        return g.evaluate(s)["validation"]["cmp"]

    def test_real_infy_and_tcs_pairs_verify_across_exchanges(self):
        self.assertEqual(self.bse(1185.0, 1182.0, 11199)["status"], "VERIFIED")    # 0.253%
        self.assertEqual(self.bse(2502.6, 2496.0, 11650)["status"], "VERIFIED")    # 0.264%
        self.assertEqual(self.bse(1164.9, 1163.55, 36671)["status"], "VERIFIED")   # 0.116%

    def test_cross_exchange_boundary(self):
        self.assertEqual(self.bse(1000, 1004.9)["status"], "VERIFIED")             # 0.49%
        self.assertEqual(self.bse(1000, 1006.0)["status"], "CONFLICT")             # 0.60%

    def test_thin_bse_trading_is_ignored_not_a_conflict(self):
        v = self.bse(100.0, 110.0, trades=2)       # a 10% gap, but only 2 BSE trades
        self.assertEqual(v["status"], "SINGLE_OFFICIAL")
        self.assertTrue(v["ignored"])
        self.assertEqual(self.bse(100.0, 100.0, trades=99)["status"], "SINGLE_OFFICIAL")
        self.assertEqual(self.bse(100.0, 100.0, trades=100)["status"], "VERIFIED")

    def test_bse_without_trade_count_is_ignored(self):
        s = clean()
        s["evidence"]["cmp"] = [rec("nse_bhavcopy", 100), rec("bse_bhavcopy", 100)]
        self.assertEqual(g.evaluate(s)["validation"]["cmp"]["status"], "SINGLE_OFFICIAL")

    def test_active_bse_that_disagrees_is_a_real_conflict(self):
        self.assertEqual(self.bse(100.0, 110.0, trades=5000)["status"], "CONFLICT")

    def test_same_exchange_tolerance_unchanged_for_vendors(self):
        s = clean()
        s["evidence"]["cmp"] = [rec("nse_bhavcopy", 1000), rec("twelve_data", 1002.5)]  # 0.25%
        self.assertEqual(g.evaluate(s)["validation"]["cmp"]["status"], "CONFLICT")

    def test_bse_volume_can_never_verify_nse_volume(self):
        s = clean()
        s["evidence"]["volume"] = [rec("nse_bhavcopy", 13030713), rec("bse_bhavcopy", 459395)]
        v = g.evaluate(s)["validation"]["volume"]
        self.assertEqual(v["status"], "SINGLE_OFFICIAL")
        self.assertTrue(any("cannot corroborate" in w for w in v["warnings"]))
        s["evidence"]["volume"] = [rec("nse_bhavcopy", 13030713), rec("bse_bhavcopy", 13030713)]
        self.assertEqual(g.evaluate(s)["validation"]["volume"]["status"], "SINGLE_OFFICIAL")  # even if identical

    # ---- volume rule B: NSE's two files ------------------------------------
    def vol(self, records, **cfg):
        s = clean()
        s["evidence"]["volume"] = records
        old = {k: g.CONFIG[k] for k in cfg}
        g.CONFIG.update(cfg)
        try:
            return g.evaluate(s)
        finally:
            g.CONFIG.update(old)

    def test_two_nse_files_agreeing_verify_volume_but_are_labelled_weaker(self):
        r = self.vol([rec("nse_bhavcopy", 1200000), rec("nse_udiff", 1200000)])
        self.assertEqual(r["validation"]["volume"]["status"], "NSE_TWO_FILES")
        self.assertEqual(r["status"], "BUY NOW")
        self.assertTrue(r["validation_status"].startswith("VERIFIED (weaker:"))
        self.assertTrue(any("volume" in w for w in r["weaker_checks"]))
        self.assertLess(r["data_confidence"]["score"], g.evaluate(clean())["data_confidence"]["score"])  # costs confidence

    def test_switching_the_rule_off_restores_strict_volume(self):
        r = self.vol([rec("nse_bhavcopy", 1200000), rec("nse_udiff", 1200000)], volume_two_file_ok=False)
        self.assertEqual(r["validation"]["volume"]["status"], "SINGLE_OFFICIAL")
        self.assertNotEqual(r["status"], "BUY NOW")

    def test_two_nse_files_that_disagree_do_not_verify(self):
        r = self.vol([rec("nse_bhavcopy", 1200000), rec("nse_udiff", 1250000)])
        self.assertEqual(r["validation"]["volume"]["status"], "SINGLE_OFFICIAL")
        self.assertNotEqual(r["status"], "BUY NOW")

    def test_one_nse_file_alone_is_not_enough(self):
        self.assertEqual(self.vol([rec("nse_bhavcopy", 1200000)])["validation"]["volume"]["status"], "SINGLE_OFFICIAL")
        self.assertEqual(self.vol([rec("nse_udiff", 1200000)])["validation"]["volume"]["status"], "SINGLE_OFFICIAL")

    def test_a_stale_second_file_does_not_count(self):
        r = self.vol([rec("nse_bhavcopy", 1200000), rec("nse_udiff", 1200000, "2026-10-28")])
        self.assertEqual(r["validation"]["volume"]["status"], "SINGLE_OFFICIAL")

    def test_bse_volume_still_rejected_alongside_two_files(self):
        r = self.vol([rec("nse_bhavcopy", 1200000), rec("nse_udiff", 1200000), rec("bse_bhavcopy", 50000)])
        v = r["validation"]["volume"]
        self.assertEqual(v["status"], "NSE_TWO_FILES")
        self.assertTrue(any("cannot corroborate" in w for w in v["warnings"]))

    def test_vendor_verified_volume_is_not_labelled_weaker(self):
        r = g.evaluate(clean())
        self.assertEqual(r["validation_status"], "VERIFIED")
        self.assertEqual(r["weaker_checks"], [])

    def test_weaker_label_appears_in_the_report(self):
        r = self.vol([rec("nse_bhavcopy", 1200000), rec("nse_udiff", 1200000)])
        self.assertIn("WEAKER CHECKS USED", g.render_report([r], {}))

    def test_computed_only_indicators_are_labelled_when_allowed(self):
        s = clean()
        for spec in s["evidence"]["indicators"].values():
            spec["vendors"] = []
        old = g.CONFIG["allow_computed_only_indicators"]
        g.CONFIG["allow_computed_only_indicators"] = True
        try:
            r = g.evaluate(s)
        finally:
            g.CONFIG["allow_computed_only_indicators"] = old
        self.assertTrue(any("indicators" in w for w in r["weaker_checks"]))
        self.assertEqual(r["status"], "BUY NOW")

    def test_two_vendors_without_official_is_weak(self):
        s = clean()
        s["evidence"]["cmp"] = [rec("twelve_data", 100), rec("alpha_vantage", 100)]
        self.assertEqual(g.evaluate(s)["validation"]["cmp"]["status"], "WEAK")

    def test_unknown_source_is_ignored(self):
        s = clean()
        s["evidence"]["cmp"] = [rec("random_blog", 100), rec("nse_bhavcopy", 100)]
        v = g.evaluate(s)["validation"]["cmp"]
        self.assertEqual(v["status"], "SINGLE_OFFICIAL")
        self.assertTrue(any("unknown source" in w for w in v["warnings"]))

    # ---- dates ------------------------------------------------------------
    def test_stale_record_is_dropped(self):
        s = clean()
        s["evidence"]["cmp"] = [rec("nse_bhavcopy", 100), rec("twelve_data", 100, "2026-10-28")]
        v = g.evaluate(s)["validation"]["cmp"]
        self.assertEqual(v["status"], "SINGLE_OFFICIAL")
        self.assertEqual(len(v["dropped"]), 1)

    def test_price_record_without_date_is_dropped(self):
        s = clean()
        s["evidence"]["cmp"] = [rec("nse_bhavcopy", 100),
                                {"source": "twelve_data", "value": 100}]
        v = g.evaluate(s)["validation"]["cmp"]
        self.assertEqual(v["status"], "SINGLE_OFFICIAL")

    # ---- tolerances -------------------------------------------------------
    def test_close_tolerance_boundary(self):
        ok, bad = clean(), clean()
        ok["evidence"]["cmp"] = [rec("nse_bhavcopy", 1000), rec("twelve_data", 1000.9)]   # 0.09%
        bad["evidence"]["cmp"] = [rec("nse_bhavcopy", 1000), rec("twelve_data", 1001.5)]  # 0.15%
        self.assertEqual(g.evaluate(ok)["validation"]["cmp"]["status"], "VERIFIED")
        self.assertEqual(g.evaluate(bad)["validation"]["cmp"]["status"], "CONFLICT")

    def test_live_mode_uses_wider_tolerance(self):
        s = clean()
        s["cmp_mode"] = "live"
        s["evidence"]["cmp"] = [rec("nse_api", 1000), rec("bse_api", 1002)]  # 0.2%
        self.assertEqual(g.evaluate(s)["validation"]["cmp"]["status"], "VERIFIED")

    def test_zero_value_is_valid(self):  # promoter pledge of 0 must not be treated as missing
        r = g.evaluate(clean())
        self.assertEqual(r["validation"]["ownership"]["promoter_pledge"]["status"], "VERIFIED")

    # ---- confidence caps and penalties ------------------------------------
    def test_conflict_caps_confidence_at_49(self):
        s = clean()
        s["evidence"]["cmp"] = [rec("nse_bhavcopy", 100), rec("twelve_data", 110)]
        r = g.evaluate(s)
        self.assertLessEqual(r["data_confidence"]["score"], 49)

    def test_unadjusted_history_caps_at_59_and_blocks_buy(self):
        s = clean()
        s["evidence"]["history"]["adjusted"] = False
        r = g.evaluate(s)
        self.assertLessEqual(r["data_confidence"]["score"], 59)
        self.assertNotEqual(r["status"], "BUY NOW")
        self.assertTrue(r["excluded_for_data_quality"])

    def test_conviction_penalty_tiers_are_not_cumulative(self):
        self.assertEqual(g.conviction_penalty(95), 0)
        self.assertEqual(g.conviction_penalty(80), 0)
        self.assertEqual(g.conviction_penalty(79.9), -5)
        self.assertEqual(g.conviction_penalty(59.9), -10)
        self.assertEqual(g.conviction_penalty(39.9), -20)

    def test_stale_fundamentals_reduce_only_that_component(self):
        fresh, stale = clean(), clean()
        stale["evidence"]["fundamentals_meta"] = {"age_days": 45, "newer_result_filed": False}
        rf, rs = g.evaluate(fresh), g.evaluate(stale)
        cf, cs = rf["data_confidence"]["components"], rs["data_confidence"]["components"]
        self.assertAlmostEqual(cs["fundamentals"], cf["fundamentals"] * 0.9, delta=0.2)
        self.assertEqual(cs["cmp"], cf["cmp"])
        self.assertEqual(cs["volume"], cf["volume"])

    def test_newer_result_filed_also_marks_stale(self):
        s = clean()
        s["evidence"]["fundamentals_meta"] = {"age_days": 3, "newer_result_filed": True}
        self.assertTrue(any("stale" in n for n in g.evaluate(s)["notes"]))

    # ---- gate thresholds --------------------------------------------------
    def test_each_score_threshold_blocks_buy_now(self):
        limits = {"market_regime": 35, "technical": 75, "fundamental": 65,
                  "sentiment": 60, "backtest": 60}
        for key, limit in limits.items():
            at, below = clean(), clean()
            at["scores"][key] = limit
            below["scores"][key] = limit - 1
            self.assertEqual(g.evaluate(at)["status"], "BUY NOW", f"{key} at limit")
            self.assertNotEqual(g.evaluate(below)["status"], "BUY NOW", f"{key} below limit")

    def test_missing_score_blocks_buy_now(self):
        s = clean()
        del s["scores"]["sentiment"]
        self.assertNotEqual(g.evaluate(s)["status"], "BUY NOW")

    def test_insufficient_backtest_data_blocks_buy_now(self):
        s = clean()
        s["evidence"]["backtest"]["signals"] = 29
        self.assertNotEqual(g.evaluate(s)["status"], "BUY NOW")

    def test_computed_only_indicator_blocks_buy_by_default(self):
        s = clean()
        s["evidence"]["indicators"]["rsi14"]["vendors"] = []
        r = g.evaluate(s)
        self.assertEqual(r["validation"]["indicators"]["rsi14"]["status"], "COMPUTED_ONLY")
        self.assertNotEqual(r["status"], "BUY NOW")

    def test_indicator_mismatch_blocks_buy_now(self):
        s = clean()
        s["evidence"]["indicators"]["rsi14"]["vendors"][0]["value"] = 70.0  # internal 62.4
        r = g.evaluate(s)
        self.assertEqual(r["validation"]["indicators"]["rsi14"]["status"], "CONFLICT")
        self.assertNotEqual(r["status"], "BUY NOW")

    # ---- fallback status --------------------------------------------------
    def test_wait_for_breakout_needs_trusted_data(self):
        s = clean()
        s["setup"] = {"fallback_status": "WAIT FOR BREAKOUT"}
        s["scores"]["technical"] = 70
        self.assertEqual(g.evaluate(s)["status"], "WAIT FOR BREAKOUT")
        s["evidence"]["cmp"] = [rec("nse_bhavcopy", 100), rec("yahoo", 100)]
        self.assertEqual(g.evaluate(s)["status"], "WATCH")

    def test_data_quality_overrides_perfect_scores(self):
        s = clean()
        s["scores"] = {k: 100 for k in s["scores"]}
        s["evidence"]["volume"] = [rec("nse_bhavcopy", 1000)]
        self.assertNotEqual(g.evaluate(s)["status"], "BUY NOW")

    # ---- robustness -------------------------------------------------------
    def test_empty_stock_does_not_crash(self):
        r = g.evaluate({"symbol": "EMPTY"})
        self.assertEqual(r["status"], "WATCH")
        self.assertEqual(r["data_confidence"]["score"], 0)

    def test_garbage_values_are_ignored(self):
        s = clean()
        s["evidence"]["cmp"] = [rec("nse_bhavcopy", "100"), rec("twelve_data", True),
                                rec("bse_api", float("nan"))]
        self.assertEqual(g.evaluate(s)["validation"]["cmp"]["status"], "MISSING")

    def test_report_renders_and_carries_disclaimer(self):
        results = [g.evaluate(s) for s in load_sample().values()]
        text = g.render_report(results, {"run_date": TD})
        self.assertIn("Source Health", text)
        self.assertIn("Not investment advice", text)
        self.assertIn("Alpha Vantage | NOT RUN", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
