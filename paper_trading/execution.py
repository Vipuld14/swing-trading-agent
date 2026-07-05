"""Shared position-closing logic for the paper-trading scripts.

Both run_daily.py (once-daily, full-day-bar exits) and check_intraday.py
(several-times-a-day, single-price exits) need to close a position the same
way -- same P&L math, same R-multiple convention, same ClosedTrade record --
so that trade log entries are comparable regardless of which script caught
the exit. Pulled out here instead of duplicated.
"""
from __future__ import annotations

from paper_trading.state import ClosedTrade, PaperPosition, PaperState


def close_position(
    state: PaperState,
    pos: PaperPosition,
    exit_date: str,
    exit_price: float,
    reason: str,
    log: list[str],
) -> None:
    pnl = (exit_price - pos.entry_price) * pos.shares
    # Same convention as backtest/engine.py: R-multiple uses the ORIGINAL
    # stop distance, not a trailing-ratcheted one, so trades stay comparable.
    risk_per_share = pos.entry_price - pos.initial_stop_price
    r_multiple = (exit_price - pos.entry_price) / risk_per_share if risk_per_share > 0 else 0.0
    state.cash += pos.shares * exit_price
    state.closed_trades.append(
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
    log.append(
        f"  EXIT {pos.ticker}: {pos.shares} sh @ ${exit_price:.2f} ({reason}), "
        f"P&L ${pnl:+.2f} ({r_multiple:+.2f}R)"
    )
