"""Historical OHLCV data loading for the backtest engine.

Uses yfinance for real data (needs unrestricted internet access, e.g. your
own machine or a GitHub Actions runner). Results are cached to CSV under
data_cache/ so repeated backtest runs don't re-download.

A synthetic data generator is also provided so the rest of the pipeline can
be exercised/tested without network access.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd

CACHE_DIR = Path(__file__).resolve().parent.parent / "data_cache"


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df.columns = [str(c).lower() for c in df.columns]
    return df


def fetch_ohlcv(ticker: str, start: str, end: str | None = None, use_cache: bool = True) -> pd.DataFrame:
    """Fetch daily OHLCV for `ticker` between start/end (YYYY-MM-DD).

    Returns a DataFrame indexed by date with columns:
    open, high, low, close, volume
    """
    CACHE_DIR.mkdir(exist_ok=True)
    cache_path = CACHE_DIR / f"{ticker}_{start}_{end or 'latest'}.csv"

    if use_cache and cache_path.exists():
        df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        return df

    import yfinance as yf

    raw = yf.download(ticker, start=start, end=end, interval="1d", progress=False, auto_adjust=True)
    if raw.empty:
        raise ValueError(
            f"No data returned for {ticker}. Check the ticker symbol and that this "
            f"environment has internet access to Yahoo Finance."
        )
    df = _normalize_columns(raw)[["open", "high", "low", "close", "volume"]]
    df.index.name = "date"

    if use_cache:
        df.to_csv(cache_path)

    return df


def generate_synthetic_ohlcv(
    n_days: int = 750,
    start_price: float = 100.0,
    annual_drift: float = 0.08,
    annual_vol: float = 0.30,
    seed: int | None = None,
    start_date: str = "2021-01-01",
) -> pd.DataFrame:
    """Generate a plausible-looking daily OHLCV series via geometric Brownian
    motion, purely for smoke-testing the pipeline without network access.
    This is NOT a substitute for backtesting on real historical data.
    """
    rng = np.random.default_rng(seed)
    dt = 1 / 252
    daily_drift = annual_drift * dt
    daily_vol = annual_vol * np.sqrt(dt)

    shocks = rng.normal(loc=daily_drift, scale=daily_vol, size=n_days)
    close = start_price * np.exp(np.cumsum(shocks))

    open_ = np.empty(n_days)
    open_[0] = start_price
    open_[1:] = close[:-1]

    intraday_range = np.abs(rng.normal(loc=0.008, scale=0.006, size=n_days)) * close
    high = np.maximum(open_, close) + intraday_range
    low = np.minimum(open_, close) - intraday_range
    low = np.clip(low, a_min=0.01, a_max=None)

    base_volume = 5_000_000
    volume = np.abs(rng.normal(loc=base_volume, scale=base_volume * 0.3, size=n_days)).astype(int)

    dates = pd.bdate_range(start=start_date, periods=n_days)
    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=dates,
    )
    df.index.name = "date"
    return df
