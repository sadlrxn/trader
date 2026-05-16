"""Technical indicators for the trader -- ported from IBGA."""

from __future__ import annotations

import pandas as pd
import numpy as np


def atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    """Average True Range."""

    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index."""

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD line, signal line, and histogram."""

    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    line = ema_fast - ema_slow
    sig = line.ewm(span=signal, adjust=False).mean()
    hist = line - sig
    return line, sig, hist


def bollinger_bands(
    close: pd.Series,
    period: int = 20,
    std_dev: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Upper band, lower band, and %B."""

    sma = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = sma + std_dev * std
    lower = sma - std_dev * std
    return upper, lower, (close - lower) / (upper - lower)


def ema(close: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""

    return close.ewm(span=period, adjust=False).mean()


def compute_indicators(bars: pd.DataFrame) -> dict:
    """Compute all indicators from OHLCV bars. Returns dict of latest scalar values."""

    c, h, lo = bars["close"], bars["high"], bars["low"]
    atr_val = atr(h, lo, c)
    rsi_val = rsi(c)
    macd_line, macd_signal, macd_hist = macd(c)
    bb_upper, bb_lower, bb_pct = bollinger_bands(c)
    ema9 = ema(c, 9)
    ema20 = ema(c, 20)

    last = len(c) - 1
    return {
        "atr": float(atr_val.iloc[last]) if pd.notna(atr_val.iloc[last]) else None,
        "atr_pct": (
            float(atr_val.iloc[last] / c.iloc[last] * 100)
            if pd.notna(atr_val.iloc[last]) and c.iloc[last] > 0
            else None
        ),
        "rsi": float(rsi_val.iloc[last]) if pd.notna(rsi_val.iloc[last]) else None,
        "macd": float(macd_line.iloc[last]) if pd.notna(macd_line.iloc[last]) else None,
        "macd_signal": float(macd_signal.iloc[last])
        if pd.notna(macd_signal.iloc[last])
        else None,
        "macd_histogram": float(macd_hist.iloc[last])
        if pd.notna(macd_hist.iloc[last])
        else None,
        "bb_upper": float(bb_upper.iloc[last])
        if pd.notna(bb_upper.iloc[last])
        else None,
        "bb_lower": float(bb_lower.iloc[last])
        if pd.notna(bb_lower.iloc[last])
        else None,
        "bb_pct": float(bb_pct.iloc[last]) if pd.notna(bb_pct.iloc[last]) else None,
        "ema9": float(ema9.iloc[last]) if pd.notna(ema9.iloc[last]) else None,
        "ema20": float(ema20.iloc[last]) if pd.notna(ema20.iloc[last]) else None,
        "ema_crossover": (
            "bullish"
            if last >= 1
            and ema9.iloc[last - 1] <= ema20.iloc[last - 1]
            and ema9.iloc[last] > ema20.iloc[last]
            else (
                "bearish"
                if last >= 1
                and ema9.iloc[last - 1] >= ema20.iloc[last - 1]
                and ema9.iloc[last] < ema20.iloc[last]
                else "none"
            )
        ),
    }
