"""Typed runtime models used across the trading bot."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


class SignalType(StrEnum):
    """Describe the supported entry pattern families."""

    ORB = "opening_range_breakout"
    BULL_FLAG = "bull_flag"
    FLAT_TOP = "flat_top"


class OrderPurpose(StrEnum):
    """Describe why an order exists inside the engine."""

    ENTRY = "entry"
    STOP = "stop"
    TARGET = "target"
    EXIT = "exit"


class BrokerEventKind(StrEnum):
    """Describe the normalized broker callback event types."""

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"
    QUOTE = "quote"
    BAR = "bar"
    SCANNER = "scanner"
    SCANNER_END = "scanner_end"
    ORDER = "order"
    POSITION = "position"
    POSITION_END = "position_end"
    ACCOUNT = "account"
    ACCOUNT_END = "account_end"


class Bar(BaseModel):
    """Represent one OHLCV candle."""

    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def is_red(self) -> bool:
        """Return whether the candle closed below its open."""

        return self.close < self.open

    def range_size(self) -> Decimal:
        """Return the candle range in price units."""

        return self.high - self.low


class Quote(BaseModel):
    """Represent the latest top-of-book quote snapshot."""

    symbol: str
    bid: Decimal = Decimal("0")
    ask: Decimal = Decimal("0")
    last: Decimal = Decimal("0")
    volume: Decimal = Decimal("0")
    updated_at: datetime

    def spread(self) -> Decimal:
        """Return the current bid/ask spread."""

        if not self.bid or not self.ask:
            return Decimal("0")
        return self.ask - self.bid


class ScannerCandidate(BaseModel):
    """Represent one scanner result returned by IBKR."""

    symbol: str
    rank: int
    distance: str = ""
    benchmark: str = ""
    projection: str = ""


class SignalDecision(BaseModel):
    """Describe a strategy entry decision before sizing."""

    symbol: str
    signal_type: SignalType
    timestamp: datetime
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    reason: str

    def risk_per_share(self) -> Decimal:
        """Return the distance between entry and stop."""

        return self.entry_price - self.stop_price


class RiskDecision(BaseModel):
    """Describe whether the bot can enter and with what size."""

    approved: bool
    quantity: int = 0
    reason: str = ""


class OrderRecord(BaseModel):
    """Represent an order tracked by the engine."""

    order_id: int
    symbol: str
    purpose: OrderPurpose
    side: str
    quantity: int
    status: str = "Created"
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    filled_quantity: Decimal = Decimal("0")
    avg_fill_price: Decimal = Decimal("0")
    parent_order_id: int | None = None


class TradeEvent(BaseModel):
    """Represent one persisted trade lifecycle event."""

    timestamp: datetime
    symbol: str
    event_type: str
    order_id: int | None = None
    quantity: int = 0
    price: Decimal = Decimal("0")
    pnl_delta: Decimal = Decimal("0")
    note: str = ""


class ManagedPosition(BaseModel):
    """Represent the engine view of an active position."""

    symbol: str
    quantity: int
    remaining_quantity: int
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    signal_type: SignalType
    opened_at: datetime
    entry_order_id: int | None = None
    stop_order_id: int | None = None
    target_order_id: int | None = None
    realized_pnl: Decimal = Decimal("0")
    target_filled: bool = False


class BrokerEvent(BaseModel):
    """Normalize heterogeneous IBKR callbacks into one event envelope."""

    kind: BrokerEventKind
    timestamp: datetime
    symbol: str = ""
    message: str = ""
    quote: Quote | None = None
    bar: Bar | None = None
    scanner: ScannerCandidate | None = None
    order: OrderRecord | None = None
    position: ManagedPosition | None = None
    account_tag: str = ""
    account_value: str = ""


class RuntimeStatus(BaseModel):
    """Summarize the current bot state for the TUI and RPC responses."""

    connected: bool = False
    market_open: bool = False
    trading_enabled: bool = True
    last_error: str = ""
    equity: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    watchlist: list[str] = Field(default_factory=list)
    positions: list[ManagedPosition] = Field(default_factory=list)
    orders: list[OrderRecord] = Field(default_factory=list)
    market_data_symbols: list[str] = Field(default_factory=list)
