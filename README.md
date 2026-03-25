# Trader

Python day-trading bot for Interactive Brokers IB Gateway / TWS.

This project uses:
- `uv`
- Python `3.12`
- the vendored IB API client in `./ibga`
- a Textual terminal UI
- a gRPC control plane
- SQLite for local state, orders, and trade events

Market data comes directly from the `ibga` / IBKR socket client. The gRPC server is only an optional local control surface.

## Before You Run

Read the environment variables in [`.env.example`](/home/xn/nudes/pprog/trader/.env.example).

That file contains the runtime configuration for:
- IB host / port / client ID
- paper vs live mode
- scanner settings
- gRPC host / port
- local SQLite path
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
python3 -m compileall src tests main.py
```

## Run The Bot

Start the TUI:

```bash
uv run --python 3.12 trader bot
```

Run headless with gRPC enabled:

```bash
uv run --python 3.12 trader grpc
```

Run headless without the Textual UI:

```bash
uv run --python 3.12 trader bot --no-tui
```

`trader bot` and `trader bot --no-tui` use the direct IBKR client only and do not start the gRPC server.

If IBKR is unreachable, the bot now fails cleanly, records the broker error in runtime state, and does not continue spamming market-data or scanner requests into a dead connection.

## TUI Layout

The terminal is split into 3 sections:

- left `1/4`: bot logs
- middle `2/4`: main content
- right `1/4`: watchlist, orders, gRPC info

Top summary cards show:

- broker connection
- `Balance`
- `Market Status` with `Open`, `Pre-Market`, or `Closed`
- signed realized P&L with `+` / `-` when non-zero

The top header bar also mirrors the live `Balance`, `Market Status`, and signed `P&L` alongside the clock.

Main shortcuts:

- `b l`: toggle full-screen bot logs
- `m`: toggle full-screen live market data
- `s`: toggle full-screen positions
- `w`: toggle full-screen watchlist
- `o`: toggle full-screen orders
- `g`: toggle full-screen gRPC panel
- `p`: pause trading
- `r`: resume trading
- `Esc`: clear full-screen mode
- `q`: quit

Press the same shortcut again to turn full-screen mode off.

## gRPC

This control plane is only started by the explicit `trader grpc` or `trader rpc` commands.

The Python gRPC bindings are regenerated from [`proto/trader.proto`](/home/xn/nudes/pprog/trader/proto/trader.proto) and [`proto/grpc/reflection/v1alpha/reflection.proto`](/home/xn/nudes/pprog/trader/proto/grpc/reflection/v1alpha/reflection.proto) when those source files are newer than the checked-in generated modules.

Default target:

```text
127.0.0.1:8765
```

List services:

```bash
grpcurl -plaintext 127.0.0.1:8765 list
```

Get health:

```bash
grpcurl -plaintext 127.0.0.1:8765 trader.v1.TraderControl/Health
```

Get runtime state:

```bash
grpcurl -plaintext 127.0.0.1:8765 trader.v1.TraderControl/GetState
```

Watch runtime stream:

```bash
grpcurl -plaintext -d '{"interval_ms":1000}' 127.0.0.1:8765 trader.v1.TraderControl/WatchRuntime
```

Update a stop:

```bash
grpcurl -plaintext -d '{"symbol":"AMD","new_stop":"9.85","reason":"manual"}' 127.0.0.1:8765 trader.v1.TraderControl/UpdateStop
```

## Files Written By The Bot

SQLite database:

```text
.trader/state.sqlite3
```

Tables include:

- `kv_store`
- `positions`
- `orders`
- `trade_events`

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

## Trading Logic Notes

- Market data comes directly from the IBKR socket client in `ibga`; the gRPC server is not used for quote transport.
- The bot trades only when real account equity is available from IBKR. It no longer sizes entries from a fallback balance.
- A first-red exit only uses completed bars, not a still-forming minute candle.
- ORB entries only trigger on the breakout cross instead of re-firing repeatedly after the level is already broken.
- Bull-flag and flat-top entries now require breakout volume confirmation.
- If broker positions and local managed positions disagree, trading is paused and the mismatch is surfaced as an error.

## Project Notes

- Scanner uses Interactive Brokers top gainers on startup.
- Scanner location is `STK.US.MAJOR`.
- Scanner results are stored for the day and used to build the watchlist.
- The current implementation is intended for same-day trading only; no overnight holds are intended.
