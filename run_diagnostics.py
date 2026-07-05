#!/usr/bin/env python3
"""Diagnostic breakdown of the swing strategy.

Three questions this answers:
1. Which leg is actually driving performance -- the trend-pullback rule, the
   breakout rule, or only their combination? A strategy that only "works"
   when blended can be hiding one leg propping up (or dragging down) the
   other. (No market-regime overlay or RS prioritization here -- this is a
   pure signal-quality comparison.)
2. How much of the result is the ~5bps slippage assumption costing, vs. a
   frictionless fill? If the edge disappears at realistic costs, it isn't
   a real edge yet.
3. Does the market-regime overlay + relative-strength prioritization
   actually improve the "both" strategy, or just add complexity? Runs
   "both" with the overlay off (baseline) vs on.
4. Do trailing stops improve on a fixed reward:risk target -- tested across
   the four in-sample regimes AND the 2009-2017 window that has never been
   used to tune anything in this project, applying the same
   out-of-sample discipline that already caught one overfit-looking result
   in this project (see ARCHITECTURE.md / conversation history).

Usage:
    python run_diagnostics.py --config config_diversified.yaml
    python run_diagnostics.py --config config_diversified.yaml --synthetic
"""
from __future__ import annotations

import argparse
import copy
from pathlib import Path

import pandas as pd

from backtest import canslim_lite as canslim_mod
from backtest import metrics as metrics_mod
from backtest import strategies as strat_mod
from backtest.engine import run_backtest
from run_backtest import fetch_one, load_config
from run_multi_period import DEFAULT_PERIODS

# Never used to tune any parameter in this project -- a genuine holdout for
# Diagnostic 4. Covers the 2008-2009 crisis recovery, the 2011 European debt
# crisis, and the 2015-2016 oil/China slowdown: different regimes than any
# of DEFAULT_PERIODS above.
OUT_OF_SAMPLE_PERIOD = ("2009-2017 (out-of-sample, never tuned on)", "2009-01-01", "2018-01-01")


def fetch_raw(cfg: dict, synthetic: bool) -> dict[str, pd.DataFrame]:
    """Fetch/generate raw OHLCV once, before any strategy signals are applied,
    so re-running with a different strategy leg doesn't re-download data.
    """
    start = cfg["backtest"]["start_date"]
    end = cfg["backtest"]["end_date"]
    return {ticker: fetch_one(ticker, start, end, synthetic) for ticker in cfg["universe"]}


def build_for_strategy(raw: dict[str, pd.DataFrame], cfg: dict, active: str, synthetic: bool) -> dict[str, pd.DataFrame]:
    cfg = copy.deepcopy(cfg)
    cfg["strategy"]["active"] = active

    if active != "canslim_lite":
        return {ticker: strat_mod.apply_strategies(df, cfg) for ticker, df in raw.items()}

    canslim_cfg = cfg["strategy"]["canslim_lite"]
    start = cfg["backtest"]["start_date"]
    end = cfg["backtest"]["end_date"]
    market_df = fetch_one(canslim_cfg["market_proxy"], start, end, synthetic)
    market_uptrend = canslim_mod.compute_market_uptrend(market_df, cfg)
    rs_returns = canslim_mod.compute_relative_strength(raw, window=canslim_cfg["rs_window"])
    rs_flags = canslim_mod.relative_strength_flags(rs_returns, top_pct=canslim_cfg["rs_top_pct"])

    return {
        ticker: strat_mod.apply_strategies(df, cfg, market_uptrend=market_uptrend, rs_flag=rs_flags[ticker])
        for ticker, df in raw.items()
    }


def run_row(
    data: dict,
    cfg: dict,
    start: str,
    end: str | None,
    slippage_bps: float | None = None,
    market_uptrend=None,
    rs_returns=None,
) -> dict:
    cfg = copy.deepcopy(cfg)
    cfg["backtest"]["start_date"] = start
    cfg["backtest"]["end_date"] = end
    if slippage_bps is not None:
        cfg["backtest"]["slippage_bps"] = slippage_bps
    result = run_backtest(data, cfg, market_uptrend=market_uptrend, rs_returns=rs_returns)
    return metrics_mod.summarize(result, cfg["backtest"]["initial_capital"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose which strategy leg drives performance and how much slippage costs.")
    parser.add_argument("--config", default="config_diversified.yaml")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--out-dir", default="backtest_output")
    args = parser.parse_args()

    cfg = load_config(args.config)
    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 20)

    # Widen the raw fetch to cover Diagnostic 4's out-of-sample window (with
    # buffer for SMA200 warmup before 2009). Harmless for Diagnostics 1-3:
    # extra history before what they need doesn't change indicator values
    # for the dates they actually evaluate.
    fetch_cfg = copy.deepcopy(cfg)
    fetch_cfg["backtest"]["start_date"] = min(cfg["backtest"]["start_date"], "2008-06-01")
    print(f"Loading raw data for {len(cfg['universe'])} tickers "
          f"({'synthetic' if args.synthetic else 'yfinance'}) from {fetch_cfg['backtest']['start_date']}...")
    raw = fetch_raw(fetch_cfg, args.synthetic)

    print("\n=== Diagnostic 1: which leg drives performance (pullback vs breakout vs both vs canslim_lite) ===")
    leg_rows = []
    for active in ["pullback", "breakout", "both", "canslim_lite"]:
        data = build_for_strategy(raw, cfg, active, args.synthetic)
        for label, start, end in DEFAULT_PERIODS:
            s = run_row(data, cfg, start, end)
            if "error" in s:
                continue
            leg_rows.append(
                {
                    "period": label,
                    "strategy_leg": active,
                    "cagr_pct": s["cagr_pct"],
                    "sharpe": s["sharpe_approx"],
                    "max_dd_pct": s["max_drawdown_pct"],
                    "win_rate_pct": s.get("win_rate_pct"),
                    "profit_factor": s.get("profit_factor"),
                    "num_trades": s["num_trades"],
                }
            )
    leg_table = pd.DataFrame(leg_rows).set_index(["period", "strategy_leg"]).sort_index()
    print(leg_table.to_string())

    print("\n=== Diagnostic 2: how much is the slippage assumption costing (assumed bps vs frictionless) ===")
    data_both = build_for_strategy(raw, cfg, "both", args.synthetic)
    assumed_slippage = cfg["backtest"].get("slippage_bps", 5)
    slip_rows = []
    for slippage in sorted({assumed_slippage, 0}, reverse=True):
        for label, start, end in DEFAULT_PERIODS:
            s = run_row(data_both, cfg, start, end, slippage_bps=slippage)
            if "error" in s:
                continue
            slip_rows.append(
                {
                    "period": label,
                    "slippage_bps": slippage,
                    "cagr_pct": s["cagr_pct"],
                    "sharpe": s["sharpe_approx"],
                    "profit_factor": s.get("profit_factor"),
                }
            )
    slip_table = pd.DataFrame(slip_rows).set_index(["period", "slippage_bps"]).sort_index()
    print(slip_table.to_string())

    print("\n=== Diagnostic 3: does the market-regime overlay + RS prioritization help 'both'? ===")
    canslim_cfg = cfg["strategy"]["canslim_lite"]
    market_df = fetch_one(canslim_cfg["market_proxy"], cfg["backtest"]["start_date"], cfg["backtest"]["end_date"], args.synthetic)
    market_uptrend = canslim_mod.compute_market_uptrend(market_df, cfg)
    rs_returns = canslim_mod.compute_relative_strength(raw, window=canslim_cfg["rs_window"])

    overlay_rows = []
    for overlay_on in [False, True]:
        overlay_cfg = copy.deepcopy(cfg)
        overlay_cfg["risk"]["market_regime_overlay"]["enabled"] = overlay_on
        overlay_cfg["risk"]["prioritize_by_relative_strength"] = overlay_on
        for label, start, end in DEFAULT_PERIODS:
            s = run_row(data_both, overlay_cfg, start, end, market_uptrend=market_uptrend, rs_returns=rs_returns)
            if "error" in s:
                continue
            overlay_rows.append(
                {
                    "period": label,
                    "overlay": "on" if overlay_on else "off (baseline)",
                    "cagr_pct": s["cagr_pct"],
                    "sharpe": s["sharpe_approx"],
                    "max_dd_pct": s["max_drawdown_pct"],
                    "profit_factor": s.get("profit_factor"),
                    "num_trades": s["num_trades"],
                }
            )
    overlay_table = pd.DataFrame(overlay_rows).set_index(["period", "overlay"]).sort_index()
    print(overlay_table.to_string())

    print("\n=== Diagnostic 4: do trailing stops improve on a fixed reward:risk target? ===")
    print("(includes the 2009-2017 out-of-sample window -- never used to tune anything in this project)")
    trail_rows = []
    for trailing_on in [False, True]:
        trail_cfg = copy.deepcopy(cfg)
        trail_cfg["risk"]["profiles"]["default"]["use_trailing_stop"] = trailing_on
        for label, start, end in DEFAULT_PERIODS + [OUT_OF_SAMPLE_PERIOD]:
            s = run_row(data_both, trail_cfg, start, end, market_uptrend=market_uptrend, rs_returns=rs_returns)
            if "error" in s:
                continue
            trail_rows.append(
                {
                    "period": label,
                    "trailing_stop": "on" if trailing_on else "off (fixed target, baseline)",
                    "cagr_pct": s["cagr_pct"],
                    "sharpe": s["sharpe_approx"],
                    "max_dd_pct": s["max_drawdown_pct"],
                    "profit_factor": s.get("profit_factor"),
                    "num_trades": s["num_trades"],
                }
            )
    trail_table = pd.DataFrame(trail_rows).set_index(["period", "trailing_stop"]).sort_index()
    print(trail_table.to_string())

    out_dir = Path(args.out_dir)
    out_dir.mkdir(exist_ok=True)
    leg_table.to_csv(out_dir / "diagnostic_strategy_legs.csv")
    slip_table.to_csv(out_dir / "diagnostic_slippage.csv")
    overlay_table.to_csv(out_dir / "diagnostic_overlay.csv")
    trail_table.to_csv(out_dir / "diagnostic_trailing_stop.csv")
    print(f"\nSaved diagnostic tables to {out_dir}/diagnostic_strategy_legs.csv, "
          f"{out_dir}/diagnostic_slippage.csv, {out_dir}/diagnostic_overlay.csv, "
          f"and {out_dir}/diagnostic_trailing_stop.csv")


if __name__ == "__main__":
    main()
