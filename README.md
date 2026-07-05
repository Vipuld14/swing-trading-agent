# Swing trading agent

Phase 1 of the roadmap in `ARCHITECTURE.md`: validate the trend-pullback and
breakout swing strategies against historical data before any of this touches
a live Robinhood account. Phase 2 (see "Paper trading" below) runs that same
validated logic forward, one real day at a time, with no broker connection.

## Setup

Use a virtual environment so these dependencies don't leak into your system
Python:

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Deactivate later with `deactivate`. You'll need to `source .venv/bin/activate`
again each time you open a new terminal to work on this. Everything below
assumes the venv is active.

## Run a backtest

```bash
# Real historical data (needs a machine/CI runner with normal internet access)
python run_backtest.py --config config.yaml

# Smoke-test the pipeline with generated data (works anywhere, no network)
python run_backtest.py --config config.yaml --synthetic

# Try a single strategy or a different risk setting
python run_backtest.py --config config.yaml --strategy pullback --risk-pct 1.5
python run_backtest.py --config config.yaml --start 2019-01-01 --end 2024-01-01
```

Output lands in `backtest_output/`: `summary.json` (strategy vs. buy-and-hold
benchmark), `trades.csv` (every trade with entry/exit/reason/R-multiple),
`equity_curve.csv`, and `equity_curve.png`.

## Multi-period, multi-regime comparison

A single full-period run can be misleading if the universe happens to contain
a handful of outlier winners (e.g. `config.yaml`'s original list includes
NVDA/AMD, which had a historic run during the AI rally — buy-and-hold on
those specific names over that specific window is an unusually high bar).

`config_diversified.yaml` swaps in a broader, sector-spread universe (~26
large, liquid names across tech, healthcare, financials, consumer,
industrials, energy, utilities, materials, communications — deliberately
excluding the most extreme AI-supercycle names). `run_multi_period.py` runs
the strategy and benchmark across four regimes — pre-COVID bull, COVID
crash/recovery + 2022 bear, the recent bull, and the full range — and prints
a side-by-side comparison:

```bash
python run_multi_period.py --config config_diversified.yaml
```

Output: a comparison table printed to the terminal and saved to
`backtest_output/multi_period_comparison.csv`, plus a one-line readout of how
many of the 4 regimes the strategy beat buy-and-hold on (CAGR and Sharpe
separately). A strategy with genuine edge should hold up across most
regimes, not just the one your original universe/period happened to test.

## Strategy diagnostics

```bash
python run_diagnostics.py --config config_diversified.yaml
```

Answers two questions before you trust any headline number:
1. Which leg is actually driving performance — runs pullback-only,
   breakout-only, the combined "both", and `canslim_lite` (see below)
   separately across all four regimes, so a blended result can't hide one
   leg propping up (or dragging down) another. If "both" underperforms its
   best individual leg, that's usually a sign the strategies are competing
   for the same limited position slots rather than genuinely stacking —
   worth raising `max_concurrent_positions` or adding a candidate-ranking
   step (the LLM filter in `ARCHITECTURE.md`'s roadmap) before adding more
   signal types.
2. How much the assumed 5bps slippage is costing vs. a frictionless fill —
   if an edge only exists at zero cost, it isn't a real edge yet.
3. Whether the market-regime overlay + relative-strength prioritization
   (see below) actually improves "both", or just adds complexity for no
   benefit — runs it off (baseline) vs on across all four regimes.
4. Whether trailing stops beat a fixed reward:risk target — including
   against a 2009-2017 window that has never been used to tune anything in
   this project (see "Out-of-sample validation" below for why that matters).

## Out-of-sample validation

Every parameter in this project so far — universe, overlay mode, RS window —
was chosen by checking results against the same 2018-2026 historical window.
That's a real overfitting risk: improving a number on data you've already
looked at doesn't prove anything generalizes. `run_diagnostics.py` fetches
data back to 2008-06-01 and includes a 2009-2017 period (`OUT_OF_SAMPLE_PERIOD`
in `run_multi_period.py`) that has never been used to make any design
decision, specifically to check that.

It already caught something: "both + overlay" showed a full-range Sharpe
essentially tied with buy-and-hold on the 2018-2026 window (0.76 vs 0.77),
but on the untouched 2009-2017 window (one of the strongest sustained bull
markets in modern history), buy-and-hold clearly won on both CAGR (17.44% vs
9.38%) and Sharpe (1.09 vs 0.91). The one thing that DID hold up, in-sample
and out-of-sample alike, without exception across every period ever tested:
max drawdown is always meaningfully smaller than buy-and-hold. Read that as
the honest takeaway — this system is a volatility-dampening tool, not a
"beat the market" one, and its edge shows up in choppy/uncertain regimes
(like 2020-2022), not sustained bulls. Keep using the out-of-sample window
as a check on any future change here, not just the four in-sample regimes.

## Market-regime overlay and relative-strength prioritization

Two cross-cutting features, on top of whichever entry strategy is active
(`risk.market_regime_overlay` and `risk.prioritize_by_relative_strength` in
the config). **On by default in `config_diversified.yaml`** as of 2026-07 —
Diagnostic 3 on real data showed a clean win on the full 2018-present range
and the 2020-2022 regime (better CAGR, Sharpe, drawdown, AND profit factor
simultaneously), a wash in the 2023-present bull, and a small drag in the
specific 2018-2020 window. Still off by default in `config.yaml` (the
original tech-heavy universe hasn't been re-validated with it yet). Set
`enabled: false` to go back to the earlier baseline behavior.

- **Market-regime overlay**: gates or derisks NEW entries — regardless of
  which signal fired — based on whether the broader market (SPY by default)
  is in a confirmed uptrend. `mode: block_entries` opens no new positions at
  all while the market's trend is broken; `mode: reduce_risk` (the default
  when enabled) instead cuts `risk_pct_per_trade` by `risk_reduction_factor`
  for new entries during that time, rather than stopping entirely.
- **Relative-strength prioritization**: when more signals fire on a given
  day than there are open position slots, candidates are filled in order of
  trailing relative strength (strongest first) instead of whichever ticker
  happens to appear first in the config's universe list — which is what the
  code did by default before this existed, and wasn't really a
  prioritization scheme at all.

These reuse the `canslim_lite` strategy's market-timing and leadership
components as a general risk layer, rather than requiring you to switch to
`canslim_lite` outright to get any benefit from them.

If you want to trade some of the full-range gain for closer-to-canslim_lite
crash protection in periods like 2018-2020, try the harder gate instead of
the default soft one:

```yaml
risk:
  market_regime_overlay:
    mode: block_entries   # instead of reduce_risk
```

Not yet validated on real data — worth running Diagnostic 3 again after
switching to confirm the trade-off (likely fewer trades and less upside in
strong bull regimes, in exchange for better protection in broken markets).

Then re-run `run_diagnostics.py` (Diagnostic 3) to see whether it actually
helps on your data, rather than assuming it does.

## Trailing stops

Built in response to the out-of-sample finding above: a fixed reward:risk
target caps a winning trade the moment it hits (say) 2.5R, which is exactly
why this system gives back return during strong sustained bull markets — it
can't participate in a trend beyond its target. A trailing stop instead
follows the highest price reached since entry and only exits when price
pulls back by the stop distance, letting a genuine trend run as far as it
will.

Off by default (`risk.profiles.default.use_trailing_stop: false` in both
config files) so existing results stay comparable. To try it:

```yaml
risk:
  profiles:
    default:
      use_trailing_stop: true
      trailing_activation_r: 1.0     # only start trailing once price is up this many R from entry
      trailing_max_holding_days: 60  # replaces max_holding_days when trailing is on, so winners aren't force-closed early
```

Mechanics: the fixed target is disabled entirely (set to infinity) once
trailing is on — profit-taking happens only via the ratcheting stop, the
original hard stop (before trailing activates), or the (now longer) time
stop as a backstop against dead trades. Verified on a synthetic clean-trend
case before trusting it: with a fixed target the same setup took two small
~2.5R wins and exited early each time; with trailing stops on, the identical
entry stayed open for the full trend and captured 27R instead. R-multiples
in `trades.csv` are computed off the ORIGINAL stop distance (not the
ratcheted one), so they stay comparable to non-trailing trades. Exit reason
is `trailing_stop` once the trail has activated, `stop` if the original
hard stop was hit first.

**Tested on real data 2026-07 and rejected — leave this off.** Diagnostic 4
showed trailing stops underperforming the fixed target in 4 of 5 periods,
including both the critical 2009-2017 out-of-sample window (CAGR 8.75% →
2.23%, Sharpe 0.79 → 0.26) and the 2023-present bull it was specifically
built to help (10.00% → 6.17% CAGR). Drawdown got WORSE, not better, in most
periods too (e.g. full range -17.94% → -19.63%) — likely because the trail
distance is frozen at the ATR measured at entry and never adapts as the
stock's volatility changes over the (now much longer, up to 60-day) hold,
and aging positions tie up slots during broad downturns that the old fixed
target would have freed up sooner. It only helped in the shorter 2018-2020
window. The mechanism itself is verified correct (see the synthetic
clean-trend test above) — the idea just didn't survive contact with real,
noisy price data. Resist the urge to re-tune the trail distance or
activation threshold to chase a better number here: we just spent this
whole project establishing why fitting parameters to the same historical
data is the trap to avoid. Left in the codebase (config default: off) as a
documented, tested, and rejected idea rather than deleted, so it isn't
re-attempted without re-reading this.

### The `canslim_lite` strategy

A price/volume-only approximation of William O'Neil's CANSLIM method,
implemented in `backtest/canslim_lite.py`: breakout on volume (same rule as
the plain breakout strategy), but only when the stock is a relative-strength
leader (top 30% of the universe's trailing 6-month return, re-ranked every
day) AND the broader market (SPY by default) is in a confirmed uptrend. Uses
O'Neil's tighter fixed stop (~7.5%, not ATR-based) with more room to run
(40-day time stop instead of 15, 3:1 reward:risk instead of 2.5:1) — see the
`canslim_lite` profile under `risk.profiles` in the config.

**Honest scope**: this only implements the technical half of CANSLIM ("N" —
breakout on volume, "L" — leader via relative strength, "M" — market
direction). It does NOT implement "C", "A", "S", or "I" (current/annual
earnings growth, sales growth, institutional sponsorship) because those
require point-in-time fundamentals data — knowing what was actually reported
and when, not restated figures pulled later — which this project doesn't
have a reliable source for. Treat any comparison against this as "does
CANSLIM's technical playbook help," not a real CANSLIM backtest. Run it with:

```bash
python run_backtest.py --config config_diversified.yaml --strategy canslim_lite
```

## Paper trading (Phase 2)

`paper_trading/run_daily.py` runs the exact same signal engine, risk
profiles, and market-regime overlay validated above against **live** data,
one real trading day at a time, and logs what it would have done. No broker
connection, no credentials, no real orders — the point is to compare paper
fills and timing against the backtest's assumptions before this goes near
`ARCHITECTURE.md`'s Phase 3 (live trading via the Robinhood MCP).

```bash
# Manual run (fetches real data via yfinance)
python paper_trading/run_daily.py --config config_diversified.yaml

# Smoke-test without network access
python paper_trading/run_daily.py --config config_diversified.yaml --synthetic
```

State lives in `paper_trading/state.json` (cash, open positions, pending
entries, closed trades, an equal-weight buy-and-hold benchmark, and daily
equity snapshots) — plain JSON, not a database, so a GitHub Actions run can
commit it straight back into the repo and pick up where it left off with no
external service. `paper_trading/digest.md` is the human-readable summary of
each run: what closed, what opened, current holdings, and running P&L vs.
the benchmark.

Mechanics worth knowing:
- Signals are computed on a day's close and filled at the **next** trading
  day's real open — same timing convention as the backtest, but using the
  actual observed open instead of a backtest fill assumption.
- If a scheduled run is missed (e.g. the runner is down for a few days), the
  next run catches up by replaying every missed trading day in order, not
  just the latest one — this preserves the correct next-day-fill timing
  even after a multi-day gap. Verified by comparing one big catch-up run
  against the same range processed one day at a time on synthetic data —
  final state (cash, positions, closed trades, equity history) was
  byte-identical either way.
- Re-running with no new trading days available is a no-op (checked against
  `state.json`'s `last_run_date`).
- `--as-of YYYY-MM-DD` is a testing/backfill hook that pretends "today" is
  an earlier date — used for the catch-up-parity check above, and useful if
  you ever need to intentionally replay a specific historical range.

Deployment: `.github/workflows/paper_trading_daily.yml` runs this on
weekdays shortly after the US market close via GitHub Actions cron, then
commits the updated `state.json` and `digest.md` back into the repo. No
secrets are configured because none are needed — this is a pure dry-run
against public market data.

### Intraday exit checks

`paper_trading/check_intraday.py` polls open positions ~4x during market
hours (`.github/workflows/paper_trading_intraday.yml`: ~10am, noon, 2pm,
2:45pm ET) and exits immediately if a stop or target has already been
touched, instead of waiting for the once-daily job to find out at the close.
Deliberately narrow — it does **not** compute signals, fill new entries, or
touch `last_run_date`; those all stay on the once-daily cycle above. If it
never ran at all, `run_daily.py`'s end-of-day check still catches the same
exit using that day's real high/low — this is purely a faster-reaction
layer, not a replacement for the daily job as the source of truth.

```bash
python paper_trading/check_intraday.py                    # normal use (guards on US market hours automatically)
python paper_trading/check_intraday.py --force             # bypass the market-hours guard, e.g. manual testing
python paper_trading/check_intraday.py --force --fake-price MSFT=410.50  # test a specific price, no network call
```

The market-hours check uses `America/New_York` via `zoneinfo`, so it
self-corrects for daylight saving even though the workflow's cron times are
fixed UTC — a run that lands slightly outside 9:30am-4:00pm ET is a harmless
no-op rather than a bad decision made on stale hours.

Caught one real bug worth knowing about: a trailing stop's ratchet
(`highest_price`, `stop_price`) only gets written back to `state.json` if
something *closed* that poll — a bare ratchet-with-no-exit was silently
lost between polls until `check_positions()` was fixed to flag that as a
save-worthy change too. Trailing stops are off by default regardless (see
below), but this would have completely broken them the moment anyone turned
that setting back on.

**Known gap**: `paper_trading/run_daily.py` re-implements the day-stepping
control flow rather than calling `backtest/engine.py` directly (that
function is a monolithic full-range loop, not factored into a reusable
single-day step). It does reuse `backtest/risk.py` and
`backtest/canslim_lite.py` as-is. If you change the exit/entry order or
overlay logic in `engine.py`, make the matching change in
`paper_trading/run_daily.py`'s `process_date()` — they're intentionally
mirrored, not shared, so the backtest stays a dependency-free research tool.

## What "good" looks like before moving to Phase 2 (paper trading)

- Strategy CAGR and Sharpe should beat the buy-and-hold benchmark printed
  alongside it — if a rule doesn't clear that bar after costs, it's not
  worth the added complexity of active trading.
- Max drawdown should be something you could actually sit through without
  panicking or disconnecting the agent mid-drawdown.
- Look at `trades.csv`'s `exit_reason` column: a strategy that's mostly
  hitting `time_stop` (running out the clock) rather than `stop`/`target`
  usually means the setup criteria are too loose.
- Re-run across a few different date ranges and universes — a strategy that
  only works 2020–2021 is fit to one bull market, not a real edge.

## Editing the strategy universe or rules

- `config.yaml` / `config_diversified.yaml` — universe of tickers, risk parameters, strategy thresholds.
- `backtest/strategies.py` — the trend-pullback and breakout entry rules.
- `backtest/canslim_lite.py` — the market-regime filter, relative-strength
  ranking, and CANSLIM-lite entry rule.
- `backtest/indicators.py` — SMA/RSI/ATR/Donchian building blocks.
- `backtest/risk.py` — position sizing (2% equity risk per trade, capped
  position size) plus per-strategy stop/target/holding-period profiles
  under `risk.profiles` in the config (ATR-based by default, fixed-% for
  `canslim_lite`).
- `backtest/engine.py` — the day-by-day simulation loop. Simplifying
  assumptions used are documented at the top of the file.

## Known limitations of this backtest (read before trusting the numbers)

- No modeling of gaps-through-stops beyond filling at the stop price itself
  (real fills in a large gap-down would be worse than this assumes).
- Survivorship bias: the default universe in `config.yaml` is large, still-
  listed companies — a real screen should account for stocks that later
  got delisted or went to zero.
- Data source (Yahoo Finance via `yfinance`) is fine for research; a live
  agent should use a more reliable/paid feed.
- This backtests the *signal engine* only. It does not model the LLM filter
  or Robinhood execution described in `ARCHITECTURE.md` — those get
  validated in Phase 2 (paper/small-size trading) since their behavior
  isn't purely mechanical.
