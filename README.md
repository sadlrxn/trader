# Trader

Python day-trading bot for Interactive Brokers IB Gateway / TWS.

This project uses:
- `uv`
- Python `3.12`
- the vendored IB API client in `./ibga`
- a Textual terminal UI
- SQLite for local state, orders, and trade events

Market data comes directly from the `ibga` / IBKR socket client.

## Before You Run

Read the environment variables in [`.env.example`](/home/xn/nudes/pprog/trader/.env.example).

That file contains the runtime configuration for:
- IB host / port / client ID
- paper vs live mode
- scanner settings
- local SQLite path
- daily trade journal path
- daily watchlist JSON path
- TUI enable flag

Default local paper settings in this repo:

```env
IB_HOST=127.0.0.1
IB_PORT=7497
IB_PAPER=true
```

If you are running IB Gateway instead of TWS, verify the socket port. A common paper-trading IB Gateway default is `4002`.

If you want local overrides, create or edit `.env`.

## Requirements

1. Start IB Gateway or TWS.
2. Enable API access in IB Gateway / TWS.
3. Make sure the socket port matches your `.env`.
4. Use paper mode first.
5. Install dependencies with `uv`.

## Install

From the project root:

```bash
uv sync --python 3.12 --extra dev
```

## Basic Checks

Validate config and endpoint settings:

```bash
uv run --python 3.12 trader check
```

Run the tests:

```bash
uv run --python 3.12 --extra dev pytest
```

Compile the Python code before running:

```bash
uv run python -m compileall src tests main.py
```

## Run The Bot

Start the TUI:

```bash
uv run --python 3.12 trader bot
```

Run headless without the Textual UI:

```bash
uv run --python 3.12 trader bot --no-tui
```

`trader bot` and `trader bot --no-tui` use the direct IBKR client only.

If IBKR is unreachable, the bot now fails cleanly, records the broker error in runtime state, and does not continue spamming market-data or scanner requests into a dead connection.

## TUI Layout

The terminal is split into 2 columns:

- left: live market list
- right: open positions, closed trades, orders, and bot logs

Top summary cards show:

- broker connection
- `Balance`
- `Market Status` with `Open`, `Pre-Market`, or `Closed`
- signed realized P&L with `+` / `-` when non-zero

The top header bar also mirrors the live `Balance`, `Market Status`, and signed `P&L` alongside the clock.

Main shortcuts:

- `l`: toggle full-screen bot logs
- `m`: toggle full-screen live market data
- `s`: toggle full-screen positions
- `o`: toggle full-screen orders
- `x`: open the close-position modal; press a position number to submit a manual exit, or `x` / `Esc` to cancel
- `p`: pause trading
- `r`: resume trading
- `Esc`: clear full-screen mode
- `q`: quit

## Files Written By The Bot

SQLite database:

```text
.trader/state.db
```

Tables include:

- `kv_store`
- `positions`
- `closed_positions`
- `orders`
- `trade_events`

The position, order, closed-trade, and trade-event tables are bot-scoped audit
records. They track only symbols and orders handled by this bot, not unrelated
manual trades elsewhere in the account.

Daily trade journal JSON:

```text
.trader/trades/trades-DD-MM-YYYY.json
```

Each journal file is a JSON array of executed buy and sell operations, including
time, amount, operation, stock, change during buy, and realized profit.

Daily watchlist JSON:

```text
.trader/watchlists/watchlist-YYYYMMDD.json
```

The watchlist file is refreshed from the IB scanner and reused during the same trading day.

## Live Market Data Notes

If the market is closed, the market panel may show subscribed symbols with no live ticks yet.

Typical reasons:

- weekend or holiday
- market not open yet
- no live market-data entitlement in IB
- IB Gateway / TWS API connection is up but quote ticks are not streaming

The panel still shows subscribed symbols, and VWAP can be derived from recent bars when available.

Live market rows are color-coded:

- green when the latest price moved up versus the prior refresh
- red when the latest price moved down

The market panel is sized for the live scanner list and now defaults to up to
20 symbols. It also shows:

- `PM30%`: distance from the high made in the final 30 minutes before the open
- `HOD%`: distance from the current intraday high of day

## Trading Logic Notes

- Market data comes directly from the IBKR socket client in `ibga`.
- The bot trades only when real account equity is available from IBKR. It no longer sizes entries from a fallback balance.
- The live trade gate also enforces the scanner profile: the symbol must still be inside the configured price band and up at least `TRADER_MIN_DAY_GAIN_PCT` on the same trading day.
- Position size is capped by both stop-risk and max notional so tight-stop setups do not turn into broker-rejected oversized orders.
- A first-red exit only uses completed bars, not a still-forming minute candle.
- ORB entries now reference the final 30-minute pre-open high and intraday HOD instead of the entire premarket session.
- ORB entries only trigger on the breakout cross instead of re-firing repeatedly after the level is already broken.
- First-pullback entries are supported in addition to ORB, bull-flag, and flat-top breakouts.
- Bull-flag and flat-top entries now require breakout volume confirmation.
- The scanner owns the watchlist by default; there is no built-in static watchlist unless `TRADER_FALLBACK_SYMBOLS` is explicitly configured.
- Fallback symbols are only a bootstrap when no live scanner symbols are available; once live momentum symbols arrive, the fallback list is discarded.
- Broker position syncing is scoped to symbols already known to this bot, so unrelated account holdings are not shown as bot-managed trades.
- Open positions stay subscribed even if the scanner rotates to other symbols.
- Executed buys and sells are written to dated JSON trade journals in addition to SQLite trade events.
- Closed trades are persisted in SQLite and shown in a dedicated closed-positions panel in the TUI.
- Because this bot is for day trading, it now requests manual-exit style flattening for any remaining open positions at `TRADER_FLATTEN_TIME` (default `15:55` ET).
- If broker positions and local managed positions disagree, trading is paused and the mismatch is surfaced as an error.

## Project Notes

- Scanner uses Interactive Brokers top gainers on startup.
- Scanner location is `STK.US.MAJOR`.
- Scanner results are stored for the day and used to build the watchlist.
- The current implementation is intended for same-day trading only; no overnight holds are intended.
