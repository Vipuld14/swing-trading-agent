# Swing Trading Agent — Architecture

## 1. Goal and requirements

**Goal:** a semi-autonomous swing trading agent that trades a dedicated Robinhood Agentic Trading account, holding positions for days to weeks, without needing to run continuously on a local machine.

**Functional requirements**
- Screen a universe of liquid stocks daily for swing setups (trend pullback, breakout).
- Size positions by risk, not by conviction alone.
- Place, monitor, and exit trades via Robinhood's MCP server.
- Log every decision (signal, reasoning, order) somewhere durable and visible.
- Be pausable/killable instantly.

**Non-functional requirements**
- No requirement for sub-minute latency — daily decision cadence is enough for swing timeframes.
- Must run unattended on a schedule (no laptop required).
- Cost should stay near-zero for a single-user, single-account system.
- Every trade must be explainable after the fact (what signal fired, what data it saw, what it decided).

**Constraints**
- Robinhood Agentic Trading is in beta: equities only today (options/crypto/futures "coming soon" per Robinhood, May 2026 launch).
- Robinhood's MCP server handles account state and order execution, not broad historical/market-wide screening data — a separate data source is needed for the screener.
- Team size: one person. Bias toward simplicity over infrastructure.

## 2. High-level design

```
                     ┌─────────────────────────────┐
                     │   Scheduler (GitHub Actions) │
                     │   cron: ~15 min after close  │
                     └──────────────┬───────────────┘
                                    │ triggers
                                    ▼
   ┌───────────────┐        ┌─────────────────┐        ┌──────────────────┐
   │  Market data   │──────▶│  Screener /      │──────▶│  LLM sanity       │
   │  source        │       │  signal engine   │        │  filter           │
   │ (EOD OHLCV)    │       │ (indicators +    │        │ (news/context     │
   │                │       │  strategy rules) │        │  veto + rank)     │
   └───────────────┘        └─────────────────┘        └─────────┬─────────┘
                                                                   │ ranked candidates
                                                                   ▼
                                                         ┌───────────────────┐
                                                         │  Risk sizer        │
                                                         │ (2% risk/trade,    │
                                                         │  ATR stop, max     │
                                                         │  concurrent pos.)  │
                                                         └─────────┬──────────┘
                                                                   │ sized orders
                                                                   ▼
                                                         ┌───────────────────┐
                                                         │ Robinhood MCP      │
                                                         │ (account state +   │
                                                         │  order execution)  │
                                                         └─────────┬──────────┘
                                                                   │ fills / positions
                                                                   ▼
                                                         ┌───────────────────┐
                                                         │ Logging + alerts   │
                                                         │ (trade log file +  │
                                                         │  email/Slack ping) │
                                                         └───────────────────┘
```

## 3. Data flow (one daily run)

1. Scheduler fires once, ~15 minutes after market close (equities-only beta means no need to react intraday).
2. Pull latest daily OHLCV for the tracked universe from the market data source.
3. Update open positions first: check each held position's stop/target/time-limit against the day's price action; queue exit orders for anything that triggered.
4. Run the signal engine over the universe: trend-pullback and breakout rules produce a candidate list.
5. Send candidates through the LLM filter: veto anything with a disqualifying news event, rank the rest by combined technical + qualitative score.
6. Risk sizer converts ranked candidates into concrete orders: position size from the 2%-of-equity risk rule and the ATR-based stop distance, capped by max concurrent positions and available cash.
7. Orders (exits first, then new entries) are sent to Robinhood's MCP server for the agentic account.
8. Every step's inputs/outputs get written to a trade log (append-only), and a summary notification goes out (what was checked, what fired, what got vetoed, what got placed).

## 4. Components

### Market data source
Screener needs broad historical/EOD data across many tickers — Robinhood's MCP is account/execution-focused, not a general screening API. Use a standard EOD data source (e.g., a free daily-bar provider) for the signal engine and for backtesting; Robinhood's MCP is only invoked for account state and order placement.

### Signal engine
Deterministic, rule-based, and backtestable — this is the same code used in the backtest engine (Phase 1 below), just run on live data instead of history. Two initial strategies:
- **Trend pullback:** uptrend filter (50-day SMA above 200-day SMA and price above 50-day SMA) plus an RSI dip-and-recover through 40.
- **Breakout:** price clears a 20-day high on above-average volume.

Keeping this deterministic (not LLM-driven) means it can be backtested cheaply and its behavior is fully reproducible.

### LLM filter
A thin layer on top of the deterministic signals, not a replacement for them:
- Veto candidates with a disqualifying headline (litigation, guidance cut, delisting risk) since the age of the signal engine's info is at least one day old.
- Rank multiple valid candidates when there are more setups than open slots.
- Optional: generate the daily plain-English summary for the trade log/notification.

### Risk sizer
- Risk per trade: 2% of current account equity (your chosen default).
- Stop distance: ATR(14)-based, not a fixed dollar/percent — adapts to each stock's volatility.
- Position size = (equity × 2%) / stop distance, capped so no single position exceeds ~20–25% of equity (guards against very tight stops producing oversized positions).
- Max concurrent positions (e.g., 6–8) to keep the portfolio diversified and cash available.
- Daily loss circuit breaker: if the account draws down more than a set threshold (e.g., 5%) in a day or from its high-water mark, the agent stops opening new positions and flags for manual review rather than disconnecting itself silently.

### Execution (Robinhood MCP)
- All trading happens inside a dedicated Robinhood Agentic Trading account with its own funded budget — never the main portfolio.
- Preview trades before submission where Robinhood's MCP supports it.
- Exits are placed before new entries in the same run, so the agent frees up capital/slots before trying to use them.

### Logging and alerts
- Append-only trade log (structured, e.g. JSON Lines) recording: date, ticker, signal type, indicator values at signal time, LLM filter verdict, order sent, fill received.
- A daily digest (email or Slack webhook) summarizing what ran, what was skipped and why, and current open positions/P&L — this is your oversight mechanism, since Robinhood's disclosures make clear you're responsible for monitoring the agent.

## 5. Deployment

**Chosen: GitHub Actions cron.**
- A scheduled workflow (`cron` trigger, weekdays ~15 min after close) checks out the repo, installs dependencies, and runs the daily script.
- Secrets (Robinhood MCP credentials, any data-provider API key, notification webhook) live in GitHub Actions encrypted secrets, not in code.
- No server to patch or pay for; the job only exists while it runs.
- Trade-off: GitHub Actions cron schedules can lag by a few minutes at peak times, and there's a hard 6-hour job timeout (irrelevant here — a daily run should take seconds to low minutes). If tighter timing or intraday runs are ever needed, revisit with a serverless scheduler (AWS EventBridge + Lambda).

## 6. Risk and reliability

- Kill switch: disconnect the agent from the Robinhood app itself at any time — this is outside the agent's own code and always available.
- Every run is idempotent: re-running the same day's job shouldn't double-place orders (check current positions/open orders before acting).
- If the market data source or LLM call fails, the run should fail loudly (alert) and take no action, rather than trading on incomplete information.
- Backtest and paper-trade before any capital is at risk (see roadmap).

## 7. Trade-offs made explicit

| Decision | Trade-off |
|---|---|
| Daily cadence, not intraday | Misses same-day reversals, but matches swing holding periods and keeps cost/complexity near zero |
| Rule-based signals + LLM filter (not LLM-driven decisions) | Less "autonomous-feeling" but fully backtestable and reproducible; the LLM adds judgment, not core logic |
| GitHub Actions over a VPS | Free and simple, but less control over exact run timing and no persistent local state between runs (state must live in the repo/account, not memory) |
| Separate market data source from Robinhood MCP | Extra integration point, but Robinhood's MCP isn't designed for broad screening |

## 8. Roadmap

1. **Backtest engine** (this delivery): validate the trend-pullback and breakout rules on historical data before writing any live-trading code. Must beat a buy-and-hold benchmark on a risk-adjusted basis before moving on.
2. **Paper/small-size validation**: run the exact same signal engine live for several weeks at minimal position size in the agentic account, comparing actual fills/slippage to backtest assumptions.
3. **Live with real risk budget**: only after (1) and (2) hold up, wire in the LLM filter and full risk sizing against the live account.
4. **Extensions**: sector exposure caps, options-based income strategies once Robinhood's beta expands, adaptive strategy retirement if a rule's live performance diverges from its backtest.

## 9. Open questions / assumptions to revisit

- Account size and true dollar risk tolerance — 2%/trade was your chosen default; revisit position caps once a real account size is set.
- Which EOD data provider to standardize on for the screener (free tier is fine for the backtest phase; live trading may warrant a paid feed for reliability).
- Notification channel (email vs. Slack webhook) for the daily digest.
