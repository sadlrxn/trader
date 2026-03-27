"""Trading runtime orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict, deque
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd

from trader.broker.ibkr import IBBrokerAdapter
from trader.config import Settings
from trader.execution import ExecutionService
from trader.indicators import compute_indicators
from trader.market import MarketClock
from trader.models import Bar, BrokerEvent, BrokerEventKind, Quote, RuntimeStatus
from trader.risk import RiskManager
from trader.state import StateStore
from trader.strategy import StrategyEngine

logger = logging.getLogger(__name__)


def classify_vix_regime(vix: float) -> str:
    """Classify market regime from VIX level."""

    if vix >= 35:
        return "panic"
    if vix >= 25:
        return "fear"
    if vix < 15:
        return "greed"
    return "neutral"


class TradingRuntime:
    """Coordinate the broker, strategy, execution, RPC, and UI layers."""

    def __init__(self, settings: Settings, log_sink: deque[str]) -> None:
        """Initialize the runtime.

        Args:
            settings: Typed application settings.
            log_sink: Shared in-memory log sink for terminal surfaces.
        """

        self.settings = settings
        self.log_sink = log_sink
        self.market_clock = MarketClock(settings.trader_market_calendar, settings.trader_timezone)
        self.state_store = StateStore(settings.trader_state_db)
        self.status = self.state_store.load_status()
        self.status.positions = self.state_store.load_positions()
        self.broker = IBBrokerAdapter(settings)
        self.execution = ExecutionService(
            broker=self.broker,
            state_store=self.state_store,
            partial_stages_config=settings.trader_partial_stages,
            broker_positions=self._broker_positions,
        )
        self.risk = RiskManager(settings)
        self.strategy = StrategyEngine(settings)
        self.bars: dict[str, list[Bar]] = defaultdict(list)
        self.quotes = {}
        self._scanner_batch: list[str] = []
        self._broker_positions: dict[str, Decimal] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._stop_event = asyncio.Event()
        self._started = False
        self._last_signal_timestamp: dict[str, datetime] = {}
        self._vix_regime: str = "neutral"
        self._session_nlv_high: Decimal = Decimal("0")
        self._indicators: dict[str, dict] = {}

    async def start(self, start_rpc: bool = True) -> None:
        """Start the broker connection and background workers."""

        if self._started:
            return
        self.settings.validate_runtime_mode()
        try:
            await self.broker.connect()
            await self.broker.sync_account()
            await self._load_daily_watchlist()
            await self._subscribe_fallback_symbols()
            await self.broker.refresh_scanner()
            if self.settings.trader_enable_vix_gate:
                await self.broker.subscribe_vix()
        except (ConnectionError, TimeoutError, OSError, RuntimeError) as error:
            await self.broker.disconnect()
            self.status.connected = False
            self.status.last_error = str(error)
            self.state_store.save_status(self.snapshot_status())
            logger.error(self.status.last_error)
            return
        self._started = True
        self._tasks = [
            asyncio.create_task(self._consume_broker_events(), name="broker-events"),
            asyncio.create_task(self._refresh_market_status(), name="market-status"),
            asyncio.create_task(self._refresh_scanner_loop(), name="scanner-loop"),
            asyncio.create_task(self._periodic_maintenance(), name="maintenance"),
        ]
        logger.info(self.market_phase_text())

    async def stop(self) -> None:
        """Stop all workers and disconnect from IBKR."""

        if not self._started:
            return
        self._stop_event.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.broker.disconnect()
        self.status.positions = self.execution.snapshot_positions()
        self.status.orders = self.execution.snapshot_orders()
        self.state_store.save_status(self.status)
        self.state_store.close()
        self._started = False

    def pause_trading(self) -> None:
        """Disable new entries."""

        self.status.trading_enabled = False
        self.state_store.save_status(self.snapshot_status())

    def resume_trading(self) -> None:
        """Enable new entries."""

        self.status.trading_enabled = True
        self.state_store.save_status(self.snapshot_status())

    async def update_stop(self, symbol: str, new_stop: Decimal) -> None:
        """Update a managed stop through the execution service."""

        await self.execution.update_stop(symbol=symbol, stop_price=new_stop)
        self.status.positions = self.execution.snapshot_positions()
        self.state_store.save_status(self.snapshot_status())

    def snapshot_status(self) -> RuntimeStatus:
        """Return a fresh runtime snapshot."""

        self.status.positions = self.execution.snapshot_positions()
        self.status.orders = self.execution.snapshot_orders()
        return self.status.model_copy(deep=True)

    def snapshot_logs(self) -> list[str]:
        """Return the recent terminal logs."""

        return list(self.log_sink)

    def snapshot_quotes(self) -> list[Quote]:
        """Return the latest quotes sorted by watchlist priority."""

        watchlist_order = {symbol: index for index, symbol in enumerate(self.status.watchlist)}
        return sorted(
            self.quotes.values(),
            key=lambda quote: (watchlist_order.get(quote.symbol, 10_000), quote.symbol),
        )

    def get_indicators(self, symbol: str) -> dict:
        """Return the latest computed indicators for a symbol."""

        return self._indicators.get(symbol, {})

    def vwap_for_symbol(self, symbol: str) -> Decimal:
        """Return the rolling 30-minute VWAP for a symbol."""

        bars = self.bars.get(symbol, [])
        if not bars:
            return Decimal("0")
        volume_total = sum((bar.volume for bar in bars), start=Decimal("0"))
        if volume_total <= 0:
            return Decimal("0")
        weighted_total = sum((bar.close * bar.volume for bar in bars), start=Decimal("0"))
        return weighted_total / volume_total

    def market_phase(self) -> str:
        """Return the current market phase for UI surfaces."""

        return self.market_clock.market_phase(premarket_start=self.settings.trader_premarket_start)

    def market_phase_text(self) -> str:
        """Return a human-readable market phase label."""

        return self.market_clock.market_status_text(premarket_start=self.settings.trader_premarket_start)

    async def _consume_broker_events(self) -> None:
        """Process normalized broker events forever."""

        while not self._stop_event.is_set():
            event = await self.broker.next_event()
            await self._handle_broker_event(event)

    async def _handle_broker_event(self, event: BrokerEvent) -> None:
        """Apply one broker event to runtime state."""

        if event.kind is BrokerEventKind.CONNECTED:
            self.status.connected = True
            return
        if event.kind is BrokerEventKind.DISCONNECTED:
            self.status.connected = False
            return
        if event.kind is BrokerEventKind.ERROR:
            self.status.last_error = event.message
            logger.error(event.message)
            return
        if event.kind is BrokerEventKind.ACCOUNT and event.account_tag == "NetLiquidation":
            self.status.equity = Decimal(event.account_value or "0")
            self._check_drawdown(self.status.equity)
            return
        if event.kind is BrokerEventKind.POSITION:
            if event.position_quantity:
                self._broker_positions[event.symbol] = event.position_quantity
            else:
                self._broker_positions.pop(event.symbol, None)
            return
        if event.kind is BrokerEventKind.POSITION_END:
            self._reconcile_broker_positions()
            return
        if event.kind is BrokerEventKind.SCANNER and event.scanner is not None:
            if event.scanner.symbol not in self._scanner_batch:
                self._scanner_batch.append(event.scanner.symbol)
            return
        if event.kind is BrokerEventKind.SCANNER_END:
            try:
                await self.broker.cancel_scanner()
                await self._apply_watchlist()
            except ConnectionError as error:
                self.status.connected = False
                self.status.last_error = str(error)
                self.state_store.save_status(self.snapshot_status())
                logger.error(self.status.last_error)
            return
        if event.kind is BrokerEventKind.QUOTE and event.quote is not None:
            if event.symbol == "VIX" and self.settings.trader_enable_vix_gate:
                vix_val = float(event.quote.last)
                if vix_val > 0:
                    self.broker.last_vix = vix_val
                    self._vix_regime = classify_vix_regime(vix_val)
                return
            self.quotes[event.symbol] = event.quote
            await self.execution.manage_open_position(event.symbol, event.quote, self.bars[event.symbol])
            await self._evaluate_signal(event.symbol)
            return
        if event.kind is BrokerEventKind.BAR and event.bar is not None:
            self._upsert_bar(event.bar)
            self._update_indicators(event.symbol)
            quote = self.quotes.get(event.symbol)
            if quote is not None:
                await self.execution.manage_open_position(event.symbol, quote, self.bars[event.symbol])
                await self._check_trailing_stop(event.symbol, quote)
            await self._evaluate_signal(event.symbol)
            return
        if event.kind is BrokerEventKind.ORDER and event.order is not None:
            pnl_delta = await self.execution.handle_order_update(event.order)
            self.status.realized_pnl += pnl_delta
            self.state_store.save_status(self.snapshot_status())

    async def _refresh_market_status(self) -> None:
        """Refresh the market-open flag on a timer."""

        previous_phase = None
        while not self._stop_event.is_set():
            current_phase = self.market_phase()
            self.status.market_open = current_phase == "open"
            if current_phase != previous_phase:
                logger.info(self.market_phase_text())
                previous_phase = current_phase
            await asyncio.sleep(15)

    async def _refresh_scanner_loop(self) -> None:
        """Refresh the watchlist scanner periodically."""

        while not self._stop_event.is_set():
            await asyncio.sleep(60)
            self._scanner_batch.clear()
            try:
                await self.broker.refresh_scanner()
            except ConnectionError as error:
                self.status.connected = False
                self.status.last_error = str(error)
                self.state_store.save_status(self.snapshot_status())
                logger.error(self.status.last_error)

    async def _apply_watchlist(self) -> None:
        """Subscribe market data for the latest scanner symbols."""

        symbols = self._scanner_batch[: self.settings.trader_scan_max_symbols]
        if not symbols:
            symbols = self.settings.fallback_symbols()
        self.status.watchlist = symbols
        for symbol in symbols:
            await self.broker.subscribe_symbol(symbol)
        self.status.market_data_symbols = list(dict.fromkeys(symbols))
        self._save_daily_watchlist(symbols)
        self.state_store.save_status(self.snapshot_status())
        logger.info("Watchlist updated: %s", ", ".join(symbols))

    async def _subscribe_fallback_symbols(self) -> None:
        """Subscribe fallback symbols immediately so live quotes are visible before scanner output."""

        fallback = self.settings.fallback_symbols()
        if not fallback:
            return
        self.status.market_data_symbols = list(dict.fromkeys(self.status.market_data_symbols + fallback))
        if not self.status.watchlist:
            self.status.watchlist = fallback
        for symbol in fallback:
            await self.broker.subscribe_symbol(symbol)
        self.state_store.save_status(self.snapshot_status())

    def _upsert_bar(self, bar: Bar) -> None:
        """Insert or replace a minute bar while preserving time order."""

        series = self.bars[bar.symbol]
        if series and series[-1].timestamp == bar.timestamp:
            series[-1] = bar
        else:
            if series:
                series[-1].is_complete = True
            series.append(bar)
        del series[:-30]

    async def _evaluate_signal(self, symbol: str) -> None:
        """Evaluate entry logic for one symbol and submit orders when approved."""

        if not self.status.market_open:
            return
        if self.settings.trader_enable_vix_gate and self._vix_regime in ("panic", "fear"):
            logger.info("VIX regime %s -- blocking new entries", self._vix_regime)
            return
        bars = self.bars.get(symbol, [])
        quote = self.quotes.get(symbol)
        signal = self.strategy.evaluate(symbol=symbol, bars=bars, quote=quote)
        if signal is None:
            return
        last_timestamp = self._last_signal_timestamp.get(symbol)
        if last_timestamp == signal.timestamp:
            return
        decision = self.risk.size_signal(
            signal=signal,
            equity=self.status.equity,
            realized_pnl=self.status.realized_pnl,
            open_positions=len(self.execution.positions),
            trading_enabled=self.status.trading_enabled,
        )
        if not decision.approved:
            logger.info("Rejected %s signal for %s: %s", signal.signal_type, symbol, decision.reason)
            return
        self._last_signal_timestamp[symbol] = signal.timestamp
        await self.execution.enter_signal(signal=signal, quantity=decision.quantity)

    def _check_drawdown(self, equity: Decimal) -> None:
        """Pause trading when session drawdown exceeds the configured limit."""

        if equity > self._session_nlv_high:
            self._session_nlv_high = equity
        if self._session_nlv_high > 0:
            drawdown = (self._session_nlv_high - equity) / self._session_nlv_high
            if drawdown >= self.settings.trader_max_drawdown:
                self.pause_trading()
                logger.error("DRAWDOWN LIMIT: %.1f%% from session high", float(drawdown * 100))

    def _update_indicators(self, symbol: str) -> None:
        """Recompute technical indicators for a symbol after a bar update."""

        bars = self.bars.get(symbol, [])
        if len(bars) < 2:
            return
        df = pd.DataFrame(
            [
                {
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": float(b.volume),
                }
                for b in bars
            ]
        )
        try:
            self._indicators[symbol] = compute_indicators(df)
        except Exception:
            logger.debug("Indicator computation failed for %s", symbol, exc_info=True)

    async def _check_trailing_stop(self, symbol: str, quote: Quote) -> None:
        """Convert stop to trailing when price rises far enough above entry after target fill."""

        position = self.execution.positions.get(symbol)
        if position is None or not position.target_filled:
            return
        indicators = self._indicators.get(symbol, {})
        atr_val = indicators.get("atr")
        if atr_val is None or atr_val <= 0:
            return
        threshold = float(position.entry_price) + atr_val * float(self.settings.trader_trailing_stop_atr_multiple)
        if float(quote.last) >= threshold:
            await self.execution.convert_to_trailing_stop(symbol, trail_amount=atr_val)

    async def _periodic_maintenance(self) -> None:
        """Run periodic housekeeping: stale order cancellation."""

        while not self._stop_event.is_set():
            await asyncio.sleep(30)
            try:
                await self.execution.cancel_stale_entries(
                    timeout_seconds=self.settings.trader_stale_order_timeout,
                )
            except Exception:
                logger.debug("Maintenance cycle error", exc_info=True)

    def _reconcile_broker_positions(self) -> None:
        """Pause trading when local state and broker positions disagree."""

        broker_positions = {
            symbol: int(quantity)
            for symbol, quantity in self._broker_positions.items()
            if int(quantity) != 0
        }
        managed_positions = {
            symbol: position.remaining_quantity
            for symbol, position in self.execution.positions.items()
            if position.remaining_quantity != 0
        }
        if broker_positions == managed_positions:
            return
        mismatches = sorted(set(broker_positions) | set(managed_positions))
        details = ", ".join(
            f"{symbol}: broker={broker_positions.get(symbol, 0)} local={managed_positions.get(symbol, 0)}"
            for symbol in mismatches
        )
        self.status.trading_enabled = False
        self.status.last_error = f"Position mismatch detected. {details}"
        self.state_store.save_status(self.snapshot_status())
        logger.error(self.status.last_error)

    async def _load_daily_watchlist(self) -> None:
        """Load the saved daily watchlist and subscribe it before the new scanner pass completes."""

        path = self._today_watchlist_path()
        if not path.exists():
            return
        data = json.loads(path.read_text())
        symbols = [item["symbol"] for item in data.get("watchlist", []) if item.get("symbol")]
        if not symbols:
            return
        self.status.watchlist = symbols
        self.status.market_data_symbols = list(dict.fromkeys(symbols))
        for symbol in symbols:
            await self.broker.subscribe_symbol(symbol)
        logger.info("Loaded daily watchlist from %s", path)

    def _save_daily_watchlist(self, symbols: list[str]) -> None:
        """Persist the active daily watchlist to JSON for reuse across restarts."""

        path = self._today_watchlist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": self.market_clock.now().isoformat(),
            "watchlist": [self._watchlist_record(symbol) for symbol in symbols],
        }
        path.write_text(json.dumps(payload, indent=2))

    def _watchlist_record(self, symbol: str) -> dict[str, str]:
        """Build one persisted watchlist record."""

        quote = self.quotes.get(symbol)
        bars = self.bars.get(symbol, [])
        premarket_bars = [bar for bar in bars if self._is_premarket_bar(bar.timestamp)]
        premarket_high = max((bar.high for bar in premarket_bars), default=Decimal("0"))
        premarket_low = min((bar.low for bar in premarket_bars), default=Decimal("0"))
        latest_bar = bars[-1] if bars else None
        first_bar = bars[0] if bars else None
        percent_change = Decimal("0")
        if latest_bar is not None and first_bar is not None and first_bar.open > 0:
            percent_change = ((latest_bar.close - first_bar.open) / first_bar.open) * Decimal("100")
        return {
            "symbol": symbol,
            "premarket_high": str(premarket_high),
            "premarket_low": str(premarket_low),
            "volume": str((quote.volume if quote is not None else (latest_bar.volume if latest_bar is not None else Decimal("0")))),
            "percent_change": f"{percent_change:.2f}",
        }

    def _is_premarket_bar(self, timestamp: datetime) -> bool:
        """Return whether a bar falls in the configured premarket window."""

        local_time = timestamp.astimezone(self.market_clock._timezone).timetz().replace(tzinfo=None)
        return self.settings.trader_premarket_start <= local_time < self.settings.trader_entry_cutoff.replace(hour=9, minute=30)

    def _today_watchlist_path(self) -> Path:
        """Return the JSON file path for the current trading day watchlist."""

        day_key = self.market_clock.now().strftime("%Y%m%d")
        return self.settings.trader_watchlist_dir / f"watchlist-{day_key}.json"
