"""Runtime safety tests."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from decimal import Decimal
import asyncio

from trader.config import Settings
from trader.models import Bar, ClosedPosition, ManagedPosition, Quote, RiskDecision, SignalDecision, SignalType
from trader.runtime import TradingRuntime


def test_reconcile_broker_positions_pauses_on_mismatch(tmp_path) -> None:
    """Pause trading when broker and local position state disagree."""

    settings = Settings.model_validate(
        {
            "TRADER_STATE_DB": str(tmp_path / "state.sqlite3"),
            "TRADER_FALLBACK_SYMBOLS": "",
        }
    )
    runtime = TradingRuntime(settings=settings, log_sink=deque(maxlen=10))
    runtime.status.trading_enabled = True
    runtime.execution.positions["AMD"] = ManagedPosition(
        symbol="AMD",
        quantity=100,
        remaining_quantity=100,
        entry_price=Decimal("10.00"),
        stop_price=Decimal("9.50"),
        target_price=Decimal("11.00"),
        signal_type=SignalType.ORB,
        opened_at=datetime.now(tz=UTC),
    )
    runtime._broker_positions["AMD"] = Decimal("50")

    runtime._reconcile_broker_positions()

    assert runtime.status.trading_enabled is False
    assert "Position mismatch detected" in runtime.status.last_error
    runtime.state_store.close()


def test_reconcile_broker_positions_accepts_matching_state(tmp_path) -> None:
    """Leave trading enabled when local and broker positions agree."""

    settings = Settings.model_validate(
        {
            "TRADER_STATE_DB": str(tmp_path / "state.sqlite3"),
            "TRADER_FALLBACK_SYMBOLS": "",
        }
    )
    runtime = TradingRuntime(settings=settings, log_sink=deque(maxlen=10))
    runtime.status.trading_enabled = True
    runtime.execution.positions["AMD"] = ManagedPosition(
        symbol="AMD",
        quantity=100,
        remaining_quantity=100,
        entry_price=Decimal("10.00"),
        stop_price=Decimal("9.50"),
        target_price=Decimal("11.00"),
        signal_type=SignalType.ORB,
        opened_at=datetime.now(tz=UTC),
    )
    runtime._broker_positions["AMD"] = Decimal("100")

    runtime._reconcile_broker_positions()

    assert runtime.status.trading_enabled is True
    assert runtime.status.last_error == ""
    runtime.state_store.close()


def test_start_handles_subscription_failure_without_raising(tmp_path) -> None:
    """Surface startup subscription failures as runtime state instead of crashing."""

    settings = Settings.model_validate(
        {
            "TRADER_STATE_DB": str(tmp_path / "state.sqlite3"),
            "TRADER_FALLBACK_SYMBOLS": "AMD",
        }
    )
    runtime = TradingRuntime(settings=settings, log_sink=deque(maxlen=10))

    async def _ok() -> None:
        return None

    async def _boom() -> None:
        raise ConnectionError("IBKR connection dropped while sending a request.")

    runtime.broker.connect = _ok  # type: ignore[method-assign]
    runtime.broker.sync_account = _ok  # type: ignore[method-assign]
    runtime._load_daily_watchlist = _ok  # type: ignore[method-assign]
    runtime._subscribe_fallback_symbols = _boom  # type: ignore[method-assign]
    runtime.broker.refresh_scanner = _ok  # type: ignore[method-assign]
    runtime.broker.disconnect = _ok  # type: ignore[method-assign]

    asyncio.run(runtime.start())

    assert runtime.status.connected is False
    assert runtime.status.last_error == "IBKR connection dropped while sending a request."
    assert runtime._started is False
    runtime.state_store.close()


def test_apply_watchlist_keeps_open_positions_subscribed(tmp_path) -> None:
    """Keep market data for open positions even when the scanner is empty."""

    settings = Settings.model_validate(
        {
            "TRADER_STATE_DB": str(tmp_path / "state.sqlite3"),
            "TRADER_FALLBACK_SYMBOLS": "",
        }
    )
    runtime = TradingRuntime(settings=settings, log_sink=deque(maxlen=10))
    runtime.execution.positions["AMD"] = ManagedPosition(
        symbol="AMD",
        quantity=100,
        remaining_quantity=100,
        entry_price=Decimal("10.00"),
        stop_price=Decimal("9.50"),
        target_price=Decimal("11.00"),
        signal_type=SignalType.ORB,
        opened_at=datetime.now(tz=UTC),
    )
    calls: list[str] = []

    async def _subscribe(symbol: str) -> None:
        calls.append(symbol)

    runtime.broker.subscribe_symbol = _subscribe  # type: ignore[method-assign]

    asyncio.run(runtime._apply_watchlist())

    assert runtime.status.watchlist == []
    assert runtime.status.market_data_symbols == ["AMD"]
    assert calls == ["AMD"]
    runtime.state_store.close()


def test_apply_watchlist_discards_fallback_symbols_when_scanner_returns_live_names(tmp_path) -> None:
    """Replace fallback subscriptions with live scanner symbols as soon as they exist."""

    settings = Settings.model_validate(
        {
            "TRADER_STATE_DB": str(tmp_path / "state.sqlite3"),
            "TRADER_FALLBACK_SYMBOLS": "SPY,QQQ,AMD",
        }
    )
    runtime = TradingRuntime(settings=settings, log_sink=deque(maxlen=10))
    runtime.status.watchlist = ["SPY", "QQQ", "AMD"]
    runtime.status.market_data_symbols = ["SPY", "QQQ", "AMD"]
    runtime._scanner_batch = ["MARA", "PLTR"]
    subscribed: list[str] = []
    unsubscribed: list[str] = []

    async def _subscribe(symbol: str) -> None:
        subscribed.append(symbol)

    async def _unsubscribe(symbol: str) -> None:
        unsubscribed.append(symbol)

    runtime.broker.subscribe_symbol = _subscribe  # type: ignore[method-assign]
    runtime.broker.unsubscribe_symbol = _unsubscribe  # type: ignore[method-assign]

    asyncio.run(runtime._apply_watchlist())

    assert runtime.status.watchlist == ["MARA", "PLTR"]
    assert runtime.status.market_data_symbols == ["MARA", "PLTR"]
    assert unsubscribed == ["SPY", "QQQ", "AMD"]
    assert subscribed == ["MARA", "PLTR"]
    runtime.state_store.close()


def test_fallback_symbols_seed_only_when_no_live_symbols_exist(tmp_path) -> None:
    """Use fallback names only as a bootstrap when there is no live market-data set yet."""

    settings = Settings.model_validate(
        {
            "TRADER_STATE_DB": str(tmp_path / "state.sqlite3"),
            "TRADER_FALLBACK_SYMBOLS": "SPY,QQQ,AMD",
        }
    )
    runtime = TradingRuntime(settings=settings, log_sink=deque(maxlen=10))
    subscribed: list[str] = []

    async def _subscribe(symbol: str) -> None:
        subscribed.append(symbol)

    async def _unsubscribe(symbol: str) -> None:
        raise AssertionError(f"Unexpected unsubscribe for {symbol}")

    runtime.broker.subscribe_symbol = _subscribe  # type: ignore[method-assign]
    runtime.broker.unsubscribe_symbol = _unsubscribe  # type: ignore[method-assign]

    asyncio.run(runtime._subscribe_fallback_symbols())

    assert runtime.status.watchlist == ["SPY", "QQQ", "AMD"]
    assert runtime.status.market_data_symbols == ["SPY", "QQQ", "AMD"]
    assert subscribed == ["SPY", "QQQ", "AMD"]

    subscribed.clear()
    runtime.status.watchlist = ["MARA"]
    runtime.status.market_data_symbols = ["MARA"]

    asyncio.run(runtime._subscribe_fallback_symbols())

    assert subscribed == []
    assert runtime.status.watchlist == ["MARA"]
    assert runtime.status.market_data_symbols == ["MARA"]
    runtime.state_store.close()


def test_evaluate_signal_uses_live_market_phase_instead_of_stale_flag(tmp_path) -> None:
    """Allow signals when the market is open even if the cached flag is stale."""

    settings = Settings.model_validate(
        {
            "TRADER_STATE_DB": str(tmp_path / "state.sqlite3"),
            "TRADER_FALLBACK_SYMBOLS": "",
        }
    )
    runtime = TradingRuntime(settings=settings, log_sink=deque(maxlen=10))
    runtime.status.market_open = False
    runtime.status.equity = Decimal("10000")
    runtime.quotes["AMD"] = Quote(symbol="AMD", last=Decimal("10.00"), updated_at=datetime.now(tz=UTC))
    entered: dict[str, object] = {}

    runtime.market_phase = lambda: "open"  # type: ignore[method-assign]
    runtime.strategy.evaluate = lambda **kwargs: SignalDecision(  # type: ignore[method-assign]
        symbol="AMD",
        signal_type=SignalType.ORB,
        timestamp=datetime.now(tz=UTC),
        entry_price=Decimal("10.00"),
        stop_price=Decimal("9.80"),
        target_price=Decimal("10.40"),
        reason="test",
    )
    runtime.risk.size_signal = lambda **kwargs: RiskDecision(approved=True, quantity=50, reason="approved")  # type: ignore[method-assign]

    async def _enter(signal: SignalDecision, quantity: int) -> None:
        entered["signal"] = signal
        entered["quantity"] = quantity

    runtime.execution.enter_signal = _enter  # type: ignore[method-assign]

    asyncio.run(runtime._evaluate_signal("AMD"))

    assert runtime.status.market_open is True
    assert entered["quantity"] == 50
    runtime.state_store.close()


def test_upsert_bar_retains_enough_history_for_premarket_setups(tmp_path) -> None:
    """Keep a large enough minute window to preserve premarket context."""

    settings = Settings.model_validate(
        {
            "TRADER_STATE_DB": str(tmp_path / "state.sqlite3"),
            "TRADER_FALLBACK_SYMBOLS": "",
        }
    )
    runtime = TradingRuntime(settings=settings, log_sink=deque(maxlen=10))

    for minute in range(550):
        runtime._upsert_bar(
            runtime_bar(
                symbol="AMD",
                timestamp=datetime(2026, 3, 15, 8 + (minute // 60), minute % 60, tzinfo=UTC),
                open_price="10.00",
                high_price="10.10",
                low_price="9.90",
                close_price="10.05",
                volume="10000",
            )
        )

    assert len(runtime.bars["AMD"]) == 500
    runtime.state_store.close()


def test_snapshot_status_includes_closed_positions(tmp_path) -> None:
    """Expose recently closed trades to the TUI status snapshot."""

    settings = Settings.model_validate(
        {
            "TRADER_STATE_DB": str(tmp_path / "state.sqlite3"),
            "TRADER_FALLBACK_SYMBOLS": "",
        }
    )
    runtime = TradingRuntime(settings=settings, log_sink=deque(maxlen=10))
    runtime.execution.closed_positions.append(
        ClosedPosition(
            symbol="AMD",
            quantity=100,
            entry_price=Decimal("10.00"),
            exit_price=Decimal("10.80"),
            realized_pnl=Decimal("80.00"),
            opened_at=datetime.now(tz=UTC),
            closed_at=datetime.now(tz=UTC),
            signal_type=SignalType.ORB,
            exit_reason="target",
        )
    )

    snapshot = runtime.snapshot_status()

    assert snapshot.closed_positions[-1].symbol == "AMD"
    assert snapshot.closed_positions[-1].realized_pnl == Decimal("80.00")
    runtime.state_store.close()


def runtime_bar(
    symbol: str,
    timestamp: datetime,
    open_price: str,
    high_price: str,
    low_price: str,
    close_price: str,
    volume: str,
) -> Bar:
    """Build a bar object for runtime tests."""

    return Bar(
        symbol=symbol,
        timestamp=timestamp,
        open=Decimal(open_price),
        high=Decimal(high_price),
        low=Decimal(low_price),
        close=Decimal(close_price),
        volume=Decimal(volume),
    )
