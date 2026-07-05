"""Position sizing and stop/target calculation.

Sizing rule: risk a fixed percentage of current equity per trade, sized off
the distance to the stop -- not off conviction, and not a flat dollar amount
per position. `risk_pct_per_trade` and `max_position_pct` are global (apply
to every trade regardless of which signal produced it); the STOP METHOD
itself (ATR-multiple vs. a fixed percentage, reward:risk, max holding days)
can differ per strategy via `risk.profiles` in the config -- e.g. the
canslim_lite strategy uses O'Neil's tighter fixed 7-8% stop instead of the
ATR-based stop used by the pullback/breakout strategies.

Trailing stops: when a profile sets `use_trailing_stop: true`, the fixed
reward:risk target is disabled (set to infinity) and `trailing_max_holding_days`
replaces `max_holding_days`, since the whole point is to let a winner run
past what a fixed target would have closed. The engine (backtest/engine.py)
does the actual day-by-day ratcheting; this module only computes the
distance and activation threshold at entry time.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TradePlan:
    shares: int
    entry_price: float
    stop_price: float
    target_price: float
    risk_amount: float
    position_value: float
    max_holding_days: int
    use_trailing_stop: bool
    trail_distance: float
    trailing_activation_price: float


def resolve_risk_profile(cfg: dict, signal_type: str) -> dict:
    """Look up the stop/target/holding-period rules for a given signal type,
    falling back to the "default" profile (used by pullback and breakout).
    """
    profiles = cfg["risk"]["profiles"]
    return profiles.get(signal_type, profiles["default"])


def plan_trade(
    equity: float,
    entry_price: float,
    atr_value: float | None,
    cfg: dict,
    risk_profile: dict,
) -> TradePlan | None:
    risk_cfg = cfg["risk"]

    stop_method = risk_profile.get("stop_method", "atr")
    if stop_method == "atr":
        if atr_value is None or atr_value != atr_value:  # NaN check without importing numpy/pandas here
            return None
        stop_distance = risk_profile["atr_stop_multiple"] * atr_value
    elif stop_method == "fixed_pct":
        stop_distance = entry_price * (risk_profile["stop_pct"] / 100.0)
    else:
        raise ValueError(f"Unknown stop_method: {stop_method}")

    if stop_distance <= 0 or entry_price <= 0:
        return None

    stop_price = entry_price - stop_distance
    if stop_price <= 0:
        return None

    use_trailing_stop = bool(risk_profile.get("use_trailing_stop", False))
    if use_trailing_stop:
        target_price = float("inf")  # profit-taking happens via the trailing stop instead
        max_holding_days = risk_profile.get("trailing_max_holding_days", risk_profile["max_holding_days"])
        activation_r = risk_profile.get("trailing_activation_r", 0.0)
        trailing_activation_price = entry_price + activation_r * stop_distance
    else:
        target_price = entry_price + risk_profile["reward_risk_multiple"] * stop_distance
        max_holding_days = risk_profile["max_holding_days"]
        trailing_activation_price = float("inf")  # never activates

    risk_amount = equity * (risk_cfg["risk_pct_per_trade"] / 100.0)
    shares = int(risk_amount // stop_distance)

    if shares <= 0:
        return None

    # Cap position value so a very tight stop can't create an oversized position.
    max_position_value = equity * (risk_cfg["max_position_pct"] / 100.0)
    if shares * entry_price > max_position_value:
        shares = int(max_position_value // entry_price)

    if shares <= 0:
        return None

    position_value = shares * entry_price
    actual_risk = shares * stop_distance

    return TradePlan(
        shares=shares,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        risk_amount=actual_risk,
        position_value=position_value,
        max_holding_days=max_holding_days,
        use_trailing_stop=use_trailing_stop,
        trail_distance=stop_distance,
        trailing_activation_price=trailing_activation_price,
    )
