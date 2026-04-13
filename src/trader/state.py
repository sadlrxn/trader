"""Persistence for runtime state."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from trader.models import ClosedPosition, ManagedPosition, OrderRecord, RuntimeStatus, TradeEvent


class StateStore:
    """Persist runtime state, orders, and trade activity to SQLite."""

    def __init__(self, path: Path) -> None:
        """Initialize the state store.

        Args:
            path: SQLite database path.
        """

        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_db_if_needed()
        self._connection = sqlite3.connect(self._path)
        self._initialize()

    def _migrate_legacy_db_if_needed(self) -> None:
        """Copy the legacy state database into the new default path if needed."""

        if self._path.exists():
            return
        if self._path.name != "state.db":
            return
        legacy = self._path.with_name("state.sqlite3")
        if not legacy.exists():
            return
        shutil.copy2(legacy, self._path)

    def _initialize(self) -> None:
        """Create the required persistence tables if they do not exist."""

        cursor = self._connection.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
                symbol TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                order_id INTEGER PRIMARY KEY,
                payload TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS closed_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                closed_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        self._connection.commit()

    def save_status(self, status: RuntimeStatus) -> None:
        """Persist the latest runtime status snapshot."""

        self._connection.execute(
            "REPLACE INTO kv_store(key, value) VALUES(?, ?)",
            ("runtime_status", status.model_dump_json()),
        )
        self._connection.commit()

    def load_status(self) -> RuntimeStatus:
        """Load the latest runtime status snapshot if one exists."""

        row = self._connection.execute(
            "SELECT value FROM kv_store WHERE key = ?",
            ("runtime_status",),
        ).fetchone()
        if row is None:
            return RuntimeStatus()
        return RuntimeStatus.model_validate(json.loads(row[0]))

    def save_position(self, position: ManagedPosition) -> None:
        """Persist one managed position."""

        self._connection.execute(
            "REPLACE INTO positions(symbol, payload) VALUES(?, ?)",
            (position.symbol, position.model_dump_json()),
        )
        self._connection.commit()

    def delete_position(self, symbol: str) -> None:
        """Remove one managed position from persistence."""

        self._connection.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
        self._connection.commit()

    def load_positions(self) -> list[ManagedPosition]:
        """Load all persisted positions."""

        rows = self._connection.execute("SELECT payload FROM positions").fetchall()
        return [ManagedPosition.model_validate(json.loads(payload)) for (payload,) in rows]

    def append_closed_position(self, position: ClosedPosition) -> None:
        """Persist one fully closed trade record."""

        self._connection.execute(
            "INSERT INTO closed_positions(symbol, closed_at, payload) VALUES(?, ?, ?)",
            (position.symbol, position.closed_at.isoformat(), position.model_dump_json()),
        )
        self._connection.commit()

    def load_closed_positions(self, limit: int = 50) -> list[ClosedPosition]:
        """Load recently closed positions."""

        rows = self._connection.execute(
            "SELECT payload FROM closed_positions ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [ClosedPosition.model_validate(json.loads(payload)) for (payload,) in reversed(rows)]

    def save_order(self, order: OrderRecord) -> None:
        """Persist one tracked order."""

        self._connection.execute(
            "REPLACE INTO orders(order_id, payload) VALUES(?, ?)",
            (order.order_id, order.model_dump_json()),
        )
        self._connection.commit()

    def load_orders(self) -> list[OrderRecord]:
        """Load all persisted orders."""

        rows = self._connection.execute("SELECT payload FROM orders ORDER BY order_id").fetchall()
        return [OrderRecord.model_validate(json.loads(payload)) for (payload,) in rows]

    def append_trade_event(self, event: TradeEvent) -> None:
        """Append one trade lifecycle event."""

        self._connection.execute(
            "INSERT INTO trade_events(timestamp, symbol, event_type, payload) VALUES(?, ?, ?, ?)",
            (event.timestamp.isoformat(), event.symbol, event.event_type, event.model_dump_json()),
        )
        self._connection.commit()

    def load_trade_events(self, limit: int = 100) -> list[TradeEvent]:
        """Load recent trade lifecycle events."""

        rows = self._connection.execute(
            "SELECT payload FROM trade_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [TradeEvent.model_validate(json.loads(payload)) for (payload,) in reversed(rows)]

    def close(self) -> None:
        """Close the SQLite connection."""

        self._connection.close()
