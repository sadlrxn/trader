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
from textual.widgets import DataTable, Footer, RichLog, Static

from trader.runtime import TradingRuntime

logger = logging.getLogger(__name__)


class TraderTui(App[None]):
    """Trading bot terminal dashboard."""

    CSS = """
    Screen { background: #0a0a12; color: #d0d8e4; }

    /* ── Status Bar ── */
    #status-bar { height: auto; max-height: 5; layout: horizontal; }
    .status-card {
        width: 1fr; height: auto; min-height: 3; max-height: 4; padding: 0 1;
        border: round #1e3a5f; background: #0f1620;
    }

    /* ── Main Layout ── */
    #workspace { height: 1fr; }
    #left-panel { width: 1fr; }
    #right-panel { width: 1fr; }

    /* ── Tables ── */
    DataTable { background: #0c1018; border: solid #1e3a5f; height: 1fr; }
    DataTable > .datatable--header { background: #141c28; text-style: bold; color: #5b9bd5; }
    DataTable > .datatable--even-row { background: #0c1018; }
    DataTable > .datatable--odd-row { background: #101824; }
    DataTable > .datatable--cursor { background: #1e3a5f 40%; }

    #market-table { height: 2fr; }
    #positions-table { height: 1fr; }
    #orders-table { height: 1fr; }

    /* ── Log ── */
    RichLog { background: #0c1018; border: solid #1e3a5f; padding: 0 1; height: 1fr; }

    /* ── Footer ── */
    Footer { background: #141c28; }

    .hidden { display: none; }
    """

    BINDINGS = [
        Binding("q", "quit_bot", "Quit"),
        Binding("p", "pause_trading", "Pause"),
        Binding("r", "resume_trading", "Resume"),
        Binding("l", "toggle_logs_focus", "Logs"),
        Binding("m", "toggle_market_focus", "Market"),
        Binding("s", "toggle_positions_focus", "Positions"),
        Binding("o", "toggle_orders_focus", "Orders"),
        Binding("escape", "clear_focus", "Clear"),
    ]

    def __init__(self, runtime: TradingRuntime) -> None:
        super().__init__()
        self._runtime = runtime
        self._rendered_log_lines = 0
        self._focused_panel: str | None = None
        self._startup_task: asyncio.Task[None] | None = None
        self._prev_prices: dict[str, Decimal] = {}

    def compose(self) -> ComposeResult:
        with Horizontal(id="status-bar"):
            yield Static(id="broker-card", classes="status-card")
            yield Static(id="market-card", classes="status-card")
            yield Static(id="account-card", classes="status-card")
            yield Static(id="risk-card", classes="status-card")
        with Horizontal(id="workspace"):
            with Vertical(id="left-panel"):
                yield DataTable(id="market-table")
                yield DataTable(id="positions-table")
                yield DataTable(id="orders-table")
            with Vertical(id="right-panel"):
                yield RichLog(id="logs-panel", wrap=True, highlight=True, markup=True, max_lines=1000, auto_scroll=True)
        yield Footer()

    async def on_mount(self) -> None:
        mkt = self.query_one("#market-table", DataTable)
        mkt.add_columns("Symbol", "Last", "Chg%", "Vol", "Spread", "Gap%", "RSI", "ATR%", "EMA", "Pattern", "Signal")
        mkt.cursor_type = "row"
        mkt.zebra_stripes = True
        mkt.border_title = "Market"

        pos = self.query_one("#positions-table", DataTable)
        pos.add_columns("Symbol", "Qty", "Entry", "Current", "P&L$", "P&L%", "R-Mult", "Stop", "Target", "Held")
        pos.cursor_type = "row"
        pos.zebra_stripes = True
        pos.border_title = "Positions"

        orders = self.query_one("#orders-table", DataTable)
        orders.add_columns("ID", "Symbol", "Type", "Side", "Qty", "Status", "Price")
        orders.cursor_type = "row"
        orders.zebra_stripes = True
        orders.border_title = "Orders"

        self.query_one("#logs-panel", RichLog).border_title = "Logs"

        self.set_interval(1, self._refresh)
        self._startup_task = asyncio.create_task(self._start_runtime())

    async def on_unmount(self) -> None:
        if self._startup_task:
            self._startup_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._startup_task
        await self._runtime.stop()

    async def _start_runtime(self) -> None:
        try:
            await self._runtime.start()
        except Exception as e:
            logger.exception("Runtime start failed: %s", e)

    # ── Actions ──

    async def action_quit_bot(self) -> None: self.exit()
    async def action_pause_trading(self) -> None: self._runtime.pause_trading()
    async def action_resume_trading(self) -> None: self._runtime.resume_trading()
    async def action_toggle_logs_focus(self) -> None: self._toggle_focus("logs")
    async def action_toggle_market_focus(self) -> None: self._toggle_focus("market")
    async def action_toggle_positions_focus(self) -> None: self._toggle_focus("positions")
    async def action_toggle_orders_focus(self) -> None: self._toggle_focus("orders")
    async def action_clear_focus(self) -> None: self._toggle_focus(None)

    # ── Refresh ──

    def _refresh(self) -> None:
        status = self._runtime.snapshot_status()
        self._refresh_cards(status)
        self._refresh_header(status)
        self._refresh_market(status)
        self._refresh_positions(status)
        self._refresh_orders(status)
        self._refresh_logs()

    def _refresh_cards(self, s) -> None:
        # Broker
        conn = "[bold green]CONNECTED[/]" if s.connected else "[bold red]DISCONNECTED[/]"
        self.query_one("#broker-card", Static).update(Text.from_markup(
            f"[bold #5b9bd5]BROKER[/]  {conn}"))

        # Market
        phase = {"open": "[bold green]OPEN[/]", "pre-market": "[bold yellow]PRE-MKT[/]"}.get(
            self._runtime.market_phase(), "[bold red]CLOSED[/]")
        mode = "[bold red]LIVE[/]" if not self._runtime.settings.ib_paper else "PAPER"
        self.query_one("#market-card", Static).update(Text.from_markup(
            f"[bold #5b9bd5]MARKET[/]  {phase}  {mode}"))

        # Account
        eq = self._fmt(s.equity)
        pnl = self._money(s.realized_pnl)
        dd = f"{float(s.drawdown_pct):.1f}%"
        self.query_one("#account-card", Static).update(Text.from_markup(
            f"[bold #5b9bd5]NLV[/] [bold]${eq}[/]  P&L {pnl}  DD {dd}"))

        # Risk
        vr = s.vix_regime
        vc = {"greed": "green", "euphoria": "green", "neutral": "cyan", "fear": "yellow", "panic": "red"}.get(vr, "dim")
        vix = f"VIX {float(s.vix_value or 0):.1f} [{vc}]{vr.upper()}[/]"
        paused = "  [bold red]PAUSED[/]" if not s.trading_enabled else ""
        self.query_one("#risk-card", Static).update(Text.from_markup(
            f"[bold #5b9bd5]RISK[/]  {vix}  {s.open_position_count}/{s.max_positions} pos{paused}"))

    def _refresh_header(self, s) -> None:
        pnl = f"+{float(s.realized_pnl):.0f}" if s.realized_pnl >= 0 else f"{float(s.realized_pnl):.0f}"
        self.title = f"${self._fmt(s.equity)} | {self._runtime.market_phase_text()} | P&L {pnl}"
        self.sub_title = f"{'Connected' if s.connected else 'Disconnected'} | {s.vix_regime.upper()}"

    def _refresh_market(self, status) -> None:
        table = self.query_one("#market-table", DataTable)
        table.clear(columns=False)
        quotes = {q.symbol: q for q in self._runtime.snapshot_quotes()}
        bars_map = self._runtime.bars

        for symbol in status.market_data_symbols:
            quote = quotes.get(symbol)
            ind = self._runtime.get_indicators(symbol)
            bars = bars_map.get(symbol, [])

            if not quote:
                table.add_row(symbol, *["--"] * 10)
                continue

            d = self._dir(symbol, quote.last)
            last = self._ctext(self._fmt(quote.last), d)

            # Change %
            chg = "--"
            if bars and bars[0].open > 0:
                chg = self._cpct(float((quote.last - bars[0].open) / bars[0].open * 100))

            vol = self._fmt(quote.volume, 0)
            spread = self._fmt(quote.spread())

            # Gap %
            gap = "--"
            if len(bars) >= 2 and bars[-2].close > 0:
                gap = self._cpct(float((bars[-1].open - bars[-2].close) / bars[-2].close * 100))

            # RSI
            rsi = ind.get("rsi")
            rsi_t = "--"
            if rsi is not None:
                style = "bold red" if rsi > 70 else "bold green" if rsi < 30 else ""
                rsi_t = Text(f"{rsi:.0f}", style=style) if style else f"{rsi:.0f}"

            # ATR %
            atr = ind.get("atr_pct")
            atr_t = f"{atr:.1f}" if atr is not None else "--"

            # EMA cross
            ema = ind.get("ema_crossover", "none")
            ema_t = Text("▲", style="bold green") if ema == "bullish" else Text("▼", style="bold red") if ema == "bearish" else "--"

            # Pattern + Signal
            pat, sig = "--", "--"
            for p in status.positions:
                if p.symbol == symbol:
                    pat = p.signal_type.value.split("_")[0].upper()[:6]
                    sig = Text("LONG", style="bold green")
                    break
            if symbol in status.watchlist and sig == "--":
                sig = Text("WATCH", style="dim cyan")

            table.add_row(self._ctext(symbol, d), last, chg, vol, spread, gap, rsi_t, atr_t, ema_t, pat, sig)

    def _refresh_positions(self, status) -> None:
        table = self.query_one("#positions-table", DataTable)
        table.clear(columns=False)
        quotes = {q.symbol: q for q in self._runtime.snapshot_quotes()}

        for pos in status.positions:
            q = quotes.get(pos.symbol)
            cur = q.last if q else pos.entry_price
            pnl_d = (cur - pos.entry_price) * pos.remaining_quantity
            pnl_p = ((cur - pos.entry_price) / pos.entry_price * 100) if pos.entry_price else Decimal("0")
            risk = pos.entry_price - pos.stop_price
            r_mult = float((cur - pos.entry_price) / risk) if risk else 0.0
            held = max(0, int((datetime.now(tz=UTC) - pos.opened_at).total_seconds())) // 60

            table.add_row(
                pos.symbol,
                str(pos.remaining_quantity),
                self._fmt(pos.entry_price),
                self._cpnl(cur - pos.entry_price, self._fmt(cur)),
                self._cpnl(pnl_d, self._fmt(pnl_d)),
                self._cpnl(pnl_p, f"{float(pnl_p):.1f}%"),
                f"{r_mult:+.1f}R",
                self._fmt(pos.stop_price),
                self._fmt(pos.target_price),
                f"{held}m",
            )

    def _refresh_orders(self, status) -> None:
        table = self.query_one("#orders-table", DataTable)
        table.clear(columns=False)
        active = [o for o in status.orders if o.status not in ("Inactive", "Cancelled", "Filled")]
        for o in active[-15:]:
            px = o.limit_price if o.limit_price is not None else o.stop_price
            status_style = "green" if o.status == "Submitted" else "yellow" if o.status == "PreSubmitted" else ""
            status_text = Text(o.status, style=status_style) if status_style else o.status
            table.add_row(str(o.order_id), o.symbol, o.purpose.value, o.side,
                          str(o.quantity), status_text, self._fmt(px or 0))

    def _refresh_logs(self) -> None:
        widget = self.query_one("#logs-panel", RichLog)
        logs = self._runtime.snapshot_logs()
        if self._rendered_log_lines > len(logs):
            self._rendered_log_lines = 0
            widget.clear()
        for line in logs[self._rendered_log_lines:]:
            widget.write(line)
        self._rendered_log_lines = len(logs)

    # ── Focus ──

    def _toggle_focus(self, panel: str | None) -> None:
        self._focused_panel = None if self._focused_panel == panel else panel
        left = self.query_one("#left-panel")
        right = self.query_one("#right-panel")
        status = self.query_one("#status-bar")
        mkt = self.query_one("#market-table")
        pos = self.query_one("#positions-table")
        orders = self.query_one("#orders-table")

        for w in (left, right, status, mkt, pos, orders):
            w.remove_class("hidden")
        left.styles.width = "1fr"
        right.styles.width = "1fr"

        fp = self._focused_panel
        if not fp:
            return
        status.add_class("hidden")
        if fp == "logs":
            left.add_class("hidden")
        elif fp == "market":
            right.add_class("hidden"); pos.add_class("hidden"); orders.add_class("hidden")
        elif fp == "positions":
            right.add_class("hidden"); mkt.add_class("hidden"); orders.add_class("hidden")
            left.styles.width = "1fr"
        elif fp == "orders":
            right.add_class("hidden"); mkt.add_class("hidden"); pos.add_class("hidden")
            left.styles.width = "1fr"

    # ── Helpers ──

    def _fmt(self, v, places: int = 2) -> str:
        return f"{float(v):,.{places}f}"

    def _money(self, v: Decimal) -> str:
        if v > 0: return f"[bold green]+${self._fmt(v)}[/]"
        if v < 0: return f"[bold red]-${self._fmt(abs(v))}[/]"
        return f"${self._fmt(v)}"

    def _dir(self, sym: str, price: Decimal) -> int:
        prev = self._prev_prices.get(sym)
        self._prev_prices[sym] = price
        if prev is None: return 0
        return 1 if price > prev else -1 if price < prev else 0

    def _ctext(self, val: str, d: int) -> Text:
        if d > 0: return Text(val, style="bold green")
        if d < 0: return Text(val, style="bold red")
        return Text(val)

    def _cpnl(self, v, label: str) -> Text:
        f = float(v)
        if f > 0: return Text(label, style="bold green")
        if f < 0: return Text(label, style="bold red")
        return Text(label)

    def _cpct(self, pct: float) -> Text:
        s = f"{pct:+.1f}%"
        if pct > 0: return Text(s, style="green")
        if pct < 0: return Text(s, style="red")
        return Text(s)
