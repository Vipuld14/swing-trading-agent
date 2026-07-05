#!/usr/bin/env python3
"""Daily paper-trading step -- Phase 2 of ARCHITECTURE.md's roadmap.

Runs the EXACT SAME signal engine, risk profiles, and overlay logic already
validated in the backtest (backtest/strategies.py, backtest/risk.py,
backtest/canslim_lite.py), one real day at a time, against live data, and
logs what it would have done -- no broker connection, no credentials, no
real orders. The whole point is to compare these paper fills/timing against
the backtest's assumptions before this ever touches the Robinhood MCP
integration described in ARCHITECTURE.md.

Mirrors backtest/engine.py's per-day order intentionally (time exits, then
trailing-ratchet + stop/target exits, then an equity snapshot, then new
entries filled at today's open). If you change engine.py's logic, check
whether this needs the same change -- they are not shared code, by choice,
so the backtest stays a pure research tool with no state-file dependency.

Usage:
    python paper_trading/run_daily.py --config ../config_diversified.yaml
    python paper_trading/run_daily.py --config ../config_diversified.yaml --synthetic  # no network needed, for testing
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest import canslim_lite as canslim_mod
from backtest import risk as risk_mod
from backtest import strategies as strat_mod
from run_backtest import fetch_one

from paper_trading.state import PaperPosition, PaperState, PendingEntry, load_state, save_state
from paper_trading.execution import close_position


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def fetch_live_data(cfg: dict, synthetic: bool, lookback_days: int = 500):
    """Fetch enough trailing history for indicator warmup (SMA200, ATR,
    the RS window) plus the market proxy, and compute strategy signals.
    """
    end = pd.Timestamp.today().normalize()
    start = (end - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    raw = {ticker: fetch_one(ticker, start, None, synthetic) for ticker in cfg["universe"]}
    canslim_cfg = cfg["strategy"]["canslim_lite"]
    market_df = fetch_one(canslim_cfg["market_proxy"], start, None, synthetic)
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

    return data, market_uptrend, rs_returns


def _mark_to_market(state: PaperState, data: dict, date: pd.Timestamp) -> float:
    equity = state.cash
    for ticker, pos in state.open_positions.items():
        if ticker in data and date in data[ticker].index:
            equity += pos.shares * data[ticker].loc[date, "close"]
        else:
            equity += pos.shares * pos.entry_price
    return equity


def _mark_benchmark(state: PaperState, data: dict, date: pd.Timestamp) -> float:
    value = state.benchmark_cash
    for ticker, shares in state.benchmark_shares.items():
        if ticker in data and date in data[ticker].index:
            value += shares * data[ticker].loc[date, "close"]
    return value


def process_date(state: PaperState, data: dict, cfg: dict, date: pd.Timestamp, prev_date: pd.Timestamp | None,
                  market_uptrend, rs_returns, log: list[str]) -> None:
    risk_cfg = cfg["risk"]
    overlay_cfg = risk_cfg.get("market_regime_overlay", {"enabled": False})
    prioritize_rs = risk_cfg.get("prioritize_by_relative_strength", False)

    is_first_run = prev_date is None

    if not is_first_run:
        # 1) time-based exits, at today's open
        for ticker in list(state.open_positions):
            pos = state.open_positions[ticker]
            if ticker not in data or date not in data[ticker].index:
                continue
            pos.days_held += 1
            if pos.days_held >= pos.max_holding_days:
                bar = data[ticker].loc[date]
                close_position(state, pos, str(date.date()), bar["open"], "time_stop", log)
                del state.open_positions[ticker]

        # 2) trailing-stop ratchet, then stop / target exits
        for ticker in list(state.open_positions):
            pos = state.open_positions[ticker]
            if ticker not in data or date not in data[ticker].index:
                continue
            bar = data[ticker].loc[date]

            if pos.use_trailing_stop:
                pos.highest_price = max(pos.highest_price, bar["high"])
                if not pos.trailing_active and pos.highest_price >= pos.trailing_activation_price:
                    pos.trailing_active = True
                if pos.trailing_active:
                    new_stop = pos.highest_price - pos.trail_distance
                    if new_stop > pos.stop_price:
                        pos.stop_price = new_stop

            if bar["low"] <= pos.stop_price:
                reason = "trailing_stop" if pos.trailing_active else "stop"
                close_position(state, pos, str(date.date()), pos.stop_price, reason, log)
                del state.open_positions[ticker]
            elif bar["high"] >= pos.target_price:
                close_position(state, pos, str(date.date()), pos.target_price, "target", log)
                del state.open_positions[ticker]

    # 3) equity snapshot, used to size any fills today
    equity = _mark_to_market(state, data, date)

    # 4) fill pending entries (signaled on the previous processed date) at today's open
    if not is_first_run and state.pending_entries and len(state.open_positions) < risk_cfg["max_concurrent_positions"]:
        market_ok = True
        if overlay_cfg.get("enabled") and market_uptrend is not None:
            market_ok = bool(market_uptrend.get(prev_date, True))
        skip_entries = overlay_cfg.get("enabled") and not market_ok and overlay_cfg.get("mode") == "block_entries"

        if not skip_entries:
            candidates = []
            for ticker, pending in state.pending_entries.items():
                if ticker in state.open_positions or ticker not in data or date not in data[ticker].index:
                    continue
                rs_score = float("-inf")
                if rs_returns is not None and ticker in rs_returns.columns and prev_date in rs_returns.index:
                    val = rs_returns.loc[prev_date, ticker]
                    rs_score = val if pd.notna(val) else float("-inf")
                candidates.append((ticker, pending, rs_score))

            if prioritize_rs:
                candidates.sort(key=lambda c: c[2], reverse=True)

            trade_cfg = cfg
            if overlay_cfg.get("enabled") and not market_ok and overlay_cfg.get("mode") == "reduce_risk":
                import copy
                trade_cfg = copy.deepcopy(cfg)
                trade_cfg["risk"]["risk_pct_per_trade"] *= overlay_cfg.get("risk_reduction_factor", 1.0)

            for ticker, pending, _rs_score in candidates:
                if len(state.open_positions) >= risk_cfg["max_concurrent_positions"]:
                    break
                entry_price = data[ticker].loc[date, "open"]
                risk_profile = risk_mod.resolve_risk_profile(trade_cfg, pending.signal_type)
                plan = risk_mod.plan_trade(equity, entry_price, pending.atr_at_signal, trade_cfg, risk_profile)
                if plan is None:
                    continue
                cost = plan.shares * entry_price
                if cost > state.cash:
                    continue
                state.cash -= cost
                state.open_positions[ticker] = PaperPosition(
                    ticker=ticker,
                    shares=plan.shares,
                    entry_price=entry_price,
                    entry_date=str(date.date()),
                    stop_price=plan.stop_price,
                    initial_stop_price=plan.stop_price,
                    target_price=plan.target_price,
                    signal_type=pending.signal_type,
                    max_holding_days=plan.max_holding_days,
                    use_trailing_stop=plan.use_trailing_stop,
                    trail_distance=plan.trail_distance,
                    trailing_activation_price=plan.trailing_activation_price,
                    highest_price=entry_price,
                )
                log.append(f"  ENTER {ticker}: {plan.shares} sh @ ${entry_price:.2f} ({pending.signal_type}), "
                            f"stop ${plan.stop_price:.2f}, target "
                            f"{'trailing' if plan.use_trailing_stop else f'${plan.target_price:.2f}'}")

    # 5) initialize the benchmark the first time it hasn't been seeded yet
    # (self-healing: keyed off "no benchmark shares recorded" rather than
    # is_first_run, so a state file that somehow reaches here without a
    # benchmark still gets one instead of reporting $0 forever).
    if not state.benchmark_shares:
        per_ticker = state.cash / len(data)
        for ticker, df in data.items():
            if date in df.index and df.loc[date, "close"] > 0:
                state.benchmark_shares[ticker] = per_ticker / df.loc[date, "close"]
        state.benchmark_cash = state.cash - sum(
            shares * data[t].loc[date, "close"] for t, shares in state.benchmark_shares.items() if date in data[t].index
        )

    # 6) today's signals become tomorrow's pending entries (fully replaces the old set)
    new_pending = {}
    for ticker, df in data.items():
        if date not in df.index:
            continue
        bar = df.loc[date]
        if bool(bar.get("signal", False)):
            new_pending[ticker] = PendingEntry(
                ticker=ticker,
                signal_date=str(date.date()),
                signal_type=bar.get("signal_type") or "unknown",
                atr_at_signal=None if pd.isna(bar["atr"]) else float(bar["atr"]),
            )
    state.pending_entries = new_pending

    # 7) record equity history (strategy + benchmark) and advance the checkpoint
    strategy_equity = _mark_to_market(state, data, date)
    benchmark_equity = _mark_benchmark(state, data, date)
    state.equity_history.append(
        {"date": str(date.date()), "strategy_equity": strategy_equity, "benchmark_equity": benchmark_equity}
    )
    state.last_run_date = str(date.date())


def write_digest(path: Path, state: PaperState, log: list[str]) -> None:
    lines = [f"# Paper trading digest -- {state.last_run_date}", ""]
    if log:
        lines.append("## Today's activity")
        lines.extend(log)
        lines.append("")
    else:
        lines.append("## Today's activity\n  (no exits or entries)\n")

    lines.append("## Open positions")
    if state.open_positions:
        for ticker, pos in state.open_positions.items():
            lines.append(
                f"  {ticker}: {pos.shares} sh @ ${pos.entry_price:.2f} ({pos.signal_type}), "
                f"stop ${pos.stop_price:.2f}, held {pos.days_held}/{pos.max_holding_days} days"
            )
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("## Watching for tomorrow's open")
    if state.pending_entries:
        for ticker, pending in state.pending_entries.items():
            lines.append(f"  {ticker} ({pending.signal_type})")
    else:
        lines.append("  (no new signals today)")
    lines.append("")

    if state.equity_history:
        latest = state.equity_history[-1]
        strat_ret = (latest["strategy_equity"] / state.initial_capital - 1) * 100
        bench_ret = (latest["benchmark_equity"] / state.initial_capital - 1) * 100
        lines.append("## Running P&L since paper trading started")
        lines.append(f"  Strategy:  ${latest['strategy_equity']:.2f} ({strat_ret:+.2f}%)")
        lines.append(f"  Benchmark: ${latest['benchmark_equity']:.2f} ({bench_ret:+.2f}%)  (equal-weight buy-and-hold)")
        lines.append(f"  Cash: ${state.cash:.2f}")
        lines.append(f"  Closed trades so far: {len(state.closed_trades)}")

    path.write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one (or more, if catching up) day(s) of paper trading.")
    parser.add_argument("--config", default="config_diversified.yaml")
    parser.add_argument("--synthetic", action="store_true", help="No network needed -- for testing this script itself.")
    parser.add_argument("--state-file", default="paper_trading/state.json")
    parser.add_argument("--digest-file", default="paper_trading/digest.md")
    parser.add_argument("--as-of", default=None,
                         help="Test/backfill hook: pretend today is this date, ignoring anything after it. "
                              "Lets a catch-up run and a sequence of day-by-day runs be compared for parity.")
    args = parser.parse_args()

    cfg = load_config(args.config)
    state_path = Path(args.state_file)
    digest_path = Path(args.digest_file)

    state = load_state(state_path, initial_capital=cfg["backtest"]["initial_capital"])

    print(f"Fetching live data for {len(cfg['universe'])} tickers "
          f"({'synthetic' if args.synthetic else 'yfinance'})...")
    data, market_uptrend, rs_returns = fetch_live_data(cfg, args.synthetic)

    if args.as_of:
        cutoff = pd.Timestamp(args.as_of)
        data = {t: df[df.index <= cutoff] for t, df in data.items()}
        market_uptrend = market_uptrend[market_uptrend.index <= cutoff] if market_uptrend is not None else None
        rs_returns = rs_returns[rs_returns.index <= cutoff] if rs_returns is not None else None

    all_dates = sorted(set().union(*[df.index for df in data.values()]))
    if state.last_run_date is None:
        dates_to_process = all_dates[-1:]  # first run: just seed from the latest available day
    else:
        last_run = pd.Timestamp(state.last_run_date)
        dates_to_process = [d for d in all_dates if d > last_run]

    if not dates_to_process:
        print(f"Already up to date as of {state.last_run_date}. Nothing to do.")
        return

    print(f"Processing {len(dates_to_process)} new trading day(s): "
          f"{dates_to_process[0].date()} through {dates_to_process[-1].date()}")

    full_log: list[str] = []
    for date in dates_to_process:
        idx = all_dates.index(date)
        prev_date = all_dates[idx - 1] if idx > 0 and state.last_run_date is not None else None
        day_log: list[str] = []
        process_date(state, data, cfg, date, prev_date, market_uptrend, rs_returns, day_log)
        if day_log:
            full_log.append(f"[{date.date()}]")
            full_log.extend(day_log)

    save_state(state_path, state)
    write_digest(digest_path, state, full_log)

    print(f"Saved state to {state_path} and digest to {digest_path}")
    print(f"As of {state.last_run_date}: {len(state.open_positions)} open position(s), "
          f"{len(state.pending_entries)} pending entr{'y' if len(state.pending_entries) == 1 else 'ies'} for next run.")


if __name__ == "__main__":
    main()
