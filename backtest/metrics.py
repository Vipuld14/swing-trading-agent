"""Performance metrics for a backtest run, plus a buy-and-hold benchmark.

The whole point of backtesting a strategy before it ever touches a live
account is to see whether it beats simply buying and holding the same
universe -- if it doesn't, on a risk-adjusted basis, it's not worth the
added complexity and slippage of active trading.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _max_drawdown(equity: pd.Series) -> float:
    running_max = equity.cummax()
    drawdown = (equity - running_max) / running_max
    return float(drawdown.min()) if len(drawdown) else 0.0


def _cagr(equity: pd.Series) -> float:
    if len(equity) < 2:
        return 0.0
    n_years = (equity.index[-1] - equity.index[0]).days / 365.25
    if n_years <= 0:
        return 0.0
    total_return = equity.iloc[-1] / equity.iloc[0]
    if total_return <= 0:
        return -1.0
    return float(total_return ** (1 / n_years) - 1)


def _sharpe(equity: pd.Series) -> float:
    daily_returns = equity.pct_change().dropna()
    if daily_returns.std() == 0 or len(daily_returns) < 2:
        return 0.0
    return float((daily_returns.mean() / daily_returns.std()) * np.sqrt(252))


def summarize(result: dict, initial_capital: float) -> dict:
    equity_curve = result["equity_curve"]
    trades = result["trades"]

    if equity_curve.empty:
        return {"error": "No equity curve produced (check date range and data)."}

    equity = equity_curve["equity"]
    final_equity = equity.iloc[-1]

    summary = {
        "start_date": str(equity.index[0].date()),
        "end_date": str(equity.index[-1].date()),
        "initial_capital": round(initial_capital, 2),
        "final_equity": round(final_equity, 2),
        "total_return_pct": round((final_equity / initial_capital - 1) * 100, 2),
        "cagr_pct": round(_cagr(equity) * 100, 2),
        "max_drawdown_pct": round(_max_drawdown(equity) * 100, 2),
        "sharpe_approx": round(_sharpe(equity), 2),
        "num_trades": int(len(trades)),
    }

    if not trades.empty:
        wins = trades[trades["pnl"] > 0]
        losses = trades[trades["pnl"] <= 0]
        gross_profit = wins["pnl"].sum()
        gross_loss = -losses["pnl"].sum()

        summary.update(
            {
                "win_rate_pct": round(len(wins) / len(trades) * 100, 2),
                "avg_r_multiple": round(trades["r_multiple"].mean(), 2),
                "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf"),
                "avg_win": round(wins["pnl"].mean(), 2) if len(wins) else 0.0,
                "avg_loss": round(losses["pnl"].mean(), 2) if len(losses) else 0.0,
                "exit_reason_counts": trades["exit_reason"].value_counts().to_dict(),
            }
        )

    return summary


def buy_and_hold_benchmark(data: dict[str, pd.DataFrame], cfg: dict) -> dict:
    """Equal-weight buy-and-hold across the same universe and date range, as
    the bar the active strategy needs to clear.
    """
    bt_cfg = cfg["backtest"]
    start = pd.Timestamp(bt_cfg["start_date"])
    all_dates = sorted(set().union(*[df.index for df in data.values()]))
    end = pd.Timestamp(bt_cfg["end_date"]) if bt_cfg.get("end_date") else all_dates[-1]

    per_ticker_capital = bt_cfg["initial_capital"] / len(data)
    normalized = []
    for ticker, df in data.items():
        window = df[(df.index >= start) & (df.index <= end)]
        if window.empty:
            continue
        shares = per_ticker_capital / window["close"].iloc[0]
        normalized.append(shares * window["close"])

    if not normalized:
        return {"error": "No data in the requested window for benchmark."}

    combined = pd.concat(normalized, axis=1).sum(axis=1).dropna()
    combined.name = "equity"

    return summarize({"equity_curve": combined.to_frame(), "trades": pd.DataFrame()}, bt_cfg["initial_capital"])
