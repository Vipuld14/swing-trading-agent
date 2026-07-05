"""Swing trading entry signal rules.

Each strategy function takes a per-ticker OHLCV DataFrame and a config dict,
and returns the same DataFrame with an added boolean `signal` column and a
`signal_type` column. Signals are computed using only data through the close
of day t; the backtest engine is responsible for executing at day t+1's open
so there is no lookahead bias.
"""
from __future__ import annotations

import pandas as pd

from . import canslim_lite as canslim_mod
from . import indicators as ind


def add_common_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    df = df.copy()
    strat_cfg = cfg["strategy"]

    df["sma_fast"] = ind.sma(df["close"], strat_cfg["sma_fast"])
    df["sma_slow"] = ind.sma(df["close"], strat_cfg["sma_slow"])
    df["rsi"] = ind.rsi(df["close"], strat_cfg["rsi_period"])
    df["atr"] = ind.atr(df, 14)
    df["donchian_high"] = ind.donchian_high(df["high"], strat_cfg["donchian_window"])
    df["avg_volume"] = ind.rolling_avg_volume(df["volume"], strat_cfg["volume_avg_window"])
    return df


def trend_pullback_signals(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """Uptrend (fast SMA > slow SMA, price above fast SMA) plus an RSI dip
    below the pullback level that then recovers back above it.
    """
    strat_cfg = cfg["strategy"]
    level = strat_cfg["rsi_pullback_level"]

    uptrend = (df["sma_fast"] > df["sma_slow"]) & (df["close"] > df["sma_fast"])
    rsi_recovered = (df["rsi"] >= level) & (df["rsi"].shift(1) < level)

    return uptrend & rsi_recovered


def breakout_signals(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """Close breaks above the prior N-day high on above-average volume."""
    strat_cfg = cfg["strategy"]
    vol_multiple = strat_cfg["volume_breakout_multiple"]

    breakout = df["close"] > df["donchian_high"]
    volume_confirmed = df["volume"] > (df["avg_volume"] * vol_multiple)

    return breakout & volume_confirmed


def apply_strategies(
    df: pd.DataFrame,
    cfg: dict,
    market_uptrend: pd.Series | None = None,
    rs_flag: pd.Series | None = None,
) -> pd.DataFrame:
    df = add_common_indicators(df, cfg)
    active = cfg["strategy"]["active"]

    pullback = trend_pullback_signals(df, cfg) if active in ("pullback", "both") else pd.Series(False, index=df.index)
    breakout = breakout_signals(df, cfg) if active in ("breakout", "both") else pd.Series(False, index=df.index)

    if active == "canslim_lite":
        if market_uptrend is None or rs_flag is None:
            raise ValueError("canslim_lite requires market_uptrend and rs_flag (see run_backtest.build_dataset)")
        canslim = canslim_mod.canslim_lite_signal(df, cfg, market_uptrend, rs_flag)
    else:
        canslim = pd.Series(False, index=df.index)

    df["signal_pullback"] = pullback
    df["signal_breakout"] = breakout
    df["signal_canslim_lite"] = canslim
    df["signal"] = pullback | breakout | canslim
    df["signal_type"] = None
    df.loc[pullback, "signal_type"] = "pullback"
    df.loc[breakout & ~pullback, "signal_type"] = "breakout"
    df.loc[canslim, "signal_type"] = "canslim_lite"

    return df
