#!/usr/bin/env python3
"""Run the backtest across several date windows and print a comparison table.

A single full-period backtest can be misleading if the universe happens to
contain a handful of outlier winners (e.g. NVDA/AMD during the 2023-2025 AI
rally) -- buy-and-hold on those specific names over that specific window is
an unusually high bar. This script re-runs the same strategy and benchmark
across multiple sub-periods (different market regimes: pre-COVID bull,
COVID crash/recovery, 2022 bear market, recent bull) so you can see whether
the signal has consistent, regime-independent edge rather than one lucky or
unlucky window.

Usage:
    python run_multi_period.py --config config_diversified.yaml
    python run_multi_period.py --config config.yaml   # original tech-heavy universe, for comparison
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

import pandas as pd

from backtest import metrics as metrics_mod
from backtest.engine import run_backtest
from run_backtest import build_dataset, load_config

# (label, start, end). end=None means "through the most recent available bar".
DEFAULT_PERIODS = [
    ("2018-2020 (pre-COVID bull + crash)", "2018-01-01", "2020-06-30"),
    ("2020-2022 (COVID recovery + 2022 bear)", "2020-07-01", "2022-12-31"),
    ("2023-present (recent bull)", "2023-01-01", None),
    ("Full range 2018-present", "2018-01-01", None),
]


def run_one_period(
    data: dict, base_cfg: dict, start: str, end: str | None, market_uptrend=None, rs_returns=None
) -> dict:
    cfg = copy.deepcopy(base_cfg)
    cfg["backtest"]["start_date"] = start
    cfg["backtest"]["end_date"] = end

    result = run_backtest(data, cfg, market_uptrend=market_uptrend, rs_returns=rs_returns)
    strategy = metrics_mod.summarize(result, cfg["backtest"]["initial_capital"])
    benchmark = metrics_mod.buy_and_hold_benchmark(data, cfg)
    return {"strategy": strategy, "benchmark": benchmark}


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest across multiple date windows for a fairer read on edge.")
    parser.add_argument("--config", default="config_diversified.yaml")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--out-dir", default="backtest_output")
    args = parser.parse_args()

    cfg = load_config(args.config)
    # Fetch the full outer window once; indicators need history before the
    # earliest test period for SMA200/ATR warmup, so this must NOT be
    # narrowed to any single period's start date.
    print(f"Loading data for {len(cfg['universe'])} tickers "
          f"({'synthetic' if args.synthetic else 'yfinance'}) from {cfg['backtest']['start_date']}...")
    built = build_dataset(cfg, synthetic=args.synthetic)
    data, market_uptrend, rs_returns = built["data"], built["market_uptrend"], built["rs_returns"]

    rows = []
    for label, start, end in DEFAULT_PERIODS:
        print(f"\nRunning: {label} ({start} to {end or 'latest'})...")
        outcome = run_one_period(data, cfg, start, end, market_uptrend=market_uptrend, rs_returns=rs_returns)
        s, b = outcome["strategy"], outcome["benchmark"]
        if "error" in s or "error" in b:
            print(f"  skipped: {s.get('error') or b.get('error')}")
            continue
        rows.append(
            {
                "period": label,
                "strategy_cagr_pct": s["cagr_pct"],
                "benchmark_cagr_pct": b["cagr_pct"],
                "strategy_sharpe": s["sharpe_approx"],
                "benchmark_sharpe": b["sharpe_approx"],
                "strategy_max_dd_pct": s["max_drawdown_pct"],
                "benchmark_max_dd_pct": b["max_drawdown_pct"],
                "strategy_win_rate_pct": s.get("win_rate_pct"),
                "strategy_profit_factor": s.get("profit_factor"),
                "num_trades": s["num_trades"],
            }
        )

    if not rows:
        print("No periods produced results -- check your date ranges and data.")
        return

    table = pd.DataFrame(rows).set_index("period")
    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 20)

    print("\n=== Comparison across periods ===")
    print(table.to_string())

    out_dir = Path(args.out_dir)
    out_dir.mkdir(exist_ok=True)
    csv_path = out_dir / "multi_period_comparison.csv"
    table.to_csv(csv_path)
    print(f"\nSaved comparison table to {csv_path}")

    beat_count = int((table["strategy_cagr_pct"] > table["benchmark_cagr_pct"]).sum())
    print(f"\nStrategy beat buy-and-hold CAGR in {beat_count}/{len(table)} periods.")
    beat_sharpe = int((table["strategy_sharpe"] > table["benchmark_sharpe"]).sum())
    print(f"Strategy beat buy-and-hold Sharpe in {beat_sharpe}/{len(table)} periods.")


if __name__ == "__main__":
    main()
