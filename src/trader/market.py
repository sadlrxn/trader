"""Market hours utilities."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd


class MarketClock:
    """Determine whether the configured exchange is currently open."""

    def __init__(self, calendar_name: str, timezone_name: str) -> None:
        """Initialize the market clock.

        Args:
            calendar_name: Exchange calendar identifier such as ``XNYS``.
            timezone_name: Operator timezone used for display and cutoffs.
        """

        self._calendar = xcals.get_calendar(calendar_name)
        self._timezone = ZoneInfo(timezone_name)

    def now(self) -> datetime:
        """Return the current wall-clock time in the configured timezone."""

        return datetime.now(tz=self._timezone)

    def is_market_open(self, when: datetime | None = None) -> bool:
        """Return whether the exchange is open at the given time."""

        current = when or self.now()
        timestamp = pd.Timestamp(current)
        return bool(self._calendar.is_open_on_minute(timestamp))

    def market_status_text(self, when: datetime | None = None) -> str:
        """Return a human-readable market status label."""

        return "Market Open" if self.is_market_open(when) else "Market Closed"
