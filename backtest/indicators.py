"""Technical indicators used by the swing strategies.

All functions take/return pandas Series or DataFrames indexed by date and
are written so that a value at index t only uses information available
through t (no forward-looking). Callers are responsible for shifting when a
signal needs to be actionable at t+1 open (see strategies.py).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, window: int) -> pd.Series:
    return series.ewm(span=window, adjust=False, min_periods=window).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    out = out.where(avg_loss != 0, 100.0)  # no losses -> RSI 100
    return out


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = true_range(df)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def donchian_high(series: pd.Series, window: int) -> pd.Series:
    """Rolling high over the PRIOR `window` bars (excludes current bar)."""
    return series.shift(1).rolling(window=window, min_periods=window).max()


def rolling_avg_volume(volume: pd.Series, window: int) -> pd.Series:
    return volume.shift(1).rolling(window=window, min_periods=window).mean()
