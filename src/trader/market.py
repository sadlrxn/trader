"""Market hours utilities."""

from __future__ import annotations

from datetime import datetime, time
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

    def market_phase(
        self, when: datetime | None = None, premarket_start: time | None = None
    ) -> str:
        """Return the current market phase: open, pre-market, or closed."""

        current = (when or self.now()).astimezone(self._timezone)
        session = pd.Timestamp(current.date())
        if not self._calendar.is_session(session):
            return "closed"
        current_timestamp = pd.Timestamp(current)
        session_open = self._calendar.session_open(session).tz_convert(self._timezone)
        session_close = self._calendar.session_close(session).tz_convert(self._timezone)
        if session_open <= current_timestamp <= session_close:
            return "open"
        if premarket_start is not None:
            premarket_timestamp = pd.Timestamp(
                current.replace(
                    hour=premarket_start.hour,
                    minute=premarket_start.minute,
                    second=0,
                    microsecond=0,
                )
            )
            if premarket_timestamp <= current_timestamp < session_open:
                return "pre-market"
        return "closed"

    def market_status_text(
        self, when: datetime | None = None, premarket_start: time | None = None
    ) -> str:
        """Return a human-readable market status label."""

        phase = self.market_phase(when=when, premarket_start=premarket_start)
        if phase == "open":
            return "Market Open"
        if phase == "pre-market":
            return "Pre-Market"
        return "Market Closed"
