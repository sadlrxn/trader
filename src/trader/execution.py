"""Execution and position management."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from trader.broker.ibkr import IBBrokerAdapter
from trader.models import ManagedPosition, OrderPurpose, OrderRecord, Quote, SignalDecision, TradeEvent
from trader.state import StateStore
from trader.strategy import should_exit_on_first_red

logger = logging.getLogger(__name__)


class ExecutionService:
    """Manage orders, open positions, and stop updates."""

    def __init__(self, broker: IBBrokerAdapter, state_store: StateStore) -> None:
        """Initialize the execution service.

        Args:
            broker: Live broker adapter.
            state_store: Persistence layer for positions.
        """

        self._broker = broker
        self._state_store = state_store
        self.positions: dict[str, ManagedPosition] = {
            position.symbol: position for position in self._state_store.load_positions()
        }
        self.orders: dict[int, OrderRecord] = {
            order.order_id: order for order in self._state_store.load_orders()
        }
        self._pending_signals: dict[str, SignalDecision] = {}

    async def enter_signal(self, signal: SignalDecision, quantity: int) -> None:
        """Submit the entry order for a new signal."""

        if signal.symbol in self.positions or signal.symbol in self._pending_signals:
            return
        order = await self._broker.place_entry_order(
            symbol=signal.symbol,
            quantity=quantity,
            limit_price=signal.entry_price,
        )
        self.orders[order.order_id] = order
        self._state_store.save_order(order)
        self._state_store.append_trade_event(
            TradeEvent(
                timestamp=datetime.now(tz=UTC),
                symbol=signal.symbol,
                event_type="signal_submitted",
                order_id=order.order_id,
                quantity=quantity,
                price=signal.entry_price,
                note=signal.reason,
            )
        )
        self._pending_signals[signal.symbol] = signal
        logger.info("Submitted %s entry for %s x%d at %s", signal.signal_type, signal.symbol, quantity, signal.entry_price)

    async def handle_order_update(self, order: OrderRecord) -> Decimal:
        """Apply one order status update and return realized PnL delta."""

        self.orders[order.order_id] = order
        self._state_store.save_order(order)
        self._state_store.append_trade_event(
            TradeEvent(
                timestamp=datetime.now(tz=UTC),
                symbol=order.symbol,
                event_type=f"order_{order.status.lower()}",
                order_id=order.order_id,
                quantity=int(order.filled_quantity or order.quantity),
                price=order.avg_fill_price or order.limit_price or order.stop_price or Decimal("0"),
                note=order.purpose.value,
            )
        )
        if order.status != "Filled":
            return Decimal("0")

        if order.purpose is OrderPurpose.ENTRY:
            return await self._handle_entry_fill(order)
        if order.purpose is OrderPurpose.TARGET:
            return await self._handle_target_fill(order)
        if order.purpose in {OrderPurpose.STOP, OrderPurpose.EXIT}:
            return await self._handle_exit_fill(order)
        return Decimal("0")

    async def manage_open_position(self, symbol: str, quote: Quote, bars: list) -> None:
        """Check price- and bar-driven exit conditions for an open position."""

        position = self.positions.get(symbol)
        if position is None:
            return
        if (
            not position.target_filled
            and quote.last >= position.target_price
            and position.target_order_id is None
            and not self._has_open_order(symbol, OrderPurpose.TARGET)
        ):
            target_quantity = max(1, position.remaining_quantity // 2)
            if position.stop_order_id is not None:
                await self._broker.cancel_order(position.stop_order_id)
            target_order, stop_order = await self._broker.place_target_bracket_orders(
                symbol=symbol,
                target_quantity=target_quantity,
                target_price=position.target_price,
                stop_quantity=position.remaining_quantity,
                stop_price=position.stop_price,
                oca_group=f"{symbol}-{uuid4().hex}",
            )
            self.orders[target_order.order_id] = target_order
            self.orders[stop_order.order_id] = stop_order
            self._state_store.save_order(target_order)
            self._state_store.save_order(stop_order)
            position.target_order_id = target_order.order_id
            position.stop_order_id = stop_order.order_id
            self._state_store.save_position(position)
            logger.info("Submitted target order for %s at %s", symbol, position.target_price)
            return
        if should_exit_on_first_red(position.opened_at, bars, position.target_filled) and not self._has_open_order(symbol, OrderPurpose.EXIT):
            if position.stop_order_id is not None:
                await self._broker.cancel_order(position.stop_order_id)
            limit_price = max(quote.bid, quote.last - Decimal("0.02"))
            order = await self._broker.place_exit_order(symbol, position.remaining_quantity, limit_price)
            self.orders[order.order_id] = order
            self._state_store.save_order(order)
            logger.info("Submitted first-red exit for %s at %s", symbol, limit_price)

    async def update_stop(self, symbol: str, stop_price: Decimal) -> ManagedPosition:
        """Replace the active stop for a managed position."""

        position = self.positions.get(symbol)
        if position is None:
            raise KeyError(f"No managed position for {symbol}.")
        replacement = await self._broker.replace_stop_order(
            symbol=symbol,
            quantity=position.remaining_quantity,
            stop_price=stop_price,
            old_order_id=position.stop_order_id,
        )
        self.orders[replacement.order_id] = replacement
        self._state_store.save_order(replacement)
        position.stop_price = stop_price
        position.stop_order_id = replacement.order_id
        self._state_store.save_position(position)
        self._state_store.append_trade_event(
            TradeEvent(
                timestamp=datetime.now(tz=UTC),
                symbol=symbol,
                event_type="stop_updated",
                order_id=replacement.order_id,
                quantity=position.remaining_quantity,
                price=stop_price,
                note="manual or automated stop replacement",
            )
        )
        logger.info("Updated stop for %s to %s", symbol, stop_price)
        return position

    def snapshot_positions(self) -> list[ManagedPosition]:
        """Return the managed positions as a list."""

        return list(self.positions.values())

    def snapshot_orders(self) -> list[OrderRecord]:
        """Return the tracked orders as a list."""

        return sorted(self.orders.values(), key=lambda item: item.order_id)

    async def _handle_entry_fill(self, order: OrderRecord) -> Decimal:
        """Open a managed position after the entry fills."""

        signal = self._pending_signals.pop(order.symbol, None)
        if signal is None:
            return Decimal("0")
        position = ManagedPosition(
            symbol=order.symbol,
            quantity=order.quantity,
            remaining_quantity=order.quantity,
            entry_price=order.avg_fill_price or signal.entry_price,
            stop_price=signal.stop_price,
            target_price=signal.target_price,
            signal_type=signal.signal_type,
            opened_at=datetime.now(tz=UTC),
            entry_order_id=order.order_id,
        )
        stop_order = await self._broker.place_stop_order(order.symbol, position.remaining_quantity, position.stop_price)
        self.orders[stop_order.order_id] = stop_order
        self._state_store.save_order(stop_order)
        position.stop_order_id = stop_order.order_id
        self.positions[position.symbol] = position
        self._state_store.save_position(position)
        self._state_store.append_trade_event(
            TradeEvent(
                timestamp=datetime.now(tz=UTC),
                symbol=order.symbol,
                event_type="bought",
                order_id=order.order_id,
                quantity=order.quantity,
                price=position.entry_price,
                note=position.signal_type.value,
            )
        )
        logger.info("Opened position for %s at %s", position.symbol, position.entry_price)
        return Decimal("0")

    async def _handle_target_fill(self, order: OrderRecord) -> Decimal:
        """Apply target-one fill logic and move the stop to break even."""

        position = self.positions.get(order.symbol)
        if position is None:
            return Decimal("0")
        filled_quantity = int(order.filled_quantity)
        pnl_delta = (order.avg_fill_price - position.entry_price) * Decimal(filled_quantity)
        position.realized_pnl += pnl_delta
        position.remaining_quantity = max(0, position.remaining_quantity - filled_quantity)
        position.target_filled = True
        self._state_store.append_trade_event(
            TradeEvent(
                timestamp=datetime.now(tz=UTC),
                symbol=order.symbol,
                event_type="sold_target",
                order_id=order.order_id,
                quantity=filled_quantity,
                price=order.avg_fill_price,
                pnl_delta=pnl_delta,
                note="target fill",
            )
        )
        if position.remaining_quantity == 0:
            self.positions.pop(order.symbol, None)
            self._state_store.delete_position(order.symbol)
            logger.info("Closed position for %s after target fill", order.symbol)
            return pnl_delta
        replacement = await self._broker.replace_stop_order(
            symbol=order.symbol,
            quantity=position.remaining_quantity,
            stop_price=position.entry_price,
            old_order_id=position.stop_order_id,
        )
        self.orders[replacement.order_id] = replacement
        self._state_store.save_order(replacement)
        position.stop_price = position.entry_price
        position.stop_order_id = replacement.order_id
        self._state_store.save_position(position)
        logger.info("Locked break-even stop for %s after target fill", order.symbol)
        return pnl_delta

    async def _handle_exit_fill(self, order: OrderRecord) -> Decimal:
        """Close part or all of a position after a stop or exit order fills."""

        position = self.positions.get(order.symbol)
        if position is None:
            return Decimal("0")
        filled_quantity = int(order.filled_quantity)
        pnl_delta = (order.avg_fill_price - position.entry_price) * Decimal(filled_quantity)
        position.realized_pnl += pnl_delta
        position.remaining_quantity = max(0, position.remaining_quantity - filled_quantity)
        self._state_store.append_trade_event(
            TradeEvent(
                timestamp=datetime.now(tz=UTC),
                symbol=order.symbol,
                event_type="sold_exit" if order.purpose is OrderPurpose.EXIT else "sold_stop",
                order_id=order.order_id,
                quantity=filled_quantity,
                price=order.avg_fill_price,
                pnl_delta=pnl_delta,
                note=order.purpose.value,
            )
        )
        if position.remaining_quantity == 0:
            if position.target_order_id is not None and position.target_order_id != order.order_id:
                await self._broker.cancel_order(position.target_order_id)
            self.positions.pop(order.symbol, None)
            self._state_store.delete_position(order.symbol)
            logger.info("Closed position for %s", order.symbol)
        else:
            self._state_store.save_position(position)
        return pnl_delta

    def _has_open_order(self, symbol: str, purpose: OrderPurpose) -> bool:
        """Return whether a non-final order already exists for the symbol and purpose."""

        final_statuses = {"Filled", "Cancelled", "Inactive"}
        return any(
            order.symbol == symbol and order.purpose == purpose and order.status not in final_statuses
            for order in self.orders.values()
        )
