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

from trader.rpc import RpcServer
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
        height: auto;
        padding: 1 2;
        background: #0f1c2e;
        border: round #29527a;
        color: #f4f7fb;
        margin-bottom: 1;
    }

    .card.-last {
        margin-bottom: 0;
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

    #logs-column {
        width: 1fr;
        margin-right: 1;
    }

    #main-column {
        width: 2fr;
        margin-right: 1;
    }

    #side-column {
        width: 1fr;
    }

    #cards {
        height: 7;
        margin-bottom: 1;
    }

    #market-table {
        height: 1fr;
        margin-bottom: 1;
    }

    #positions-table {
        height: 1fr;
    }

    #logs-panel {
        height: 1fr;
    }

    #watchlist-table {
        height: 1fr;
        margin-bottom: 1;
    }

    #orders-table {
        height: 1fr;
        margin-bottom: 1;
    }

    #rpc-panel {
        height: auto;
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
        Binding("w", "toggle_watchlist_focus", "Watchlist"),
        Binding("o", "toggle_orders_focus", "Orders"),
        Binding("g", "toggle_grpc_focus", "gRPC"),
        Binding("escape", "clear_focus", "Clear"),
    ]

    def __init__(self, runtime: TradingRuntime, rpc_server: RpcServer | None) -> None:
        """Initialize the Textual application.

        Args:
            runtime: Shared trading runtime.
            rpc_server: Optional embedded RPC server.
        """

        super().__init__()
        self._runtime = runtime
        self._rpc_server = rpc_server
        self._rendered_log_lines = 0
        self._focused_panel: str | None = None
        self._startup_task: asyncio.Task[None] | None = None
        self._previous_last_prices: dict[str, Decimal] = {}

    def compose(self) -> ComposeResult:
        """Compose the Textual layout."""

        yield Header(show_clock=True)
        yield Vertical(
            Horizontal(
                RichLog(id="logs-panel", wrap=False, highlight=True, markup=True),
                Vertical(
                    Horizontal(
                        Static(id="connection-card", classes="card"),
                        Static(id="session-card", classes="card"),
                        Static(id="account-card", classes="card"),
                        Static(id="control-card", classes="card -last"),
                        id="cards",
                    ),
                    DataTable(id="market-table"),
                    DataTable(id="positions-table"),
                    id="main-column",
                ),
                Vertical(
                    DataTable(id="watchlist-table"),
                    DataTable(id="orders-table"),
                    Static(id="rpc-panel", classes="card -last"),
                    id="side-column",
                ),
                id="workspace",
            ),
            id="root",
        )
        yield Footer()

    async def on_mount(self) -> None:
        """Start runtime services and initialize tables."""

        watchlist = self.query_one("#watchlist-table", DataTable)
        watchlist.add_columns("Symbol")
        watchlist.cursor_type = "row"
        watchlist.zebra_stripes = True
        watchlist.border_title = "Scanner Watchlist"

        market = self.query_one("#market-table", DataTable)
        market.add_columns("Symbol", "Last", "Bid", "Ask", "Spread", "Volume", "VWAP", "Age")
        market.cursor_type = "row"
        market.zebra_stripes = True
        market.border_title = "Live Market Data"

        positions = self.query_one("#positions-table", DataTable)
        positions.add_columns("Symbol", "Qty", "Entry", "Stop", "Target", "PnL", "Pattern")
        positions.cursor_type = "row"
        positions.zebra_stripes = True
        positions.border_title = "Open Positions"

        orders = self.query_one("#orders-table", DataTable)
        orders.add_columns("ID", "Symbol", "Type", "Side", "Qty", "Status", "Px")
        orders.cursor_type = "row"
        orders.zebra_stripes = True
        orders.border_title = "Orders"

        log_widget = self.query_one("#logs-panel", RichLog)
        log_widget.border_title = "Bot Logs"
        rpc_panel = self.query_one("#rpc-panel", Static)
        rpc_panel.border_title = "gRPC Control"
        self.set_interval(1, self._refresh_view)
        self._startup_task = asyncio.create_task(self._startup_services(), name="tui-startup")

    async def on_unmount(self) -> None:
        """Stop runtime services during shutdown."""

        if self._startup_task is not None:
            self._startup_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._startup_task
        if self._rpc_server is not None:
            await self._rpc_server.stop()
        await self._runtime.stop()

    async def _startup_services(self) -> None:
        """Start the runtime services without blocking the initial TUI render."""

        try:
            await self._runtime.start()
            if self._rpc_server is not None:
                await self._rpc_server.start()
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

    async def action_toggle_watchlist_focus(self) -> None:
        """Toggle full-screen focus for the watchlist panel."""

        self._toggle_focus("watchlist")

    async def action_toggle_orders_focus(self) -> None:
        """Toggle full-screen focus for the orders panel."""

        self._toggle_focus("orders")

    async def action_toggle_grpc_focus(self) -> None:
        """Toggle full-screen focus for the gRPC panel."""

        self._toggle_focus("grpc")

    async def action_clear_focus(self) -> None:
        """Clear any active full-screen focus mode."""

        self._toggle_focus(None)

    def _refresh_view(self) -> None:
        """Redraw the status, tables, and logs."""

        status = self._runtime.snapshot_status()
        self._render_cards(status)
        self._update_header(status)

        watchlist = self.query_one("#watchlist-table", DataTable)
        watchlist.clear(columns=False)
        for symbol in status.watchlist:
            watchlist.add_row(symbol)

        market = self.query_one("#market-table", DataTable)
        market.clear(columns=False)
        for row in self._market_rows()[:20]:
            market.add_row(
                *row,
            )

        positions = self.query_one("#positions-table", DataTable)
        positions.clear(columns=False)
        for position in status.positions:
            positions.add_row(
                position.symbol,
                str(position.remaining_quantity),
                self._fmt_decimal(position.entry_price),
                self._fmt_decimal(position.stop_price),
                self._fmt_decimal(position.target_price),
                self._fmt_decimal(position.realized_pnl),
                position.signal_type.value,
            )

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

        log_widget = self.query_one("#logs-panel", RichLog)
        logs = self._runtime.snapshot_logs()
        if self._rendered_log_lines > len(logs):
            self._rendered_log_lines = 0
            log_widget.clear()
        for line in logs[self._rendered_log_lines :]:
            log_widget.write(line)
        self._rendered_log_lines = len(logs)

    def _render_cards(self, status) -> None:
        """Render the summary cards across the top of the dashboard."""

        connection = self.query_one("#connection-card", Static)
        connection.update(
            self._card_markup(
                title="Broker",
                primary="[bold green]Connected[/bold green]" if status.connected else "[bold red]Disconnected[/bold red]",
                secondary=f"Host {self._runtime.settings.ib_host}:{self._runtime.settings.ib_port}",
            )
        )

        market_label = self._market_phase_markup(self._runtime.market_phase())
        session = self.query_one("#session-card", Static)
        session.update(
            self._card_markup(
                title="Market Status",
                primary=market_label,
                secondary=f"Mode {'Paper' if self._runtime.settings.ib_paper else 'Live'}",
            )
        )

        account = self.query_one("#account-card", Static)
        account.update(
            self._card_markup(
                title="Balance",
                primary=f"[bold]{self._fmt_decimal(status.equity)}[/bold]",
                secondary=f"P&L {self._signed_money_markup(status.realized_pnl)}",
            )
        )

        error_text = status.last_error if status.last_error else "No active errors"
        control = self.query_one("#control-card", Static)
        control.update(
            self._card_markup(
                title="Controls",
                primary="[bold cyan]Trading Enabled[/bold cyan]" if status.trading_enabled else "[bold red]Trading Paused[/bold red]",
                secondary=error_text,
            )
        )
        rpc_panel = self.query_one("#rpc-panel", Static)
        if self._rpc_server is None:
            rpc_panel.update(
                self._card_markup(
                    title="gRPC",
                    primary="[bold yellow]Disabled[/bold yellow]",
                    secondary="Market data flows directly from the IBKR client in bot mode.",
                )
            )
            return
        rpc_panel.update(
            self._card_markup(
                title="gRPC",
                primary=f"[bold]{self._runtime.settings.trader_grpc_host}:{self._runtime.settings.trader_grpc_port}[/bold]",
                secondary="TraderControl/Health\nTraderControl/GetState\nTraderControl/WatchRuntime\nTraderControl/PauseTrading\nTraderControl/ResumeTrading\nTraderControl/UpdateStop",
            )
        )

    def _card_markup(self, title: str, primary: str, secondary: str) -> Text:
        """Build consistent markup for a dashboard summary card."""

        return Text.from_markup(f"[#8fb7dd]{title}[/#8fb7dd]\n{primary}\n[dim]{secondary}[/dim]")

    def _fmt_decimal(self, value, places: int = 2) -> str:
        """Format decimals and numbers for compact table display."""

        return f"{float(value):,.{places}f}"

    def _signed_money_text(self, value: Decimal) -> str:
        """Return signed PnL text for non-markup surfaces."""

        if value > 0:
            return f"+{self._fmt_decimal(value)}"
        if value < 0:
            return f"-{self._fmt_decimal(abs(value))}"
        return self._fmt_decimal(value)

    def _signed_money_markup(self, value: Decimal) -> str:
        """Return signed PnL markup for dashboard cards."""

        if value > 0:
            return f"[bold green]+{self._fmt_decimal(value)}[/bold green]"
        if value < 0:
            return f"[bold red]-{self._fmt_decimal(abs(value))}[/bold red]"
        return self._fmt_decimal(value)

    def _market_phase_markup(self, phase: str) -> str:
        """Return colored markup for the current market phase."""

        if phase == "open":
            return "[bold green]Open[/bold green]"
        if phase == "pre-market":
            return "[bold yellow]Pre-Market[/bold yellow]"
        return "[bold red]Closed[/bold red]"

    def _update_header(self, status) -> None:
        """Push live balance and market state into the header bar."""

        phase_text = self._runtime.market_phase_text()
        pnl_text = self._signed_money_text(status.realized_pnl)
        self.title = f"Balance {self._fmt_decimal(status.equity)} | Market Status {phase_text} | P&L {pnl_text}"
        self.sub_title = f"Broker {'Connected' if status.connected else 'Disconnected'}"

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

    def _quote_age_label(self, updated_at: datetime) -> str:
        """Return a short age label for quote freshness."""

        age_seconds = max(0, int((datetime.now(tz=UTC) - updated_at).total_seconds()))
        return f"{age_seconds}s"

    def _market_rows(self) -> list[tuple[object, object, object, object, str, str, str, str]]:
        """Build market table rows from subscribed symbols and live quote state."""

        rows: list[tuple[object, object, object, object, str, str, str, str]] = []
        quotes = {quote.symbol: quote for quote in self._runtime.snapshot_quotes()}
        for symbol in self._runtime.snapshot_status().market_data_symbols:
            quote = quotes.get(symbol)
            vwap = self._runtime.vwap_for_symbol(symbol)
            if quote is None:
                rows.append((symbol, "--", "--", "--", "--", "--", self._fmt_decimal(vwap), "awaiting"))
                continue
            direction = self._price_direction(symbol, quote.last)
            rows.append(
                (
                    self._market_text(symbol, direction),
                    self._market_text(self._fmt_decimal(quote.last), direction),
                    self._market_text(self._fmt_decimal(quote.bid), direction),
                    self._market_text(self._fmt_decimal(quote.ask), direction),
                    self._fmt_decimal(quote.spread()),
                    self._fmt_decimal(quote.volume, places=0),
                    self._fmt_decimal(vwap),
                    self._quote_age_label(quote.updated_at),
                )
            )
        return rows

    def _toggle_focus(self, panel: str | None) -> None:
        """Toggle full-screen focus mode for the requested panel."""

        self._focused_panel = None if self._focused_panel == panel else panel
        self._apply_focus_mode()

    def _apply_focus_mode(self) -> None:
        """Apply the current full-screen focus selection to the layout."""

        logs_column = self.query_one("#logs-panel")
        main_column = self.query_one("#main-column")
        side_column = self.query_one("#side-column")
        cards = self.query_one("#cards")
        market = self.query_one("#market-table")
        positions = self.query_one("#positions-table")
        watchlist = self.query_one("#watchlist-table")
        orders = self.query_one("#orders-table")
        grpc_panel = self.query_one("#rpc-panel")

        for widget in (logs_column, main_column, side_column, cards, market, positions, watchlist, orders, grpc_panel):
            widget.remove_class("hidden")

        logs_column.styles.width = "1fr"
        main_column.styles.width = "2fr"
        side_column.styles.width = "1fr"

        if self._focused_panel is None:
            return
        if self._focused_panel == "logs":
            main_column.add_class("hidden")
            side_column.add_class("hidden")
            logs_column.styles.width = "1fr"
            return
        if self._focused_panel == "market":
            logs_column.add_class("hidden")
            side_column.add_class("hidden")
            cards.add_class("hidden")
            positions.add_class("hidden")
            main_column.styles.width = "1fr"
            return
        if self._focused_panel == "positions":
            logs_column.add_class("hidden")
            side_column.add_class("hidden")
            cards.add_class("hidden")
            market.add_class("hidden")
            main_column.styles.width = "1fr"
            return
        if self._focused_panel == "watchlist":
            logs_column.add_class("hidden")
            main_column.add_class("hidden")
            orders.add_class("hidden")
            grpc_panel.add_class("hidden")
            side_column.styles.width = "1fr"
            return
        if self._focused_panel == "orders":
            logs_column.add_class("hidden")
            main_column.add_class("hidden")
            watchlist.add_class("hidden")
            grpc_panel.add_class("hidden")
            side_column.styles.width = "1fr"
            return
        if self._focused_panel == "grpc":
            logs_column.add_class("hidden")
            main_column.add_class("hidden")
            watchlist.add_class("hidden")
            orders.add_class("hidden")
            side_column.styles.width = "1fr"
