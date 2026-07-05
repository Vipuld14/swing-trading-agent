#!/usr/bin/env python3
"""Intraday exit monitoring for paper trading.

run_daily.py is the source of truth: once a day, after close, it computes
signals, fills pending entries, and checks exits against that day's full
high/low range. This script does ONE thing several times during market
hours: ask whether any OPEN position's stop or target has already been
touched, using the current live price, and exit immediately if so, instead
of waiting for the end of the day to find out.

Deliberately narrow scope -- this does NOT:
  - compute new signals
  - fill new entries (those stay on the once-daily open-price fill timing)
  - advance state.last_run_date (that's run_daily.py's checkpoint, not this
    script's)
  - touch equity_history (kept at one point per day, from run_daily.py, so
    the digest's daily readout isn't muddied by multiple intraday points)

If this script never runs, or misses a breach between polls, run_daily.py's
end-of-day check still catches it using the real day's low/high -- this is
purely a faster-reaction layer on top of that, not a replacement for it.

Known limitation: a trailing stop's "highest price seen" is only updated at
each poll, not continuously, so it can undercount the true intraday high
between checks (same caveat applies to run_daily.py's daily-bar version,
just at coarser granularity). Trailing stops are off by default in the
config regardless (see README).

Usage:
    python paper_trading/check_intraday.py --state-file paper_trading/state.json
    python paper_trading/check_intraday.py --force   # bypass the market-hours guard, e.g. for manual testing
    python paper_trading/check_intraday.py --fake-price MSFT=410.50 --fake-price DUK=230.00  # test without network
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paper_trading.state import load_state, save_state
from paper_trading.execution import close_position

MARKET_OPEN = dt_time(9, 30)
MARKET_CLOSE = dt_time(16, 0)
ET = ZoneInfo("America/New_York")


def market_is_open(now_et: datetime) -> bool:
    if now_et.weekday() >= 5:  # Saturday/Sunday
        return False
    return MARKET_OPEN <= now_et.time() <= MARKET_CLOSE


def fetch_current_price(ticker: str) -> float | None:
    import yfinance as yf

    try:
        fast = yf.Ticker(ticker).fast_info
        price = fast.get("lastPrice") or fast.get("last_price")
        return float(price) if price else None
    except Exception as exc:  # network hiccups shouldn't crash the whole poll
        print(f"  Could not fetch a live price for {ticker}: {exc}")
        return None


def check_positions(state, prices: dict[str, float], log: list[str]) -> bool:
    """Applies the trailing-ratchet-then-stop/target check from
    backtest/engine.py / run_daily.py, but against a single current price
    instead of a full day's high/low range. Returns True if state needs
    saving -- which includes a bare trailing-stop ratchet with no exit, not
    just a close. Without that, highest_price/stop_price mutations would be
    silently lost every poll (state.json never gets rewritten), so the
    trailing stop would never actually progress across multiple checks.
    """
    changed = False
    for ticker in list(state.open_positions):
        pos = state.open_positions[ticker]
        price = prices.get(ticker)
        if price is None:
            continue

        if pos.use_trailing_stop:
            prev_highest, prev_active, prev_stop = pos.highest_price, pos.trailing_active, pos.stop_price
            pos.highest_price = max(pos.highest_price, price)
            if not pos.trailing_active and pos.highest_price >= pos.trailing_activation_price:
                pos.trailing_active = True
            if pos.trailing_active:
                new_stop = pos.highest_price - pos.trail_distance
                if new_stop > pos.stop_price:
                    pos.stop_price = new_stop
            if (pos.highest_price, pos.trailing_active, pos.stop_price) != (prev_highest, prev_active, prev_stop):
                changed = True

        if price <= pos.stop_price:
            reason = "intraday_trailing_stop" if pos.trailing_active else "intraday_stop"
            close_position(state, pos, str(datetime.now(ET).date()), price, reason, log)
            del state.open_positions[ticker]
            changed = True
        elif price >= pos.target_price:
            close_position(state, pos, str(datetime.now(ET).date()), price, "intraday_target", log)
            del state.open_positions[ticker]
            changed = True

    return changed


def append_intraday_log(path: Path, log: list[str]) -> None:
    timestamp = datetime.now(ET).strftime("%Y-%m-%d %H:%M %Z")
    lines = [f"\n## Intraday check -- {timestamp}"]
    lines.extend(log)
    with path.open("a") as f:
        f.write("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll open paper-trading positions for a stop/target hit.")
    parser.add_argument("--state-file", default="paper_trading/state.json")
    parser.add_argument("--digest-file", default="paper_trading/digest.md")
    parser.add_argument("--force", action="store_true", help="Run even outside US market hours (testing/manual use).")
    parser.add_argument("--fake-price", action="append", default=[],
                         help="TICKER=PRICE override, no network call for that ticker. Repeatable. For testing.")
    args = parser.parse_args()

    now_et = datetime.now(ET)
    if not args.force and not market_is_open(now_et):
        print(f"Market is closed ({now_et.strftime('%Y-%m-%d %H:%M %Z')}). Skipping intraday check. Use --force to override.")
        return

    state_path = Path(args.state_file)
    # initial_capital is only used if no state file exists yet -- for an
    # intraday check that should never be the case (run_daily.py creates it
    # first), but 0.0 is a harmless placeholder if it somehow is.
    state = load_state(state_path, initial_capital=0.0)

    if not state.open_positions:
        print("No open positions. Nothing to check.")
        return

    fake_prices = {}
    for entry in args.fake_price:
        ticker, _, price = entry.partition("=")
        fake_prices[ticker] = float(price)

    prices = {}
    for ticker in state.open_positions:
        if ticker in fake_prices:
            prices[ticker] = fake_prices[ticker]
        else:
            prices[ticker] = fetch_current_price(ticker)

    log: list[str] = []
    changed = check_positions(state, prices, log)

    if changed:
        save_state(state_path, state)
        if log:
            append_intraday_log(Path(args.digest_file), log)
            print(f"Closed {len(log)} position(s):")
            for line in log:
                print(line)
        else:
            print("Checked -- a trailing stop ratcheted up, nothing closed.")
    else:
        print("Checked, nothing hit a stop or target.")


if __name__ == "__main__":
    main()
