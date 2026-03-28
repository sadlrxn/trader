"""Write a daily JSON journal of executed buy and sell operations."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo


class TradeJournal:
    """Append executed operations to a per-day JSON file."""

    def __init__(self, directory: Path, timezone_name: str) -> None:
        """Initialize the trade journal storage."""

        self._directory = directory
        self._directory.mkdir(parents=True, exist_ok=True)
        self._timezone = ZoneInfo(timezone_name)

    def append_operation(
        self,
        *,
        timestamp: datetime,
        amount: int,
        operation: str,
        stock: str,
        change_during_buy: Decimal,
        profit: Decimal,
    ) -> None:
        """Append one executed buy or sell operation to the daily journal."""

        path = self._path_for(timestamp)
        payload = json.loads(path.read_text()) if path.exists() else []
        payload.append(
            {
                "time": timestamp.astimezone(self._timezone).isoformat(),
                "amount": amount,
                "operation": operation,
                "stock": stock,
                "change_during_buy": _to_number(change_during_buy),
                "profit": _to_number(profit),
            }
        )
        path.write_text(json.dumps(payload, indent=2))

    def _path_for(self, timestamp: datetime) -> Path:
        """Return the journal path for the trading day of the timestamp."""

        local_timestamp = timestamp.astimezone(self._timezone)
        return self._directory / f"trades-{local_timestamp.strftime('%d-%m-%Y')}.json"


class NullTradeJournal:
    """Fallback journal used when trade persistence is not configured."""

    def append_operation(
        self,
        *,
        timestamp: datetime,
        amount: int,
        operation: str,
        stock: str,
        change_during_buy: Decimal,
        profit: Decimal,
    ) -> None:
        """Ignore journal writes."""


def _to_number(value: Decimal) -> float:
    """Convert a Decimal into a JSON-friendly numeric value."""

    return float(value.quantize(Decimal("0.0001")))
