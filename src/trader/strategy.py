"""Momentum strategy implementation."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, time
from decimal import Decimal
from statistics import median
from zoneinfo import ZoneInfo

from trader.config import Settings
from trader.models import Bar, Quote, SignalDecision, SignalType

_TICK = Decimal("0.01")
_MAX_SPREAD = Decimal("0.05")


class StrategyEngine:
    """Evaluate ORB, bull-flag, and flat-top breakout patterns."""

    def __init__(self, settings: Settings) -> None:
        """Initialize the strategy engine.

        Args:
            settings: Typed application settings.
        """

        self._settings = settings
        self._timezone = ZoneInfo(settings.trader_timezone)

    def evaluate(self, symbol: str, bars: Sequence[Bar], quote: Quote | None) -> SignalDecision | None:
        """Return the highest-priority signal for the given symbol.

        Args:
            symbol: Ticker symbol under evaluation.
            bars: Time-ordered minute bars for the active session.
            quote: Latest quote snapshot for spread and last-price checks.

        Returns:
            A signal decision when one of the supported patterns is valid.
        """

        if quote is None or len(bars) < 3:
            return None
        if quote.spread() > _MAX_SPREAD:
            return None

        ordered = sorted(bars, key=lambda item: item.timestamp)
        return (
            self._detect_orb(symbol=symbol, bars=ordered, quote=quote)
            or self._detect_bull_flag(symbol=symbol, bars=ordered, quote=quote)
            or self._detect_flat_top(symbol=symbol, bars=ordered, quote=quote)
        )

    def _detect_orb(self, symbol: str, bars: Sequence[Bar], quote: Quote) -> SignalDecision | None:
        """Detect an opening-range breakout using premarket and first-minute highs."""

        regular_bars = [bar for bar in bars if self._local_time(bar.timestamp) >= time(9, 30)]
        if len(regular_bars) < 2:
            return None

        # Ross-style ORB: wait for the first regular-hours candle to define the
        # range, then only buy the first clean cross above the premarket / 9:30
        # high while volume expands and the spread is still tradeable.
        first_bar = regular_bars[0]
        latest_bar = regular_bars[-1]
        if self._local_time(latest_bar.timestamp) > self._settings.trader_entry_cutoff:
            return None

        premarket_high = max(
            (bar.high for bar in bars if self._local_time(bar.timestamp) < time(9, 30)),
            default=first_bar.high,
        )
        entry_price = max(first_bar.high, premarket_high) + _TICK
        average_volume = _average_volume(regular_bars[:-1], window=20)
        volume_gate = average_volume > 0 and latest_bar.volume >= average_volume * Decimal("1.5")
        prior_high = regular_bars[-2].high
        crossed = prior_high < entry_price and (latest_bar.high >= entry_price or quote.last >= entry_price)
        if not volume_gate or not crossed:
            return None

        stop_price = first_bar.low - _TICK
        target_price = entry_price + ((entry_price - stop_price) * self._settings.trader_target_r_multiple)
        return SignalDecision(
            symbol=symbol,
            signal_type=SignalType.ORB,
            timestamp=latest_bar.timestamp,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            change_during_buy=_percentage_change(entry_price, first_bar.open),
            reason="Opening-range breakout above premarket/first-minute high.",
        )

    def _detect_bull_flag(self, symbol: str, bars: Sequence[Bar], quote: Quote) -> SignalDecision | None:
        """Detect a bull-flag breakout with a bounded pullback."""

        if len(bars) < 8:
            return None

        # Ross-style bull flag: require a fast pole, a shallow 2-4 candle
        # pullback, then buy the first candle that makes a fresh high above the
        # pullback while volume comes back in.
        window = list(bars[-8:])
        pole_source = window[:5]
        pole_start = pole_source[0].open
        pole_high = max(bar.high for bar in pole_source)
        if pole_start <= 0:
            return None

        pole_gain = (pole_high - pole_start) / pole_start
        if pole_gain < Decimal("0.08"):
            return None

        pullback = window[5:]
        if not 2 <= len(pullback) <= 4:
            return None
        if not all(bar.is_red() for bar in pullback[:-1]):
            return None

        pullback_low = min(bar.low for bar in pullback)
        if pole_high - pullback_low > (pole_high - pole_start) * Decimal("0.5"):
            return None

        resistance = max(bar.high for bar in pullback[:-1] or pullback)
        trigger = resistance + _TICK
        if pullback[-1].high < trigger and quote.last < trigger:
            return None
        average_pullback_volume = _average_volume(pullback[:-1] or pullback, window=3)
        if average_pullback_volume > 0 and pullback[-1].volume < average_pullback_volume:
            return None

        target_price = trigger + ((trigger - (pullback_low - _TICK)) * self._settings.trader_target_r_multiple)
        return SignalDecision(
            symbol=symbol,
            signal_type=SignalType.BULL_FLAG,
            timestamp=pullback[-1].timestamp,
            entry_price=trigger,
            stop_price=pullback_low - _TICK,
            target_price=target_price,
            change_during_buy=_percentage_change(trigger, pole_start),
            reason="Bull-flag breakout after a controlled pullback.",
        )

    def _detect_flat_top(self, symbol: str, bars: Sequence[Bar], quote: Quote) -> SignalDecision | None:
        """Detect a flat-top breakout with repeated resistance tests."""

        if len(bars) < 10:
            return None

        # Ross-style flat top: look for several highs into the same ceiling,
        # confirm the lows are tightening upward underneath it, then buy the
        # first push through resistance with better-than-baseline volume.
        window = list(bars[-10:])
        highs = [bar.high for bar in window]
        resistance = max(highs)
        matching_highs = [high for high in highs if abs(high - resistance) <= Decimal("0.02")]
        if len(matching_highs) < 3:
            return None

        recent_lows = [bar.low for bar in window[-4:]]
        if recent_lows != sorted(recent_lows):
            return None

        trigger = resistance + _TICK
        if window[-1].high < trigger and quote.last < trigger:
            return None
        average_volume = _average_volume(window[:-1], window=5)
        if average_volume > 0 and window[-1].volume < average_volume * Decimal("1.2"):
            return None

        stop_price = min(recent_lows) - _TICK
        target_price = trigger + ((trigger - stop_price) * self._settings.trader_target_r_multiple)
        return SignalDecision(
            symbol=symbol,
            signal_type=SignalType.FLAT_TOP,
            timestamp=window[-1].timestamp,
            entry_price=trigger,
            stop_price=stop_price,
            target_price=target_price,
            change_during_buy=_percentage_change(trigger, window[0].open),
            reason="Flat-top breakout above repeated resistance.",
        )

    def _local_time(self, timestamp: datetime) -> time:
        """Return the bar time converted into the configured trading timezone."""

        return timestamp.astimezone(self._timezone).timetz().replace(tzinfo=None)


def _average_volume(bars: Sequence[Bar], window: int) -> Decimal:
    """Return the average volume across the requested trailing window."""

    if not bars:
        return Decimal("0")
    trailing = list(bars[-window:])
    return sum((bar.volume for bar in trailing), start=Decimal("0")) / Decimal(len(trailing))


def median_bar_range(bars: Sequence[Bar]) -> Decimal:
    """Return the median bar range for a sequence of candles."""

    if not bars:
        return Decimal("0")
    return Decimal(str(median(float(bar.range_size()) for bar in bars)))


def _percentage_change(current: Decimal, reference: Decimal) -> Decimal:
    """Return percent change from a reference price to the current price."""

    if reference <= 0:
        return Decimal("0")
    return ((current - reference) / reference) * Decimal("100")


def should_exit_on_first_red(position_entry_time: datetime, bars: Sequence[Bar], target_filled: bool) -> bool:
    """Return whether the latest bar should force a red-candle exit.

    Args:
        position_entry_time: Timestamp of the original entry.
        bars: Full bar history for the symbol.
        target_filled: Whether the first profit target has already filled.

    Returns:
        ``True`` when the latest completed bar is red and the position has not
        yet locked in target-one profits.
    """

    if target_filled or not bars:
        return False
    completed_bars = [bar for bar in bars if bar.is_complete]
    if not completed_bars:
        return False
    latest = completed_bars[-1]
    return latest.timestamp >= position_entry_time and latest.is_red()
