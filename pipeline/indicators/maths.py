#!/usr/bin/env python3
"""
maths.py - pure, zero-dependency financial indicator mathematics.

Standard library only. All functions operate on standard Python lists of floats/ints.
Implementations follow standard technical analysis literature (Wilder, Murphy).

Indicators implemented:
- SMA: Simple Moving Average
- EMA: Exponential Moving Average (standard alpha = 2 / (N + 1))
- RSI: Wilder's Relative Strength Index (period 14 standard)
- MACD: Moving Average Convergence Divergence (12, 26, 9)
- ATR: Wilder's Average True Range (period 14 standard)
- Breakout: N-bar breakout and price channel levels (e.g. 20-day, 52-week)
"""
from typing import List, Optional, Dict, Any


def sma_series(values: List[float], period: int) -> List[Optional[float]]:
    """Compute Simple Moving Average series. Returns None for indices < period - 1."""
    if period <= 0:
        raise ValueError(f"period must be positive, got {period}")
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if n < period:
        return out
    
    # Running window sum
    window_sum = sum(values[:period])
    out[period - 1] = window_sum / period
    for i in range(period, n):
        window_sum += values[i] - values[i - period]
        out[i] = window_sum / period
    return out


def sma(values: List[float], period: int) -> Optional[float]:
    """Compute Simple Moving Average of the latest bar."""
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / float(period)


def ema_series(values: List[float], period: int, alpha: Optional[float] = None) -> List[Optional[float]]:
    """
    Compute Exponential Moving Average series.
    Initialised with arithmetic mean of first `period` values.
    Returns None for indices < period - 1.
    """
    if period <= 0:
        raise ValueError(f"period must be positive, got {period}")
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if n < period:
        return out
    
    if alpha is None:
        alpha = 2.0 / (period + 1.0)
    
    # Initial SMA seed
    current_ema = sum(values[:period]) / float(period)
    out[period - 1] = current_ema
    
    for i in range(period, n):
        current_ema = alpha * values[i] + (1.0 - alpha) * current_ema
        out[i] = current_ema
    return out


def ema(values: List[float], period: int, alpha: Optional[float] = None) -> Optional[float]:
    """Compute Exponential Moving Average of the latest bar."""
    series = ema_series(values, period, alpha)
    return series[-1] if series else None


def rsi_series(closes: List[float], period: int = 14) -> List[Optional[float]]:
    """
    Compute Wilder's Relative Strength Index (RSI) series.
    Uses Wilder's smoothing:
      Initial avg gain/loss = arithmetic mean of first 14 changes.
      Subsequent avg = (prev_avg * 13 + current) / 14.
    Returns None for indices < period.
    """
    if period <= 0:
        raise ValueError(f"period must be positive, got {period}")
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n <= period:
        return out

    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        change = closes[i] - closes[i - 1]
        if change > 0:
            gains[i] = change
        elif change < 0:
            losses[i] = -change

    # First average (arithmetic mean over bars 1..period)
    avg_gain = sum(gains[1:period + 1]) / float(period)
    avg_loss = sum(losses[1:period + 1]) / float(period)

    def _calc_rsi(ag: float, al: float) -> float:
        if al == 0.0:
            return 100.0 if ag > 0.0 else 50.0
        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = _calc_rsi(avg_gain, avg_loss)

    # Subsequent bars use Wilder's smoothing
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / float(period)
        avg_loss = (avg_loss * (period - 1) + losses[i]) / float(period)
        out[i] = _calc_rsi(avg_gain, avg_loss)

    return out


def rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """Compute Wilder's RSI for the latest bar."""
    series = rsi_series(closes, period)
    return series[-1] if series else None


def macd(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Optional[Dict[str, float]]:
    """
    Compute Moving Average Convergence Divergence (MACD).
    MACD Line   = EMA(fast) - EMA(slow)
    Signal Line = EMA(MACD Line, signal)
    Histogram   = MACD Line - Signal Line
    """
    if len(closes) < slow + signal or fast >= slow or fast <= 0 or signal <= 0:
        return None

    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)

    # Compute MACD line where both fast and slow are available (starting at slow - 1)
    macd_line: List[float] = []
    macd_indices: List[int] = []
    for i in range(slow - 1, len(closes)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_line.append(ema_fast[i] - ema_slow[i])
            macd_indices.append(i)

    if len(macd_line) < signal:
        return None

    signal_line_series = ema_series(macd_line, signal)
    latest_macd = macd_line[-1]
    latest_signal = signal_line_series[-1]

    if latest_signal is None:
        return None

    return {
        "macd": round(latest_macd, 4),
        "signal": round(latest_signal, 4),
        "hist": round(latest_macd - latest_signal, 4),
    }


def true_range_series(highs: List[float], lows: List[float], closes: List[float]) -> List[float]:
    """Compute True Range for each bar."""
    n = min(len(highs), len(lows), len(closes))
    if n == 0:
        return []
    tr = [highs[0] - lows[0]]
    for i in range(1, n):
        h, l, prev_c = highs[i], lows[i], closes[i - 1]
        tr.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    return tr


def atr_series(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[Optional[float]]:
    """Compute Wilder's Average True Range (ATR) series."""
    if period <= 0:
        raise ValueError(f"period must be positive, got {period}")
    n = min(len(highs), len(lows), len(closes))
    out: List[Optional[float]] = [None] * n
    if n < period:
        return out

    tr = true_range_series(highs, lows, closes)
    # First ATR is arithmetic mean of first `period` TRs
    curr_atr = sum(tr[:period]) / float(period)
    out[period - 1] = curr_atr

    for i in range(period, n):
        curr_atr = (curr_atr * (period - 1) + tr[i]) / float(period)
        out[i] = curr_atr
    return out


def atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    """Compute Wilder's ATR of the latest bar."""
    series = atr_series(highs, lows, closes, period)
    return round(series[-1], 4) if (series and series[-1] is not None) else None


def breakout(highs: List[float], lows: List[float], closes: List[float], period: int = 20) -> Optional[Dict[str, Any]]:
    """
    Compute price breakout metrics over previous `period` bars (excluding latest bar).
    Returns None if fewer than period + 1 bars are available.
    """
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1 or period <= 0:
        return None

    # Previous N bars (excluding the current bar at index -1)
    prev_highs = highs[-(period + 1):-1]
    prev_lows = lows[-(period + 1):-1]

    high_n = max(prev_highs)
    low_n = min(prev_lows)
    current_close = closes[-1]
    current_high = highs[-1]

    pct_from_high = ((current_close - high_n) / high_n) * 100.0 if high_n > 0 else 0.0
    pct_from_low = ((current_close - low_n) / low_n) * 100.0 if low_n > 0 else 0.0

    return {
        "period": period,
        "high_n": round(high_n, 2),
        "low_n": round(low_n, 2),
        "current_close": round(current_close, 2),
        "is_breakout_high": current_close > high_n,
        "is_intraday_breakout_high": current_high > high_n,
        "is_breakdown_low": current_close < low_n,
        "pct_from_high": round(pct_from_high, 2),
        "pct_from_low": round(pct_from_low, 2),
    }

