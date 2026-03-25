"""Risk engine tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from trader.config import Settings
from trader.models import SignalDecision, SignalType
from trader.risk import RiskManager


def test_size_signal_returns_expected_quantity() -> None:
    """Size a valid signal from the configured risk budget."""

    settings = Settings.model_validate({"TRADER_RISK_PER_TRADE": "0.01"})
    manager = RiskManager(settings)
    signal = SignalDecision(
        symbol="AMD",
        signal_type=SignalType.ORB,
        timestamp=datetime.now(tz=UTC),
        entry_price=Decimal("10.00"),
        stop_price=Decimal("9.50"),
        target_price=Decimal("11.00"),
        reason="test",
    )
    decision = manager.size_signal(
        signal=signal,
        equity=Decimal("10000"),
        realized_pnl=Decimal("0"),
        open_positions=0,
        trading_enabled=True,
    )
    assert decision.approved is True
    assert decision.quantity == 200


def test_size_signal_rejects_when_daily_loss_limit_hit() -> None:
    """Reject new entries after the daily loss threshold is breached."""

    settings = Settings.model_validate({"TRADER_MAX_DAILY_LOSS": "0.02"})
    manager = RiskManager(settings)
    signal = SignalDecision(
        symbol="AMD",
        signal_type=SignalType.ORB,
        timestamp=datetime.now(tz=UTC),
        entry_price=Decimal("10.00"),
        stop_price=Decimal("9.50"),
        target_price=Decimal("11.00"),
        reason="test",
    )
    decision = manager.size_signal(
        signal=signal,
        equity=Decimal("10000"),
        realized_pnl=Decimal("-250"),
        open_positions=0,
        trading_enabled=True,
    )
    assert decision.approved is False


def test_size_signal_rejects_when_equity_is_unavailable() -> None:
    """Reject new entries until the runtime has a real account balance."""

    manager = RiskManager(Settings())
    signal = SignalDecision(
        symbol="AMD",
        signal_type=SignalType.ORB,
        timestamp=datetime.now(tz=UTC),
        entry_price=Decimal("10.00"),
        stop_price=Decimal("9.50"),
        target_price=Decimal("11.00"),
        reason="test",
    )
    decision = manager.size_signal(
        signal=signal,
        equity=Decimal("0"),
        realized_pnl=Decimal("0"),
        open_positions=0,
        trading_enabled=True,
    )
    assert decision.approved is False
