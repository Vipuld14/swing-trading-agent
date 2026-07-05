"""A price/volume-only approximation of William O'Neil's CANSLIM method.

Honest scope: this captures the TECHNICAL half of CANSLIM --
- "N" (new highs / breakouts on volume)
- "L" (leader, via relative strength ranking against the universe)
- "M" (market direction -- only trade when the broader market is confirmed
  uptrend)

It does NOT implement "C", "A", "S", or "I" -- current/annual earnings
growth, sales growth, or institutional sponsorship -- because those need
point-in-time fundamentals data (what was actually reported and when, not
restated figures pulled later) which this project doesn't have a reliable
source for yet. Treat this as "does the technical half of CANSLIM's playbook
help our engine", not a real CANSLIM backtest.
"""
from __future__ import annotations

import pandas as pd

from . import indicators as ind


def compute_market_uptrend(market_df: pd.DataFrame, cfg: dict) -> pd.Series:
    """True on days the market proxy (e.g. SPY) is in a confirmed uptrend:
    fast SMA above slow SMA and price above the fast SMA -- the same
    trend definition used for individual stocks, applied to the market.
    """
    strat_cfg = cfg["strategy"]
    sma_fast = ind.sma(market_df["close"], strat_cfg["sma_fast"])
    sma_slow = ind.sma(market_df["close"], strat_cfg["sma_slow"])
    uptrend = (sma_fast > sma_slow) & (market_df["close"] > sma_fast)
    uptrend.name = "market_uptrend"
    return uptrend


def compute_relative_strength(raw_data: dict[str, pd.DataFrame], window: int) -> pd.DataFrame:
    """Trailing `window`-day price return for every ticker, aligned by date.
    This is the same idea as IBD's "RS Rating" -- how a stock has performed
    relative to its peers -- computed from price alone.
    """
    returns = {ticker: df["close"] / df["close"].shift(window) - 1.0 for ticker, df in raw_data.items()}
    return pd.DataFrame(returns)


def relative_strength_flags(rs_returns: pd.DataFrame, top_pct: float) -> pd.DataFrame:
    """Boolean frame: True where a ticker's trailing return is in the top
    `top_pct` of the universe on that date (a "leader" that day).
    Ranked cross-sectionally, so a ticker can drop in/out of leadership as
    the rest of the universe moves, not just based on its own price path.
    """
    pct_rank = rs_returns.rank(axis=1, pct=True, ascending=True)
    return pct_rank >= (1 - top_pct)


def canslim_lite_signal(df: pd.DataFrame, cfg: dict, market_uptrend: pd.Series, rs_flag: pd.Series) -> pd.Series:
    """Breakout on volume (same base rule as the plain breakout strategy),
    gated by: the stock being a relative-strength leader that day, AND the
    broader market being in a confirmed uptrend that day.
    """
    strat_cfg = cfg["strategy"]
    vol_multiple = strat_cfg["volume_breakout_multiple"]

    breakout = df["close"] > df["donchian_high"]
    volume_confirmed = df["volume"] > (df["avg_volume"] * vol_multiple)

    market_ok = market_uptrend.reindex(df.index).fillna(False)
    leader_ok = rs_flag.reindex(df.index).fillna(False)

    return breakout & volume_confirmed & market_ok & leader_ok
