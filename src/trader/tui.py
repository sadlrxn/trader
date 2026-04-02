"""Textual terminal UI for the trading bot."""
from __future__ import annotations
import asyncio, logging
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

def _c(val, label: str) -> Text:
    """Color text green/red by sign."""
    f = float(val)
    return Text(label, style="bold green" if f > 0 else "bold red" if f < 0 else "")

def _fmt(v, p: int = 2) -> str:
    return f"{float(v):,.{p}f}"


class TraderTui(App[None]):
    CSS = """
    Screen { background: #0a0a12; color: #d0d8e4; }
    #status-bar { height: 3; }
    .status-card { width: 1fr; height: 3; padding: 0 1; background: #0f1620; border: round #1e3a5f; }
    #workspace { height: 1fr; }
    #left-panel, #right-panel { width: 1fr; }
    DataTable { background: #0c1018; border: solid #1e3a5f; height: 1fr; }
    DataTable > .datatable--header { background: #141c28; text-style: bold; color: #5b9bd5; }
    DataTable > .datatable--even-row { background: #0c1018; }
    DataTable > .datatable--odd-row { background: #101824; }
    DataTable > .datatable--cursor { background: #1e3a5f 40%; }
    #market-table { height: 2fr; }
    #logs-panel { height: 2fr; }
    RichLog { background: #0c1018; border: solid #1e3a5f; height: 1fr; }
    Footer { background: #141c28; }
    """
    BINDINGS = [
        Binding("q", "quit_bot", "Quit"), Binding("p", "pause_trading", "Pause"),
        Binding("r", "resume_trading", "Resume"), Binding("l", "focus('logs')", "Logs"),
        Binding("m", "focus('market')", "Market"), Binding("s", "focus('positions')", "Positions"),
        Binding("c", "focus('closed')", "Closed"), Binding("o", "focus('orders')", "Orders"), Binding("escape", "focus('')", "Clear"),
    ]

    def __init__(self, runtime: TradingRuntime) -> None:
        super().__init__()
        self._rt = runtime
        self._log_count = 0
        self._focus: str = ""
        self._prev: dict[str, Decimal] = {}

    def compose(self) -> ComposeResult:
        with Horizontal(id="status-bar"):
            yield Static(id="c-broker", classes="status-card")
            yield Static(id="c-market", classes="status-card")
            yield Static(id="c-account", classes="status-card")
            yield Static(id="c-risk", classes="status-card")
        with Horizontal(id="workspace"):
            with Vertical(id="left-panel"):
                yield DataTable(id="market-table")
                yield DataTable(id="positions-table")
            with Vertical(id="right-panel"):
                yield DataTable(id="closed-table")
                yield DataTable(id="orders-table")
                yield RichLog(id="logs-panel", wrap=True, highlight=True, markup=True, max_lines=1000, auto_scroll=True)
        yield Footer()

    async def on_mount(self) -> None:
        for tid, cols, title in [
            ("market-table", ["Symbol", "Last", "Chg%", "Vol", "Spread", "Gap%", "RSI", "ATR%", "EMA", "Pattern", "Signal"], "Market"),
            ("positions-table", ["Symbol", "Qty", "Entry", "Current", "P&L$", "P&L%", "R-Mult", "Stop", "Target", "Held"], "Open Positions"),
            ("closed-table", ["Symbol", "Qty", "Entry", "Exit", "P&L$", "P&L%", "Signal", "Reason", "Closed"], "Closed Positions"),
            ("orders-table", ["ID", "Symbol", "Type", "Side", "Qty", "Filled", "Status", "Price", "Avg Fill"], "Orders"),
        ]:
            t = self.query_one(f"#{tid}", DataTable)
            t.add_columns(*cols); t.cursor_type = "row"; t.zebra_stripes = True; t.border_title = title
        self.query_one("#logs-panel", RichLog).border_title = "Logs"
        self.set_interval(1, self._refresh)
        asyncio.create_task(self._start())

    async def on_unmount(self) -> None:
        await self._rt.stop()

    async def _start(self) -> None:
        try: await self._rt.start()
        except Exception as e: logger.exception("Runtime failed: %s", e)

    async def action_quit_bot(self) -> None: self.exit()
    async def action_pause_trading(self) -> None: self._rt.pause_trading()
    async def action_resume_trading(self) -> None: self._rt.resume_trading()

    def action_focus(self, panel: str) -> None:
        self._focus = "" if self._focus == panel else panel
        widgets = {
            "status": self.query_one("#status-bar"),
            "left": self.query_one("#left-panel"), "right": self.query_one("#right-panel"),
            "market": self.query_one("#market-table"), "positions": self.query_one("#positions-table"),
            "closed": self.query_one("#closed-table"), "orders": self.query_one("#orders-table"),
        }
        for w in widgets.values(): w.display = True
        if not self._focus: return
        widgets["status"].display = False
        hide = {
            "logs": ["left"],
            "market": ["right", "positions"],
            "positions": ["right", "market"],
            "closed": ["left", "orders"],
            "orders": ["left", "closed"],
        }
        for k in hide.get(self._focus, []): widgets[k].display = False

    def _refresh(self) -> None:
        s = self._rt.snapshot_status()
        # Cards
        conn = "[bold green]CONNECTED[/]" if s.connected else "[bold red]DISCONNECTED[/]"
        self.query_one("#c-broker", Static).update(Text.from_markup(f"[bold #5b9bd5]BROKER[/]  {conn}"))
        phase = {"open": "[bold green]OPEN[/]", "pre-market": "[bold yellow]PRE-MKT[/]"}.get(self._rt.market_phase(), "[bold red]CLOSED[/]")
        mode = "[bold red]LIVE[/]" if not self._rt.settings.ib_paper else "PAPER"
        self.query_one("#c-market", Static).update(Text.from_markup(f"[bold #5b9bd5]MARKET[/]  {phase}  {mode}"))
        pnl_mk = f"[bold green]+${_fmt(s.realized_pnl)}[/]" if s.realized_pnl > 0 else f"[bold red]-${_fmt(abs(s.realized_pnl))}[/]" if s.realized_pnl < 0 else f"${_fmt(s.realized_pnl)}"
        self.query_one("#c-account", Static).update(Text.from_markup(f"[bold #5b9bd5]NLV[/] [bold]${_fmt(s.equity)}[/]  P&L {pnl_mk}  DD {float(s.drawdown_pct):.1f}%"))
        vc = {"calm": "green", "neutral": "cyan", "fear": "yellow", "panic": "red"}.get(s.vix_regime, "dim")
        paused = "  [bold red]PAUSED[/]" if not s.trading_enabled else ""
        self.query_one("#c-risk", Static).update(Text.from_markup(f"[bold #5b9bd5]RISK[/]  VIX {float(s.vix_value or 0):.1f} [{vc}]{s.vix_regime.upper()}[/]  {s.open_position_count}/{s.max_positions} pos{paused}"))
        # Header
        pnl_h = f"+{float(s.realized_pnl):.0f}" if s.realized_pnl >= 0 else f"{float(s.realized_pnl):.0f}"
        self.title = f"${_fmt(s.equity)} | {self._rt.market_phase_text()} | P&L {pnl_h}"
        self.sub_title = f"{'Connected' if s.connected else 'Disconnected'} | {s.vix_regime.upper()}"
        # Market
        mt = self.query_one("#market-table", DataTable); mt.clear(columns=False)
        quotes = {q.symbol: q for q in self._rt.snapshot_quotes()}
        bars_map = self._rt.bars
        for sym in s.market_data_symbols:
            q = quotes.get(sym)
            if not q: mt.add_row(sym, *["--"] * 10); continue
            ind = self._rt.get_indicators(sym); bars = bars_map.get(sym, [])
            d = self._dir(sym, q.last)
            chg = self._pct(float((q.last - bars[0].open) / bars[0].open * 100)) if bars and bars[0].open > 0 else "--"
            gap = self._pct(float((bars[-1].open - bars[-2].close) / bars[-2].close * 100)) if len(bars) >= 2 and bars[-2].close > 0 else "--"
            rsi = ind.get("rsi")
            rsi_t = Text(f"{rsi:.0f}", style="bold red" if rsi > 70 else "bold green" if rsi < 30 else "") if rsi is not None else "--"
            atr = ind.get("atr_pct"); ema = ind.get("ema_crossover", "none")
            ema_t = Text("▲", style="bold green") if ema == "bullish" else Text("▼", style="bold red") if ema == "bearish" else "--"
            pat, sig = "--", Text("WATCH", style="dim cyan") if sym in s.watchlist else "--"
            for p in s.positions:
                if p.symbol == sym: pat = p.signal_type.value.split("_")[0].upper()[:6]; sig = Text("LONG", style="bold green"); break
            mt.add_row(self._ct(sym, d), self._ct(_fmt(q.last), d), chg, _fmt(q.volume, 0), _fmt(q.spread()), gap, rsi_t, f"{atr:.1f}" if atr else "--", ema_t, pat, sig)
        # Positions
        pt = self.query_one("#positions-table", DataTable); pt.clear(columns=False)
        for p in s.positions:
            q = quotes.get(p.symbol); cur = q.last if q else p.entry_price
            pnl_d = (cur - p.entry_price) * p.remaining_quantity
            pnl_p = ((cur - p.entry_price) / p.entry_price * 100) if p.entry_price else Decimal(0)
            risk = p.entry_price - p.stop_price; rm = float((cur - p.entry_price) / risk) if risk else 0.0
            held = max(0, int((datetime.now(tz=UTC) - p.opened_at).total_seconds())) // 60
            pt.add_row(p.symbol, str(p.remaining_quantity), _fmt(p.entry_price), _c(cur - p.entry_price, _fmt(cur)),
                       _c(pnl_d, _fmt(pnl_d)), _c(pnl_p, f"{float(pnl_p):.1f}%"), f"{rm:+.1f}R", _fmt(p.stop_price), _fmt(p.target_price), f"{held}m")
        # Closed positions
        ct = self.query_one("#closed-table", DataTable); ct.clear(columns=False)
        for p in reversed(s.closed_positions[-15:]):
            cost_basis = p.entry_price * p.quantity
            pnl_pct = (p.realized_pnl / cost_basis * 100) if cost_basis else Decimal("0")
            closed_at = p.closed_at.astimezone(UTC).strftime("%H:%M:%S")
            ct.add_row(
                p.symbol,
                str(p.quantity),
                _fmt(p.entry_price),
                _fmt(p.exit_price),
                _c(p.realized_pnl, _fmt(p.realized_pnl)),
                _c(pnl_pct, f"{float(pnl_pct):.1f}%"),
                p.signal_type.value.replace("_", " "),
                p.exit_reason,
                closed_at,
            )
        # Orders
        ot = self.query_one("#orders-table", DataTable); ot.clear(columns=False)
        for o in s.orders[-20:]:
            px = o.limit_price if o.limit_price is not None else o.stop_price
            if o.status == "Filled":
                st = Text(o.status, style="bold green")
            elif o.status in {"Submitted", "PreSubmitted"}:
                st = Text(o.status, style="bold yellow")
            elif o.status in {"Cancelled", "Inactive", "ApiCancelled"}:
                st = Text(o.status, style="bold red")
            else:
                st = Text(o.status)
            avg_fill = "--" if o.avg_fill_price <= 0 else _fmt(o.avg_fill_price)
            ot.add_row(
                str(o.order_id),
                o.symbol,
                o.purpose.value,
                o.side,
                str(o.quantity),
                _fmt(o.filled_quantity, 0),
                st,
                _fmt(px or 0),
                avg_fill,
            )
        # Logs
        lw = self.query_one("#logs-panel", RichLog); logs = self._rt.snapshot_logs()
        if self._log_count > len(logs): self._log_count = 0; lw.clear()
        for line in logs[self._log_count:]: lw.write(line)
        self._log_count = len(logs)

    def _dir(self, sym: str, price: Decimal) -> int:
        prev = self._prev.get(sym); self._prev[sym] = price
        return 0 if prev is None else 1 if price > prev else -1 if price < prev else 0

    def _ct(self, v: str, d: int) -> Text:
        return Text(v, style="bold green" if d > 0 else "bold red" if d < 0 else "")

    def _pct(self, p: float) -> Text:
        return Text(f"{p:+.1f}%", style="green" if p > 0 else "red" if p < 0 else "")
