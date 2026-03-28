"""Runtime safety tests."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from decimal import Decimal
import asyncio

from trader.config import Settings
from trader.models import ManagedPosition, SignalType
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
