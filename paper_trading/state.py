"""Persistent state for the daily paper-trading step.

This is deliberately plain JSON, not a database: a GitHub Actions run can
commit the updated file straight back into the repo, so state survives
between scheduled runs without needing any external service or credentials.

Phase 2 of ARCHITECTURE.md's roadmap is "paper/small-size validation" --
this module holds the paper side: cash, open positions, entries that
signaled yesterday and get filled at today's real open (mirroring the
backtest's t / t+1 convention), closed trade history, and daily equity
snapshots for both the strategy and an equal-weight buy-and-hold benchmark
tracked the same way.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class PaperPosition:
    ticker: str
    shares: int
    entry_price: float
    entry_date: str
    stop_price: float
    initial_stop_price: float
    target_price: float
    signal_type: str
    max_holding_days: int
    use_trailing_stop: bool = False
    trail_distance: float = 0.0
    trailing_activation_price: float = float("inf")
    highest_price: float = 0.0
    trailing_active: bool = False
    days_held: int = 0


@dataclass
class PendingEntry:
    """A signal from the most recently processed day, to be filled at the
    NEXT processed day's real open -- same timing as the backtest engine.
    """
    ticker: str
    signal_date: str
    signal_type: str
    atr_at_signal: float | None


@dataclass
class ClosedTrade:
    ticker: str
    signal_type: str
    entry_date: str
    exit_date: str
    shares: int
    entry_price: float
    exit_price: float
    exit_reason: str
    pnl: float
    r_multiple: float


@dataclass
class PaperState:
    cash: float
    initial_capital: float
    last_run_date: str | None = None
    open_positions: dict[str, PaperPosition] = field(default_factory=dict)
    pending_entries: dict[str, PendingEntry] = field(default_factory=dict)
    closed_trades: list[ClosedTrade] = field(default_factory=list)
    equity_history: list[dict] = field(default_factory=list)
    # Equal-weight buy-and-hold benchmark, bought once on day 1 and marked to
    # market alongside the strategy, so the digest can show a running
    # comparison instead of just the strategy in isolation.
    benchmark_shares: dict[str, float] = field(default_factory=dict)
    benchmark_cash: float = 0.0


def load_state(path: Path, initial_capital: float) -> PaperState:
    if not path.exists():
        return PaperState(cash=initial_capital, initial_capital=initial_capital)
    raw = json.loads(path.read_text())
    return PaperState(
        cash=raw["cash"],
        initial_capital=raw.get("initial_capital", initial_capital),
        last_run_date=raw.get("last_run_date"),
        open_positions={k: PaperPosition(**v) for k, v in raw.get("open_positions", {}).items()},
        pending_entries={k: PendingEntry(**v) for k, v in raw.get("pending_entries", {}).items()},
        closed_trades=[ClosedTrade(**t) for t in raw.get("closed_trades", [])],
        equity_history=raw.get("equity_history", []),
        benchmark_shares=raw.get("benchmark_shares", {}),
        benchmark_cash=raw.get("benchmark_cash", 0.0),
    )


def save_state(path: Path, state: PaperState) -> None:
    payload = {
        "cash": state.cash,
        "initial_capital": state.initial_capital,
        "last_run_date": state.last_run_date,
        "open_positions": {k: asdict(v) for k, v in state.open_positions.items()},
        "pending_entries": {k: asdict(v) for k, v in state.pending_entries.items()},
        "closed_trades": [asdict(t) for t in state.closed_trades],
        "equity_history": state.equity_history,
        "benchmark_shares": state.benchmark_shares,
        "benchmark_cash": state.benchmark_cash,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str))
