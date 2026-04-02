"""Strategy engine tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from trader.config import Settings
from trader.models import Bar, Quote, SignalType
from trader.strategy import StrategyEngine, should_exit_on_first_red


def test_detects_opening_range_breakout() -> None:
    """Emit an ORB signal when price clears the opening range with volume."""

    engine = StrategyEngine(Settings())
    bars = [
        _bar("AMD", datetime(2026, 3, 15, 13, 0, tzinfo=UTC), "9.60", "9.90", "9.55", "9.85", "20000"),
        _bar("AMD", datetime(2026, 3, 15, 13, 30, tzinfo=UTC), "9.90", "10.00", "9.80", "9.95", "50000"),
        _bar("AMD", datetime(2026, 3, 15, 13, 31, tzinfo=UTC), "9.95", "10.05", "9.92", "10.03", "150000"),
    ]
    quote = Quote(
        symbol="AMD",
        bid=Decimal("10.02"),
        ask=Decimal("10.04"),
        last=Decimal("10.03"),
        volume=Decimal("150000"),
        updated_at=datetime.now(tz=UTC),
    )
    signal = engine.evaluate("AMD", bars, quote)
    assert signal is not None
    assert signal.signal_type == SignalType.ORB


def test_detects_opening_range_breakout_with_only_opening_bars() -> None:
    """Allow the ORB to trigger from the first two regular-session bars."""

    engine = StrategyEngine(Settings())
    bars = [
        _bar("AMD", datetime(2026, 3, 15, 13, 30, tzinfo=UTC), "9.90", "10.00", "9.80", "9.95", "50000"),
        _bar("AMD", datetime(2026, 3, 15, 13, 31, tzinfo=UTC), "9.95", "10.05", "9.92", "10.03", "150000"),
    ]
    quote = Quote(
        symbol="AMD",
        bid=Decimal("10.02"),
        ask=Decimal("10.04"),
        last=Decimal("10.03"),
        volume=Decimal("150000"),
        updated_at=datetime.now(tz=UTC),
    )
    signal = engine.evaluate("AMD", bars, quote)
    assert signal is not None
    assert signal.signal_type == SignalType.ORB


def test_detects_bull_flag_breakout() -> None:
    """Emit a bull-flag signal after a strong pole and shallow pullback."""

    engine = StrategyEngine(Settings())
    bars = [
        _bar("AMD", datetime(2026, 3, 15, 13, 30 + index, tzinfo=UTC), *values)
        for index, values in enumerate(
            [
                ("10.00", "10.20", "9.98", "10.18", "100000"),
                ("10.18", "10.45", "10.15", "10.42", "120000"),
                ("10.42", "10.75", "10.40", "10.70", "140000"),
                ("10.70", "10.95", "10.65", "10.90", "130000"),
                ("10.90", "11.05", "10.88", "11.00", "125000"),
                ("11.00", "11.01", "10.85", "10.90", "80000"),
                ("10.90", "10.92", "10.80", "10.84", "70000"),
                ("10.84", "11.03", "10.82", "11.01", "90000"),
            ]
        )
    ]
    quote = Quote(
        symbol="AMD",
        bid=Decimal("11.00"),
        ask=Decimal("11.02"),
        last=Decimal("11.01"),
        volume=Decimal("90000"),
        updated_at=datetime.now(tz=UTC),
    )
    signal = engine.evaluate("AMD", bars, quote)
    assert signal is not None
    assert signal.signal_type == SignalType.BULL_FLAG


def test_detects_first_pullback_breakout() -> None:
    """Emit a first-pullback signal after the opening move gets reclaimed."""

    engine = StrategyEngine(Settings())
    bars = [
        _bar("AMD", datetime(2026, 3, 15, 13, 30, tzinfo=UTC), "10.00", "10.25", "9.99", "10.22", "100000"),
        _bar("AMD", datetime(2026, 3, 15, 13, 31, tzinfo=UTC), "10.22", "10.55", "10.20", "10.50", "125000"),
        _bar("AMD", datetime(2026, 3, 15, 13, 32, tzinfo=UTC), "10.50", "10.75", "10.48", "10.70", "135000"),
        _bar("AMD", datetime(2026, 3, 15, 13, 33, tzinfo=UTC), "10.70", "10.72", "10.50", "10.56", "80000"),
        _bar("AMD", datetime(2026, 3, 15, 13, 34, tzinfo=UTC), "10.56", "10.80", "10.55", "10.77", "120000"),
    ]
    quote = Quote(
        symbol="AMD",
        bid=Decimal("10.74"),
        ask=Decimal("10.80"),
        last=Decimal("10.78"),
        volume=Decimal("120000"),
        updated_at=datetime.now(tz=UTC),
    )
    signal = engine.evaluate("AMD", bars, quote)
    assert signal is not None
    assert signal.signal_type == SignalType.FIRST_PULLBACK


def test_first_red_exit_only_applies_before_target_fill() -> None:
    """Exit on the first red candle only before the first target fills."""

    bars = [
        _bar("AMD", datetime(2026, 3, 15, 14, 0, tzinfo=UTC), "10.00", "10.10", "9.95", "10.08", "50000"),
        _bar("AMD", datetime(2026, 3, 15, 14, 1, tzinfo=UTC), "10.08", "10.12", "10.00", "10.01", "45000"),
    ]
    assert should_exit_on_first_red(bars[0].timestamp, bars, target_filled=False) is True
    assert should_exit_on_first_red(bars[0].timestamp, bars, target_filled=True) is False


def test_first_red_exit_ignores_incomplete_latest_bar() -> None:
    """Do not exit on a still-forming red candle."""

    bars = [
        _bar("AMD", datetime(2026, 3, 15, 14, 0, tzinfo=UTC), "10.00", "10.10", "9.95", "10.08", "50000"),
        _bar(
            "AMD",
            datetime(2026, 3, 15, 14, 1, tzinfo=UTC),
            "10.08",
            "10.12",
            "10.00",
            "10.01",
            "45000",
            is_complete=False,
        ),
    ]
    assert should_exit_on_first_red(bars[0].timestamp, bars, target_filled=False) is False


def test_orb_only_triggers_on_initial_breakout_cross() -> None:
    """Do not keep firing the same ORB after the breakout bar already cleared the level."""

    engine = StrategyEngine(Settings())
    bars = [
        _bar("AMD", datetime(2026, 3, 15, 13, 0, tzinfo=UTC), "9.60", "9.90", "9.55", "9.85", "20000"),
        _bar("AMD", datetime(2026, 3, 15, 13, 30, tzinfo=UTC), "9.90", "10.00", "9.80", "9.95", "50000"),
        _bar("AMD", datetime(2026, 3, 15, 13, 31, tzinfo=UTC), "9.95", "10.05", "9.92", "10.03", "150000"),
        _bar("AMD", datetime(2026, 3, 15, 13, 32, tzinfo=UTC), "10.03", "10.08", "10.00", "10.06", "170000"),
    ]
    quote = Quote(
        symbol="AMD",
        bid=Decimal("10.05"),
        ask=Decimal("10.07"),
        last=Decimal("10.06"),
        volume=Decimal("170000"),
        updated_at=datetime.now(tz=UTC),
    )
    signal = engine.evaluate("AMD", bars, quote)
    assert signal is None or signal.signal_type != SignalType.ORB


def _bar(
    symbol: str,
    timestamp: datetime,
    open_price: str,
    high_price: str,
    low_price: str,
    close_price: str,
    volume: str,
    is_complete: bool = True,
) -> Bar:
    """Build a typed test bar."""

    return Bar(
        symbol=symbol,
        timestamp=timestamp,
        open=Decimal(open_price),
        high=Decimal(high_price),
        low=Decimal(low_price),
        close=Decimal(close_price),
        volume=Decimal(volume),
        is_complete=is_complete,
    )
