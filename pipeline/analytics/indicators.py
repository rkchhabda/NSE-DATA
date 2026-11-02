"""
indicators.py - technical indicators from adjusted daily bars. Pure Python, no dependencies.

Every function takes plain lists and returns a list of the same length, with None where there
is not yet enough data (the warm-up). Nothing is estimated or back-filled.

Conventions (the usual ones, stated so a vendor's numbers can be compared knowingly):
  SMA   simple average of the last n closes.
  EMA   seeded with the SMA of the first n values, then ema = x*k + prev*(1-k), k = 2/(n+1).
  RSI   Wilder: first average gain/loss is a simple mean of the first n changes, then
        avg = (prev*(n-1) + x) / n. RSI = 100 - 100/(1+avgGain/avgLoss); 100 if no losses
        (50 if the price never moved).
  ATR   Wilder: true range = max(H-L, |H-prevC|, |L-prevC|); first ATR is the mean of the first n
        true ranges (bars 1..n), then Wilder smoothing.
  MACD  line = EMA(12) - EMA(26); signal = EMA(9) of the line, seeded with the SMA of its first 9
        values; histogram = line - signal.
Small differences from a vendor come from the seed (the first values) and fade with more history.
"""


def sma(values, n):
    out = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    total = sum(values[:n])
    out[n - 1] = total / n
    for i in range(n, len(values)):
        total += values[i] - values[i - n]
        out[i] = total / n
    return out


def ema(values, n):
    out = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / n
    out[n - 1] = e
    for i in range(n, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def rsi(closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gain = loss = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gain += max(d, 0.0)
        loss += max(-d, 0.0)
    ag, al = gain / n, loss / n

    def val(ag, al):
        if al == 0:
            return 100.0 if ag > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + ag / al)

    out[n] = val(ag, al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = val(ag, al)
    return out


def true_range(highs, lows, closes):
    tr = [None] * len(closes)
    for i in range(1, len(closes)):
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    return tr


def atr(highs, lows, closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    tr = true_range(highs, lows, closes)
    a = sum(tr[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(closes)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def macd(closes, fast=12, slow=26, signal=9):
    """Returns (line, signal_line, histogram), each the same length as closes."""
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [None if (a is None or b is None) else a - b for a, b in zip(ef, es)]
    first = next((i for i, v in enumerate(line) if v is not None), None)
    sig = [None] * len(closes)
    if first is not None and len(closes) - first >= signal:
        tail = ema(line[first:], signal)
        sig[first:] = tail
    hist = [None if (a is None or b is None) else a - b for a, b in zip(line, sig)]
    return line, sig, hist


def latest(series):
    """The last value, or None if the series has no value yet."""
    return series[-1] if series else None


def compute_all(opens, highs, lows, closes):
    """The indicator set the gate expects, as {name: latest value or None}, plus macd extras."""
    line, sig, hist = macd(closes)
    return {
        "rsi14": latest(rsi(closes, 14)),
        "macd": latest(line), "macd_signal": latest(sig), "macd_hist": latest(hist),
        "atr14": latest(atr(highs, lows, closes, 14)),
        "ema20": latest(ema(closes, 20)),
        "ema50": latest(ema(closes, 50)),
        "sma200": latest(sma(closes, 200)),
    }
