"""Textual terminal UI for the trading bot."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from trader.runtime import TradingRuntime

logger = logging.getLogger(__name__)


class TraderTui(App[None]):
    """Render runtime status, watchlist, positions, and logs in the terminal."""

    CSS = """
    Screen {
        background: #08111f;
        color: #d9e2ec;
    }

    #root {
        height: 1fr;
        padding: 1 1;
    }

    .card {
        height: 5;
        padding: 0 2;
        background: #0f1c2e;
        border: round #29527a;
        color: #f4f7fb;
    }

    .card-title {
        color: #8fb7dd;
        text-style: bold;
    }

    .hidden {
        display: none;
    }

    #workspace {
        height: 1fr;
    }

    #main-column {
        width: 2fr;
        margin-right: 1;
    }

    #logs-column {
        width: 1fr;
    }

    #cards {
        height: 5;
        margin-bottom: 1;
    }

    #market-table {
        height: 2fr;
        margin-bottom: 1;
    }

    #positions-table {
        height: 1fr;
        margin-bottom: 1;
    }

    #orders-table {
        height: 1fr;
    }

    #logs-panel {
        height: 1fr;
    }

    DataTable, RichLog {
        background: #0b1626;
        border: round #2f5d87;
        color: #dce7f2;
    }

    RichLog {
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit_bot", "Quit"),
        Binding("p", "pause_trading", "Pause"),
        Binding("r", "resume_trading", "Resume"),
        Binding("b,l", "toggle_logs_focus", "Logs"),
        Binding("m", "toggle_market_focus", "Market"),
        Binding("s", "toggle_positions_focus", "Positions"),
        Binding("o", "toggle_orders_focus", "Orders"),
        Binding("escape", "clear_focus", "Clear"),
    ]

    def __init__(self, runtime: TradingRuntime) -> None:
        """Initialize the Textual application.

        Args:
            runtime: Shared trading runtime.
        """

        super().__init__()
        self._runtime = runtime
        self._rendered_log_lines = 0
        self._focused_panel: str | None = None
        self._startup_task: asyncio.Task[None] | None = None
        self._previous_last_prices: dict[str, Decimal] = {}

    def compose(self) -> ComposeResult:
        """Compose the Textual layout."""

        yield Header(show_clock=True)
        yield Vertical(
            Horizontal(
                Vertical(
                    Horizontal(
                        Static(id="broker-card", classes="card"),
                        Static(id="market-card", classes="card"),
                        Static(id="account-card", classes="card"),
                        Static(id="risk-card", classes="card"),
                        id="cards",
                    ),
                    DataTable(id="market-table"),
                    DataTable(id="positions-table"),
                    DataTable(id="orders-table"),
                    id="main-column",
                ),
                Vertical(
                    RichLog(id="logs-panel", wrap=False, highlight=True, markup=True),
                    id="logs-column",
                ),
                id="workspace",
            ),
            id="root",
        )
        yield Footer()

    async def on_mount(self) -> None:
        """Start runtime services and initialize tables."""

        market = self.query_one("#market-table", DataTable)
        market.add_columns(
            "Symbol", "Last", "Chg%", "Vol", "Spread", "Gap%",
            "RSI", "ATR%", "EMA x", "Pattern", "Signal",
        )
        market.cursor_type = "row"
        market.zebra_stripes = True
        market.border_title = "Market Data"

        positions = self.query_one("#positions-table", DataTable)
        positions.add_columns(
            "Symbol", "Qty", "Entry", "Current", "P&L$", "P&L%",
            "R-Mult", "Stop", "Target", "Pattern", "Held",
        )
        positions.cursor_type = "row"
        positions.zebra_stripes = True
        positions.border_title = "Positions"

        orders = self.query_one("#orders-table", DataTable)
        orders.add_columns("ID", "Symbol", "Type", "Side", "Qty", "Status", "Px")
        orders.cursor_type = "row"
        orders.zebra_stripes = True
        orders.border_title = "Orders"

        log_widget = self.query_one("#logs-panel", RichLog)
        log_widget.border_title = "Logs"

        self.set_interval(1, self._refresh_view)
        self._startup_task = asyncio.create_task(self._startup_services(), name="tui-startup")

    async def on_unmount(self) -> None:
        """Stop runtime services during shutdown."""

        if self._startup_task is not None:
            self._startup_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._startup_task
        await self._runtime.stop()

    async def _startup_services(self) -> None:
        """Start the runtime services without blocking the initial TUI render."""

        try:
            await self._runtime.start()
        except Exception:
            logger.exception("Failed to start runtime services.")

    async def action_quit_bot(self) -> None:
        """Quit the terminal application."""

        self.exit()

    async def action_pause_trading(self) -> None:
        """Pause new entries from the keyboard."""

        self._runtime.pause_trading()

    async def action_resume_trading(self) -> None:
        """Resume new entries from the keyboard."""

        self._runtime.resume_trading()

    async def action_toggle_logs_focus(self) -> None:
        """Toggle full-screen focus for the bot logs panel."""

        self._toggle_focus("logs")

    async def action_toggle_market_focus(self) -> None:
        """Toggle full-screen focus for the live market data panel."""

        self._toggle_focus("market")

    async def action_toggle_positions_focus(self) -> None:
        """Toggle full-screen focus for the positions panel."""

        self._toggle_focus("positions")

    async def action_toggle_orders_focus(self) -> None:
        """Toggle full-screen focus for the orders panel."""

        self._toggle_focus("orders")

    async def action_clear_focus(self) -> None:
        """Clear any active full-screen focus mode."""

        self._toggle_focus(None)

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def _refresh_view(self) -> None:
        """Redraw the status, tables, and logs."""

        status = self._runtime.snapshot_status()
        self._render_cards(status)
        self._update_header(status)

        # Market data table (merged watchlist + indicators)
        market = self.query_one("#market-table", DataTable)
        market.clear(columns=False)
        for row in self._market_rows(status)[:30]:
            market.add_row(*row)

        # Positions
        positions = self.query_one("#positions-table", DataTable)
        positions.clear(columns=False)
        quotes = {q.symbol: q for q in self._runtime.snapshot_quotes()}
        for pos in status.positions:
            quote = quotes.get(pos.symbol)
            current = quote.last if quote else pos.entry_price
            pnl_dollars = (current - pos.entry_price) * pos.remaining_quantity
            pnl_pct = ((current - pos.entry_price) / pos.entry_price * 100) if pos.entry_price else Decimal("0")
            risk = pos.entry_price - pos.stop_price
            r_mult = float((current - pos.entry_price) / risk) if risk else 0.0
            held_seconds = max(0, int((datetime.now(tz=UTC) - pos.opened_at).total_seconds()))
            held_min = held_seconds // 60

            pnl_d_text = self._colored_pnl(pnl_dollars, self._fmt_decimal(pnl_dollars))
            pnl_p_text = self._colored_pnl(pnl_pct, f"{float(pnl_pct):.1f}%")
            current_text = self._colored_price(pos.symbol, current)

            positions.add_row(
                pos.symbol,
                str(pos.remaining_quantity),
                self._fmt_decimal(pos.entry_price),
                current_text,
                pnl_d_text,
                pnl_p_text,
                f"{r_mult:+.1f}R",
                self._fmt_decimal(pos.stop_price),
                self._fmt_decimal(pos.target_price),
                pos.signal_type.value.split("_")[0].upper()[:6],
                f"{held_min}m",
            )

        # Orders
        orders = self.query_one("#orders-table", DataTable)
        orders.clear(columns=False)
        for order in status.orders[-12:]:
            price = order.limit_price if order.limit_price is not None else order.stop_price
            orders.add_row(
                str(order.order_id),
                order.symbol,
                order.purpose.value,
                order.side,
                str(order.quantity),
                order.status,
                self._fmt_decimal(price or 0),
            )

        # Logs
        log_widget = self.query_one("#logs-panel", RichLog)
        logs = self._runtime.snapshot_logs()
        if self._rendered_log_lines > len(logs):
            self._rendered_log_lines = 0
            log_widget.clear()
        for line in logs[self._rendered_log_lines:]:
            log_widget.write(line)
        self._rendered_log_lines = len(logs)

    # ------------------------------------------------------------------
    # Cards
    # ------------------------------------------------------------------

    def _render_cards(self, status) -> None:
        """Render the 4 compact status cards."""

        # Broker
        broker = self.query_one("#broker-card", Static)
        conn = "[bold green]Connected[/]" if status.connected else "[bold red]Disconnected[/]"
        broker.update(Text.from_markup(
            f"[#8fb7dd]Broker[/]\n{conn}  {self._runtime.settings.ib_host}:{self._runtime.settings.ib_port}"
        ))

        # Market
        mkt = self.query_one("#market-card", Static)
        phase = self._market_phase_markup(self._runtime.market_phase())
        mode = "Paper" if self._runtime.settings.ib_paper else "[bold red]LIVE[/]"
        mkt.update(Text.from_markup(f"[#8fb7dd]Market[/]\n{phase}  {mode}"))

        # Account
        acct = self.query_one("#account-card", Static)
        equity = self._fmt_decimal(status.equity)
        pnl = self._signed_money_markup(status.realized_pnl)
        dd = self._drawdown_pct(status.equity)
        acct.update(Text.from_markup(f"[#8fb7dd]Account[/]\n${equity}  P&L {pnl}  DD {dd}"))

        # Risk
        risk = self.query_one("#risk-card", Static)
        vix_regime = self._runtime._vix_regime
        regime_color = {"greed": "green", "neutral": "cyan", "fear": "yellow", "panic": "red"}.get(vix_regime, "dim")
        pos_count = len(status.positions)
        paused = "" if status.trading_enabled else "  [bold red]PAUSED[/]"
        risk.update(Text.from_markup(
            f"[#8fb7dd]Risk[/]\nVIX [{regime_color}]{vix_regime.upper()}[/]  Pos {pos_count}{paused}"
        ))

    # ------------------------------------------------------------------
    # Market rows
    # ------------------------------------------------------------------

    def _market_rows(self, status) -> list[tuple]:
        """Build market table rows with indicators for every subscribed symbol."""

        rows: list[tuple] = []
        quotes = {q.symbol: q for q in self._runtime.snapshot_quotes()}
        bars_map = self._runtime.bars

        for symbol in status.market_data_symbols:
            quote = quotes.get(symbol)
            indicators = self._runtime.get_indicators(symbol)
            bars = bars_map.get(symbol, [])

            if quote is None:
                rows.append((symbol, "--", "--", "--", "--", "--", "--", "--", "--", "--", "--"))
                continue

            direction = self._price_direction(symbol, quote.last)
            last_text = self._market_text(self._fmt_decimal(quote.last), direction)

            # Chg% from first bar open
            chg_pct = "--"
            if bars and bars[0].open > 0:
                pct = float((quote.last - bars[0].open) / bars[0].open * 100)
                chg_pct = self._colored_pct(pct)

            vol = self._fmt_decimal(quote.volume, places=0)
            spread = self._fmt_decimal(quote.spread())

            # Gap% from indicators or bars
            gap_pct = "--"
            if len(bars) >= 2:
                prev_close = bars[-2].close
                if prev_close > 0:
                    gap = float((bars[-1].open - prev_close) / prev_close * 100)
                    gap_pct = self._colored_pct(gap)

            # RSI
            rsi_val = indicators.get("rsi")
            rsi_text: Text | str = "--"
            if rsi_val is not None:
                rsi_text = Text(f"{rsi_val:.0f}")
                if rsi_val > 70:
                    rsi_text = Text(f"{rsi_val:.0f}", style="bold red")
                elif rsi_val < 30:
                    rsi_text = Text(f"{rsi_val:.0f}", style="bold green")

            # ATR%
            atr_pct = indicators.get("atr_pct")
            atr_text = f"{atr_pct:.1f}" if atr_pct is not None else "--"

            # EMA crossover
            ema_x = indicators.get("ema_crossover", "none")
            ema_text: Text | str = "-"
            if ema_x == "bullish":
                ema_text = Text("\u25b2", style="bold green")
            elif ema_x == "bearish":
                ema_text = Text("\u25bc", style="bold red")

            # Pattern detection from positions
            pattern = "-"
            for pos in status.positions:
                if pos.symbol == symbol:
                    pattern = pos.signal_type.value.split("_")[0].upper()[:6]
                    break

            # Signal
            signal = "-"
            for pos in status.positions:
                if pos.symbol == symbol:
                    signal = Text("LONG", style="bold green")
                    break
            if symbol in status.watchlist and signal == "-":
                signal = Text("WATCH", style="dim cyan")

            rows.append((
                self._market_text(symbol, direction),
                last_text,
                chg_pct,
                vol,
                spread,
                gap_pct,
                rsi_text,
                atr_text,
                ema_text,
                pattern,
                signal,
            ))
        return rows

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _card_markup(self, title: str, primary: str, secondary: str) -> Text:
        """Build consistent markup for a dashboard summary card."""

        return Text.from_markup(f"[#8fb7dd]{title}[/]\n{primary}\n[dim]{secondary}[/]")

    def _fmt_decimal(self, value, places: int = 2) -> str:
        """Format decimals and numbers for compact table display."""

        return f"{float(value):,.{places}f}"

    def _signed_money_markup(self, value: Decimal) -> str:
        """Return signed PnL markup for dashboard cards."""

        if value > 0:
            return f"[bold green]+{self._fmt_decimal(value)}[/]"
        if value < 0:
            return f"[bold red]-{self._fmt_decimal(abs(value))}[/]"
        return self._fmt_decimal(value)

    def _signed_money_text(self, value: Decimal) -> str:
        """Return signed PnL text for non-markup surfaces."""

        if value > 0:
            return f"+{self._fmt_decimal(value)}"
        if value < 0:
            return f"-{self._fmt_decimal(abs(value))}"
        return self._fmt_decimal(value)

    def _market_phase_markup(self, phase: str) -> str:
        """Return colored markup for the current market phase."""

        if phase == "open":
            return "[bold green]Open[/]"
        if phase == "pre-market":
            return "[bold yellow]Pre-Mkt[/]"
        return "[bold red]Closed[/]"

    def _update_header(self, status) -> None:
        """Push live balance and market state into the header bar."""

        phase_text = self._runtime.market_phase_text()
        pnl_text = self._signed_money_text(status.realized_pnl)
        self.title = f"${self._fmt_decimal(status.equity)} | {phase_text} | P&L {pnl_text}"
        self.sub_title = f"{'Connected' if status.connected else 'Disconnected'} | {self._runtime._vix_regime.upper()}"

    def _price_direction(self, symbol: str, last_price: Decimal) -> int:
        """Return whether the last price moved up, down, or stayed flat."""

        previous = self._previous_last_prices.get(symbol)
        self._previous_last_prices[symbol] = last_price
        if previous is None:
            return 0
        if last_price > previous:
            return 1
        if last_price < previous:
            return -1
        return 0

    def _market_text(self, value: str, direction: int) -> Text:
        """Return a colored table cell for market direction."""

        if direction > 0:
            return Text(value, style="bold green")
        if direction < 0:
            return Text(value, style="bold red")
        return Text(value)

    def _colored_pnl(self, value, label: str) -> Text:
        """Return green/red Text based on PnL sign."""

        fval = float(value)
        if fval > 0:
            return Text(label, style="bold green")
        if fval < 0:
            return Text(label, style="bold red")
        return Text(label)

    def _colored_pct(self, pct: float) -> Text:
        """Return green/red Text for a percentage."""

        label = f"{pct:+.1f}%"
        if pct > 0:
            return Text(label, style="green")
        if pct < 0:
            return Text(label, style="red")
        return Text(label)

    def _colored_price(self, symbol: str, price: Decimal) -> Text:
        """Return a price cell colored by direction."""

        direction = self._price_direction(symbol, price)
        return self._market_text(self._fmt_decimal(price), direction)

    def _drawdown_pct(self, equity: Decimal) -> str:
        """Return the current drawdown percentage from session high."""

        high = self._runtime._session_nlv_high
        if high <= 0 or equity >= high:
            return "0.0%"
        dd = float((high - equity) / high * 100)
        return f"{dd:.1f}%"

    def _quote_age_label(self, updated_at: datetime) -> str:
        """Return a short age label for quote freshness."""

        age_seconds = max(0, int((datetime.now(tz=UTC) - updated_at).total_seconds()))
        return f"{age_seconds}s"

    # ------------------------------------------------------------------
    # Focus
    # ------------------------------------------------------------------

    def _toggle_focus(self, panel: str | None) -> None:
        """Toggle full-screen focus mode for the requested panel."""

        self._focused_panel = None if self._focused_panel == panel else panel
        self._apply_focus_mode()

    def _apply_focus_mode(self) -> None:
        """Apply the current full-screen focus selection to the layout."""

        main_column = self.query_one("#main-column")
        logs_column = self.query_one("#logs-column")
        cards = self.query_one("#cards")
        market = self.query_one("#market-table")
        positions = self.query_one("#positions-table")
        orders = self.query_one("#orders-table")

        for widget in (main_column, logs_column, cards, market, positions, orders):
            widget.remove_class("hidden")

        main_column.styles.width = "2fr"
        logs_column.styles.width = "1fr"

        if self._focused_panel is None:
            return
        if self._focused_panel == "logs":
            main_column.add_class("hidden")
            logs_column.styles.width = "1fr"
            return
        if self._focused_panel == "market":
            logs_column.add_class("hidden")
            cards.add_class("hidden")
            positions.add_class("hidden")
            orders.add_class("hidden")
            main_column.styles.width = "1fr"
            return
        if self._focused_panel == "positions":
            logs_column.add_class("hidden")
            cards.add_class("hidden")
            market.add_class("hidden")
            orders.add_class("hidden")
            main_column.styles.width = "1fr"
            return
        if self._focused_panel == "orders":
            logs_column.add_class("hidden")
            cards.add_class("hidden")
            market.add_class("hidden")
            positions.add_class("hidden")
            main_column.styles.width = "1fr"
            return
