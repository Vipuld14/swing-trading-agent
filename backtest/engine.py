"""Portfolio-level event-driven backtest for the swing strategies.

This is a research/validation tool, not the live execution path: the goal
is to check whether the trend-pullback / breakout rules have any edge before
wiring them into anything that touches a real account.

Known simplifications (documented, not hidden):
- Fills assume you get exactly the stop/target/open price touched that day
  (no partial fills; a gap through the stop is still filled at the stop
  price, which is optimistic).
- Mark-to-market equity used for sizing a day's new entries is computed once
  before that day's entries, so several same-day entries share one equity
  snapshot rather than perfectly re-marking after each one.
- Corporate actions (splits/dividends) rely on yfinance's auto-adjusted
  prices.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

import pandas as pd

from . import risk as risk_mod


@dataclass
class OpenPosition:
    ticker: str
    shares: int
    entry_price: float
    stop_price: float
    initial_stop_price: float  # never mutated; used for R-multiple even after a trailing stop ratchets stop_price up
    target_price: float
    entry_date: pd.Timestamp
    signal_type: str
    max_holding_days: int
    use_trailing_stop: bool = False
    trail_distance: float = 0.0
    trailing_activation_price: float = float("inf")
    highest_price: float = 0.0
    trailing_active: bool = False
    days_held: int = 0


@dataclass
class ClosedTrade:
    ticker: str
    signal_type: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    shares: int
    entry_price: float
    exit_price: float
    exit_reason: str
    pnl: float
    r_multiple: float


def _apply_slippage(price: float, bps: float, side: str) -> float:
    adj = price * (bps / 10_000.0)
    return price + adj if side == "buy" else price - adj


def _close_position(pos: OpenPosition, exit_date, exit_price: float, reason: str,
                     closed_trades: list, commission: float) -> None:
    pnl = (exit_price - pos.entry_price) * pos.shares - 2 * commission
    # Use the ORIGINAL stop distance, not the current (possibly ratcheted-up
    # by a trailing stop) stop_price, so R-multiples stay comparable across
    # trailing and non-trailing trades.
    risk_per_share = pos.entry_price - pos.initial_stop_price
    r_multiple = (exit_price - pos.entry_price) / risk_per_share if risk_per_share > 0 else 0.0
    closed_trades.append(
        ClosedTrade(
            ticker=pos.ticker,
            signal_type=pos.signal_type,
            entry_date=pos.entry_date,
            exit_date=exit_date,
            shares=pos.shares,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            exit_reason=reason,
            pnl=pnl,
            r_multiple=r_multiple,
        )
    )


def run_backtest(
    data: dict[str, pd.DataFrame],
    cfg: dict,
    market_uptrend: pd.Series | None = None,
    rs_returns: pd.DataFrame | None = None,
) -> dict:
    """market_uptrend and rs_returns are optional, cross-cutting overlays that
    apply regardless of which strategy produced a given day's signal:
    - market_uptrend gates/derisks NEW entries when the broader market isn't
      in a confirmed uptrend (risk.market_regime_overlay in the config).
    - rs_returns, when risk.prioritize_by_relative_strength is set, is used
      to rank same-day candidates so the strongest names fill the limited
      position slots first, instead of whichever ticker happens to appear
      first in the config's universe list.
    """
    bt_cfg = cfg["backtest"]
    risk_cfg = cfg["risk"]
    slippage_bps = bt_cfg.get("slippage_bps", 0)
    commission = bt_cfg.get("commission_per_trade", 0.0)
    overlay_cfg = risk_cfg.get("market_regime_overlay", {"enabled": False})
    prioritize_rs = risk_cfg.get("prioritize_by_relative_strength", False) and rs_returns is not None

    all_dates = sorted(set().union(*[df.index for df in data.values()]))
    if not all_dates:
        return {"equity_curve": pd.DataFrame(columns=["equity", "cash", "open_positions"]), "trades": pd.DataFrame(), "final_cash": float(bt_cfg["initial_capital"])}

    start = pd.Timestamp(bt_cfg["start_date"])
    end = pd.Timestamp(bt_cfg["end_date"]) if bt_cfg.get("end_date") else all_dates[-1]
    dates = [d for d in all_dates if start <= d <= end]
    if not dates:
        # Requested window doesn't overlap the data at all (e.g. an
        # out-of-sample period tested against synthetic data that doesn't
        # cover it) -- report this as "no result" rather than crashing.
        return {"equity_curve": pd.DataFrame(columns=["equity", "cash", "open_positions"]), "trades": pd.DataFrame(), "final_cash": float(bt_cfg["initial_capital"])}

    cash = float(bt_cfg["initial_capital"])
    open_positions: dict[str, OpenPosition] = {}
    closed_trades: list[ClosedTrade] = []
    equity_curve = []

    for i, date in enumerate(dates):
        if i == 0:
            continue  # need a prior day for signal lookback
        prev_date = dates[i - 1]

        # 1) time-based exits (checked at today's open)
        for ticker in list(open_positions):
            pos = open_positions[ticker]
            if ticker not in data or date not in data[ticker].index:
                continue
            pos.days_held += 1
            if pos.days_held >= pos.max_holding_days:
                bar = data[ticker].loc[date]
                exit_price = _apply_slippage(bar["open"], slippage_bps, "sell")
                _close_position(pos, date, exit_price, "time_stop", closed_trades, commission)
                cash += pos.shares * exit_price - commission
                del open_positions[ticker]

        # 2) trailing-stop ratchet, then stop / target exits, checked against
        # today's intraday range
        for ticker in list(open_positions):
            pos = open_positions[ticker]
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
                exit_reason = "trailing_stop" if pos.trailing_active else "stop"
                exit_price = _apply_slippage(pos.stop_price, slippage_bps, "sell")
                _close_position(pos, date, exit_price, exit_reason, closed_trades, commission)
                cash += pos.shares * exit_price - commission
                del open_positions[ticker]
            elif bar["high"] >= pos.target_price:
                exit_price = _apply_slippage(pos.target_price, slippage_bps, "sell")
                _close_position(pos, date, exit_price, "target", closed_trades, commission)
                cash += pos.shares * exit_price - commission
                del open_positions[ticker]

        # 3) mark-to-market equity snapshot, used to size any new entries today
        equity = cash
        for ticker, pos in open_positions.items():
            if ticker in data and date in data[ticker].index:
                equity += pos.shares * data[ticker].loc[date, "close"]
            else:
                equity += pos.shares * pos.entry_price

        # 4) new entries, from yesterday's close signal, filled at today's open.
        # Market-regime overlay applies to ALL strategies, checked once for
        # the day using yesterday's close (same timing convention as signals).
        market_ok = True
        if overlay_cfg.get("enabled") and market_uptrend is not None:
            market_ok = bool(market_uptrend.get(prev_date, True))

        skip_new_entries_today = overlay_cfg.get("enabled") and not market_ok and overlay_cfg.get("mode") == "block_entries"

        if not skip_new_entries_today and len(open_positions) < risk_cfg["max_concurrent_positions"]:
            # Gather every valid candidate first (no side effects yet), so we
            # can rank them before committing cash/slots to any of them.
            candidates = []
            for ticker, df in data.items():
                if ticker in open_positions:
                    continue
                if prev_date not in df.index or date not in df.index:
                    continue
                prev_bar = df.loc[prev_date]
                if not bool(prev_bar.get("signal", False)):
                    continue
                rs_score = float("-inf")
                if rs_returns is not None and ticker in rs_returns.columns and prev_date in rs_returns.index:
                    val = rs_returns.loc[prev_date, ticker]
                    rs_score = val if pd.notna(val) else float("-inf")
                candidates.append((ticker, df, prev_bar, rs_score))

            if prioritize_rs:
                candidates.sort(key=lambda c: c[3], reverse=True)

            trade_cfg = cfg
            if overlay_cfg.get("enabled") and not market_ok and overlay_cfg.get("mode") == "reduce_risk":
                trade_cfg = copy.deepcopy(cfg)
                trade_cfg["risk"]["risk_pct_per_trade"] *= overlay_cfg.get("risk_reduction_factor", 1.0)

            for ticker, df, prev_bar, _rs_score in candidates:
                if len(open_positions) >= risk_cfg["max_concurrent_positions"]:
                    break

                today_bar = df.loc[date]
                entry_price = _apply_slippage(today_bar["open"], slippage_bps, "buy")
                atr_value = prev_bar["atr"]
                signal_type = prev_bar.get("signal_type") or "unknown"
                risk_profile = risk_mod.resolve_risk_profile(trade_cfg, signal_type)

                plan = risk_mod.plan_trade(equity, entry_price, atr_value, trade_cfg, risk_profile)
                if plan is None:
                    continue
                cost = plan.shares * entry_price + commission
                if cost > cash:
                    continue

                cash -= cost
                open_positions[ticker] = OpenPosition(
                    ticker=ticker,
                    shares=plan.shares,
                    entry_price=entry_price,
                    stop_price=plan.stop_price,
                    initial_stop_price=plan.stop_price,
                    target_price=plan.target_price,
                    entry_date=date,
                    signal_type=signal_type,
                    max_holding_days=plan.max_holding_days,
                    use_trailing_stop=plan.use_trailing_stop,
                    trail_distance=plan.trail_distance,
                    trailing_activation_price=plan.trailing_activation_price,
                    highest_price=entry_price,
                )

        # recompute equity after entries for the curve
        equity = cash
        for ticker, pos in open_positions.items():
            if ticker in data and date in data[ticker].index:
                equity += pos.shares * data[ticker].loc[date, "close"]
            else:
                equity += pos.shares * pos.entry_price
        equity_curve.append(
            {"date": date, "equity": equity, "cash": cash, "open_positions": len(open_positions)}
        )

    # close anything still open at the end of the window, mark-to-market at last close
    final_date = dates[-1]
    for ticker, pos in list(open_positions.items()):
        if ticker in data and final_date in data[ticker].index:
            exit_price = data[ticker].loc[final_date, "close"]
        else:
            exit_price = pos.entry_price
        _close_position(pos, final_date, exit_price, "end_of_backtest", closed_trades, commission)
        cash += pos.shares * exit_price - commission
    open_positions.clear()

    equity_df = (
        pd.DataFrame(equity_curve).set_index("date")
        if equity_curve
        else pd.DataFrame(columns=["equity", "cash", "open_positions"])
    )
    trades_df = pd.DataFrame([t.__dict__ for t in closed_trades])

    return {"equity_curve": equity_df, "trades": trades_df, "final_cash": cash}
