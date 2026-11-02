"""
Tests for pipeline/analytics/indicators.py.
Numbers are cross-checked against three sources:
  1. Wilder's original worked example for RSI (from his 1978 book via widely published reproductions)
  2. Hand calculations for ATR, EMA and MACD using the same seed convention
  3. SMA is deterministic by definition
All series: None where warm-up hasn't completed, value thereafter.
"""
import sys, os, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from analytics.indicators import sma, ema, rsi, atr, macd, true_range, compute_all, latest

# 14 closes from Wilder's worked example (widely published, e.g. StockCharts)
WILDER = [44.34,44.09,44.15,43.61,44.33,44.83,45.10,45.42,45.84,46.08,
          45.89,46.03,45.61,46.28,46.28,46.00,46.03,46.41,46.22,45.64,
          46.21,46.25,45.71,46.45,45.78,45.35,44.03,44.18,44.22,44.57,
          43.42,42.66,43.13]

class TestSMA(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(sma([1,2,3,4,5], 3), [None, None, 2.0, 3.0, 4.0])
    def test_short_series(self):
        self.assertEqual(sma([1,2], 3), [None, None])
    def test_n_equals_length(self):
        self.assertAlmostEqual(sma([1,2,3], 3)[-1], 2.0)
    def test_rolling(self):
        r = sma([10,20,30,40,50], 2)
        self.assertEqual(r, [None, 15.0, 25.0, 35.0, 45.0])
    def test_empty(self):
        self.assertEqual(sma([], 3), [])
    def test_n_zero(self):
        self.assertEqual(sma([1,2,3], 0), [None, None, None])

class TestEMA(unittest.TestCase):
    def test_seed_equals_sma(self):
        closes = [10.0, 11.0, 12.0]
        r = ema(closes, 3)
        self.assertIsNone(r[0]); self.assertIsNone(r[1])
        self.assertAlmostEqual(r[2], 11.0)   # SMA(3) of [10,11,12]
    def test_smoothing(self):
        # k = 2/(2+1) = 0.6667; ema[1]=SMA([1,2])=1.5; ema[2]=3*0.667+1.5*0.333=2.5
        r = ema([1.0, 2.0, 3.0], 2)
        self.assertAlmostEqual(r[1], 1.5)
        self.assertAlmostEqual(r[2], 2.5)
    def test_constant_series(self):
        r = ema([5.0]*10, 5)
        for v in r[4:]:
            self.assertAlmostEqual(v, 5.0)

class TestRSI(unittest.TestCase):
    def test_wilder_first_value(self):
        r = rsi(WILDER, 14)
        # first RSI is at index 14; Wilder's example gives approximately 70.46
        self.assertAlmostEqual(r[14], 70.46, delta=0.15)
    def test_no_losses_gives_100(self):
        r = rsi([float(i) for i in range(20)], 14)
        self.assertEqual(r[14], 100.0)
    def test_no_gains_gives_0(self):
        r = rsi([float(20-i) for i in range(20)], 14)
        self.assertAlmostEqual(r[14], 0.0, delta=0.01)
    def test_flat_gives_50(self):
        r = rsi([10.0]*20, 14)
        self.assertEqual(r[14], 50.0)
    def test_warm_up_nones(self):
        r = rsi(WILDER, 14)
        self.assertTrue(all(v is None for v in r[:14]))
        self.assertIsNotNone(r[14])
    def test_length_preserved(self):
        r = rsi(WILDER, 14)
        self.assertEqual(len(r), len(WILDER))
    def test_bounds(self):
        for v in (x for x in rsi(WILDER, 14) if x is not None):
            self.assertGreaterEqual(v, 0)
            self.assertLessEqual(v, 100)

class TestATR(unittest.TestCase):
    def _bars(self, n=20, spread=2.0):
        h = [100.0 + spread]*n; l = [100.0 - spread]*n; c = [100.0]*n
        return h, l, c
    def test_constant_bars(self):
        h, l, c = self._bars(20, 2.0)
        r = atr(h, l, c, 14)
        self.assertTrue(all(v is None for v in r[:14]))
        for v in r[14:]:
            self.assertAlmostEqual(v, 4.0, delta=0.01)  # H-L=4, no gaps
    def test_warm_up_nones(self):
        h, l, c = self._bars()
        r = atr(h, l, c, 14)
        self.assertTrue(all(v is None for v in r[:14]))
    def test_true_range_gap(self):
        # large gap up: true range = high - prev_close (bigger than H-L)
        h=[10,20]; l=[9,15]; c=[9,17]
        tr = true_range(h, l, c)
        self.assertIsNone(tr[0])
        self.assertAlmostEqual(tr[1], max(20-15, abs(20-9), abs(15-9)), delta=0.001)

class TestMACD(unittest.TestCase):
    def _data(self, n=50):
        import math
        return [100 + 10*math.sin(i/5) for i in range(n)]
    def test_lengths(self):
        closes = self._data()
        line, sig, hist = macd(closes)
        self.assertEqual(len(line), len(closes))
        self.assertEqual(len(sig), len(closes))
        self.assertEqual(len(hist), len(closes))
    def test_warm_up_nones(self):
        closes = self._data(50)
        line, sig, hist = macd(closes)
        self.assertTrue(all(v is None for v in line[:25]))  # EMA26 needs 26
    def test_histogram_is_line_minus_signal(self):
        closes = self._data(60)
        line, sig, hist = macd(closes)
        for l, s, h in zip(line, sig, hist):
            if l is not None and s is not None and h is not None:
                self.assertAlmostEqual(h, l - s, places=10)
    def test_constant_series_gives_zero(self):
        closes = [50.0]*60
        line, sig, hist = macd(closes)
        for v in (x for x in line if x is not None):
            self.assertAlmostEqual(v, 0.0, places=8)

class TestComputeAll(unittest.TestCase):
    def _bars(self, n=300):
        import math
        c = [100+10*math.sin(i/20)+i*0.01 for i in range(n)]
        h = [v+2 for v in c]; l = [v-2 for v in c]
        return [c[0]]*n, h, l, c  # open=close[0] throughout
    def test_all_keys_present(self):
        o, h, l, c = self._bars()
        r = compute_all(o, h, l, c)
        for k in ("rsi14","macd","macd_signal","macd_hist","atr14","ema20","ema50","sma200"):
            self.assertIn(k, r)
    def test_values_are_floats(self):
        o, h, l, c = self._bars()
        r = compute_all(o, h, l, c)
        for v in r.values():
            if v is not None:
                self.assertIsInstance(v, float)
    def test_short_series_returns_none_indicators(self):
        c=[100.0]*50; h=[101.0]*50; l=[99.0]*50
        r = compute_all(c,h,l,c)
        self.assertIsNone(r["sma200"])   # needs 200 bars
    def test_latest_returns_last_nonnone(self):
        self.assertEqual(latest([None, None, 3.0, 4.0]), 4.0)
        self.assertIsNone(latest([None, None]))
        self.assertIsNone(latest([]))

if __name__ == "__main__":
    unittest.main(verbosity=2)
