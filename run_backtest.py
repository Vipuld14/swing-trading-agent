#!/usr/bin/env python3
"""CLI entry point for backtesting the swing trading strategies.

Examples:
    # Real data (needs unrestricted internet access to Yahoo Finance)
    python run_backtest.py --config config.yaml

    # Smoke-test the pipeline with synthetic data (no network needed)
    python run_backtest.py --config config.yaml --synthetic

    # Override a few settings from the CLI
    python run_backtest.py --config config.yaml --start 2020-01-01 --risk-pct 1.5
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

import yaml

from backtest import canslim_lite as canslim_mod
from backtest import data as data_mod
from backtest import metrics as metrics_mod
from backtest import strategies as strat_mod
from backtest.engine import run_backtest


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _seed_for(ticker: str) -> int:
    # Deterministic-but-varied per ticker, and stable across processes/machines.
    # Python's builtin hash() is randomized per-process (PYTHONHASHSEED) and
    # must not be used for seeding here.
    return int(hashlib.md5(ticker.encode()).hexdigest(), 16) % (2**32)


def fetch_one(ticker: str, start: str, end: str | None, synthetic: bool):
    if synthetic:
        return data_mod.generate_synthetic_ohlcv(n_days=900, seed=_seed_for(ticker), start_date="2020-01-01")
    return data_mod.fetch_ohlcv(ticker, start=start, end=end)


def build_dataset(cfg: dict, synthetic: bool) -> dict:
    """Returns {"data": {ticker: df}, "market_uptrend": pd.Series, "rs_returns": pd.DataFrame}.

    market_uptrend and rs_returns are ALWAYS computed (not just for
    canslim_lite) so the engine's market-regime overlay and relative-strength
    entry prioritization (risk.market_regime_overlay /
    risk.prioritize_by_relative_strength) work regardless of which entry
    strategy is active. They reuse strategy.canslim_lite's market_proxy /
    rs_window params as the general "market & leadership" settings.
    """
    start = cfg["backtest"]["start_date"]
    end = cfg["backtest"]["end_date"]

    raw = {ticker: fetch_one(ticker, start, end, synthetic) for ticker in cfg["universe"]}

    canslim_cfg = cfg["strategy"]["canslim_lite"]
    market_df = fetch_one(canslim_cfg["market_proxy"], start, end, synthetic)
    market_uptrend = canslim_mod.compute_market_uptrend(market_df, cfg)
    rs_returns = canslim_mod.compute_relative_strength(raw, window=canslim_cfg["rs_window"])

    rs_flags = None
    if cfg["strategy"]["active"] == "canslim_lite":
        rs_flags = canslim_mod.relative_strength_flags(rs_returns, top_pct=canslim_cfg["rs_top_pct"])

    data = {}
    for ticker, df in raw.items():
        if rs_flags is not None:
            data[ticker] = strat_mod.apply_strategies(df, cfg, market_uptrend=market_uptrend, rs_flag=rs_flags[ticker])
        else:
            data[ticker] = strat_mod.apply_strategies(df, cfg)

    return {"data": data, "market_uptrend": market_uptrend, "rs_returns": rs_returns}


def plot_equity_curve(equity_df, benchmark_summary, out_path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(equity_df.index, equity_df["equity"], label="Strategy")
    ax.set_title("Swing agent backtest — equity curve")
    ax.set_xlabel("Date")
    ax.set_ylabel("Equity ($)")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the swing trading agent's strategies.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--synthetic", action="store_true", help="Use generated data instead of Yahoo Finance (no network needed).")
    parser.add_argument("--start", default=None, help="Override backtest.start_date")
    parser.add_argument("--end", default=None, help="Override backtest.end_date")
    parser.add_argument("--risk-pct", type=float, default=None, help="Override risk.risk_pct_per_trade")
    parser.add_argument("--strategy", choices=["pullback", "breakout", "both", "canslim_lite"], default=None, help="Override strategy.active")
    parser.add_argument("--out-dir", default="backtest_output")
    args = parser.parse_args()

    cfg = load_config(args.config)
    cfg = copy.deepcopy(cfg)

    if args.start:
        cfg["backtest"]["start_date"] = args.start
    if args.end:
        cfg["backtest"]["end_date"] = args.end
    if args.risk_pct is not None:
        cfg["risk"]["risk_pct_per_trade"] = args.risk_pct
    if args.strategy:
        cfg["strategy"]["active"] = args.strategy

    out_dir = Path(args.out_dir)
    out_dir.mkdir(exist_ok=True)

    print(f"Loading data for {len(cfg['universe'])} tickers"
          f" ({'synthetic' if args.synthetic else 'yfinance'})...")
    built = build_dataset(cfg, synthetic=args.synthetic)
    data, market_uptrend, rs_returns = built["data"], built["market_uptrend"], built["rs_returns"]

    print("Running backtest...")
    result = run_backtest(data, cfg, market_uptrend=market_uptrend, rs_returns=rs_returns)

    summary = metrics_mod.summarize(result, cfg["backtest"]["initial_capital"])
    benchmark = metrics_mod.buy_and_hold_benchmark(data, cfg)

    print("\n=== Strategy performance ===")
    print(json.dumps(summary, indent=2, default=str))

    print("\n=== Buy-and-hold benchmark (equal-weight, same universe) ===")
    print(json.dumps(benchmark, indent=2, default=str))

    if not result["trades"].empty:
        trades_path = out_dir / "trades.csv"
        result["trades"].to_csv(trades_path, index=False)
        print(f"\nSaved trade log to {trades_path}")

    if not result["equity_curve"].empty:
        equity_path = out_dir / "equity_curve.csv"
        result["equity_curve"].to_csv(equity_path)
        plot_path = out_dir / "equity_curve.png"
        plot_equity_curve(result["equity_curve"], benchmark, plot_path)
        print(f"Saved equity curve to {equity_path} and {plot_path}")

    summary_path = out_dir / "summary.json"
    with open(summary_path, "w") as f:
        json.dump({"strategy": summary, "benchmark": benchmark}, f, indent=2, default=str)
    print(f"Saved summary to {summary_path}")


if __name__ == "__main__":
    main()
