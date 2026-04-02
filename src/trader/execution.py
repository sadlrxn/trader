"""Execution and position management."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from trader.broker.ibkr import IBBrokerAdapter
from trader.models import ClosedPosition, ManagedPosition, OrderPurpose, OrderRecord, Quote, SignalDecision, TradeEvent
from trader.state import StateStore
from trader.strategy import should_exit_on_first_red
from trader.trade_journal import NullTradeJournal, TradeJournal

logger = logging.getLogger(__name__)


def parse_partial_stages(stages_str: str) -> list[tuple[Decimal, Decimal]]:
    """Parse 'R_multiple:fraction,...' config into a sorted list of (r_multiple, fraction) tuples."""

    stages: list[tuple[Decimal, Decimal]] = []
    for pair in stages_str.split(","):
        pair = pair.strip()
        if not pair:
            continue
        r_str, frac_str = pair.split(":")
        stages.append((Decimal(r_str.strip()), Decimal(frac_str.strip())))
    stages.sort(key=lambda s: s[0])
    return stages


class ExecutionService:
    """Manage orders, open positions, and stop updates."""

    def __init__(
        self,
        broker: IBBrokerAdapter,
        state_store: StateStore,
        partial_stages_config: str = "1:0.50,2:0.50",
        broker_positions: dict[str, Decimal] | None = None,
        trade_journal: TradeJournal | None = None,
    ) -> None:
        """Initialize the execution service.

        Args:
            broker: Live broker adapter.
            state_store: Persistence layer for positions.
            partial_stages_config: R-multiple:fraction pairs for staged profit taking.
            broker_positions: Reference to broker-reported positions for verification.
            trade_journal: Daily JSON journal for executed buy and sell operations.
        """

        self._broker = broker
        self._state_store = state_store
        self._partial_stages = parse_partial_stages(partial_stages_config)
        self._broker_positions = broker_positions or {}
        self._trade_journal = trade_journal or NullTradeJournal()
        self.positions: dict[str, ManagedPosition] = {
            position.symbol: position for position in self._state_store.load_positions()
        }
        self.closed_positions: list[ClosedPosition] = self._state_store.load_closed_positions()
        self.orders: dict[int, OrderRecord] = {
            order.order_id: order for order in self._state_store.load_orders()
        }
        self._pending_signals: dict[str, SignalDecision] = {}
        self._order_placed_at: dict[int, datetime] = {}
        self._trailing_converted: set[str] = set()
        self._target_stage_by_order: dict[int, int] = {}

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
        self._order_placed_at[order.order_id] = datetime.now(tz=UTC)
        logger.info("Submitted %s entry for %s x%d at %s", signal.signal_type, signal.symbol, quantity, signal.entry_price)

    async def handle_order_update(self, order: OrderRecord) -> Decimal:
        """Apply one order status update and return realized PnL delta."""

        previous = self.orders.get(order.order_id)
        previous_filled = previous.filled_quantity if previous is not None else Decimal("0")
        self.orders[order.order_id] = order
        self._state_store.save_order(order)
        if self._should_record_order_event(previous=previous, current=order):
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
        if order.purpose is OrderPurpose.ENTRY and order.status in {"Cancelled", "Inactive", "ApiCancelled"}:
            self._pending_signals.pop(order.symbol, None)
            self._order_placed_at.pop(order.order_id, None)

        filled_delta = int(order.filled_quantity - previous_filled)
        if filled_delta <= 0:
            return Decimal("0")

        if order.purpose is OrderPurpose.ENTRY:
            return await self._handle_entry_fill(order, filled_delta)
        if order.purpose is OrderPurpose.TARGET:
            return await self._handle_target_fill(order, filled_delta)
        if order.purpose in {OrderPurpose.STOP, OrderPurpose.EXIT}:
            return await self._handle_exit_fill(order, filled_delta)
        return Decimal("0")

    async def manage_open_position(self, symbol: str, quote: Quote, bars: list) -> None:
        """Check price- and bar-driven exit conditions for an open position.

        Uses configurable multi-stage partial profit taking based on R-multiples.
        """

        position = self.positions.get(symbol)
        if position is None:
            return
        await self._ensure_stop_loss_protection(position=position, quote=quote)
        # Scale out one stage at a time. A stage is only marked complete after
        # the target actually fills, not when the order is merely submitted.
        if not self._has_open_order(symbol, OrderPurpose.TARGET):
            risk_per_share = position.entry_price - position.stop_price
            if risk_per_share > 0:
                triggered_stage = self._next_triggered_stage(position, quote.last, risk_per_share)
                if triggered_stage is not None:
                    stage_index, _, fraction = triggered_stage
                    sell_quantity = max(1, int(fraction * position.remaining_quantity))
                    sell_quantity = min(sell_quantity, position.remaining_quantity)
                    if sell_quantity > 0:
                        if position.stop_order_id is not None:
                            await self._broker.cancel_order(position.stop_order_id)
                        target_order, stop_order = await self._broker.place_target_bracket_orders(
                            symbol=symbol,
                            target_quantity=sell_quantity,
                            target_price=quote.last,
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
                        self._target_stage_by_order[target_order.order_id] = stage_index
                        self._state_store.save_position(position)
                        logger.info(
                            "Stage %d triggered for %s: sell %d at %s (R=%.1f)",
                            stage_index, symbol, sell_quantity, quote.last, float(triggered_stage[1]),
                        )
                        return
        if should_exit_on_first_red(position.opened_at, bars, position.target_filled) and not self._has_open_order(symbol, OrderPurpose.EXIT):
            if position.stop_order_id is not None:
                await self._broker.cancel_order(position.stop_order_id)
            limit_price = max(quote.bid, quote.last - Decimal("0.02"))
            order = await self._broker.place_exit_order(symbol, position.remaining_quantity, limit_price)
            self.orders[order.order_id] = order
            self._state_store.save_order(order)
            logger.info("Submitted first-red exit for %s at %s", symbol, limit_price)

    def _next_triggered_stage(
        self, position: ManagedPosition, current_price: Decimal, risk_per_share: Decimal,
    ) -> tuple[int, Decimal, Decimal] | None:
        """Return the next incomplete stage whose R-multiple threshold is met.

        Returns:
            Tuple of (stage_index, r_multiple, fraction) or None.
        """

        for stage_index, (r_multiple, fraction) in enumerate(self._partial_stages):
            if stage_index in position.completed_stages:
                continue
            r_achieved = (current_price - position.entry_price) / risk_per_share
            if r_achieved >= r_multiple:
                return stage_index, r_multiple, fraction
        return None

    def _verify_position(self, symbol: str) -> bool:
        """Verify position exists at the broker before modifying orders."""

        broker_qty = self._broker_positions.get(symbol, Decimal("0"))
        return int(broker_qty) > 0

    async def update_stop(self, symbol: str, stop_price: Decimal) -> ManagedPosition:
        """Replace the active stop for a managed position."""

        position = self.positions.get(symbol)
        if position is None:
            raise KeyError(f"No managed position for {symbol}.")
        if not self._verify_position(symbol):
            logger.warning("Guard: broker has no position for %s, skipping stop update", symbol)
            raise KeyError(f"Broker has no position for {symbol}.")
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

    async def manual_exit_position(self, symbol: str, quote: Quote | None) -> OrderRecord:
        """Submit a manual exit for an open position from the operator interface."""

        position = self.positions.get(symbol)
        if position is None:
            raise KeyError(f"No managed position for {symbol}.")
        if not self._verify_position(symbol):
            raise KeyError(f"Broker has no position for {symbol}.")
        if self._has_open_order(symbol, OrderPurpose.EXIT):
            raise ValueError(f"Manual exit already pending for {symbol}.")

        if position.stop_order_id is not None and self._has_open_order(symbol, OrderPurpose.STOP):
            await self._broker.cancel_order(position.stop_order_id)
        if position.target_order_id is not None and self._has_open_order(symbol, OrderPurpose.TARGET):
            await self._broker.cancel_order(position.target_order_id)

        if quote is not None and quote.bid > 0:
            limit_price = max(Decimal("0.01"), quote.bid)
        elif quote is not None and quote.last > 0:
            limit_price = max(Decimal("0.01"), quote.last - Decimal("0.02"))
        else:
            limit_price = max(Decimal("0.01"), position.stop_price)
        order = await self._broker.place_exit_order(symbol, position.remaining_quantity, limit_price)
        self.orders[order.order_id] = order
        self._state_store.save_order(order)
        self._state_store.append_trade_event(
            TradeEvent(
                timestamp=datetime.now(tz=UTC),
                symbol=symbol,
                event_type="manual_exit_submitted",
                order_id=order.order_id,
                quantity=position.remaining_quantity,
                price=limit_price,
                note="operator requested exit",
            )
        )
        logger.info("Manual exit submitted for %s at %s", symbol, limit_price)
        return order

    def snapshot_positions(self) -> list[ManagedPosition]:
        """Return the managed positions as a list."""

        return list(self.positions.values())

    def snapshot_closed_positions(self) -> list[ClosedPosition]:
        """Return the recently closed positions for TUI surfaces."""

        return list(self.closed_positions)

    def snapshot_orders(self) -> list[OrderRecord]:
        """Return the tracked orders as a list."""

        return sorted(self.orders.values(), key=lambda item: item.order_id)

    async def _handle_entry_fill(self, order: OrderRecord, filled_quantity: int) -> Decimal:
        """Open a managed position after the entry fills."""

        signal = self._pending_signals.get(order.symbol)
        if signal is None:
            return Decimal("0")
        fill_price = order.avg_fill_price or signal.entry_price
        now = datetime.now(tz=UTC)
        position = self.positions.get(order.symbol)
        if position is None:
            position = ManagedPosition(
                symbol=order.symbol,
                quantity=filled_quantity,
                remaining_quantity=filled_quantity,
                entry_price=fill_price,
                stop_price=signal.stop_price,
                target_price=signal.target_price,
                change_during_buy=signal.change_during_buy,
                signal_type=signal.signal_type,
                opened_at=now,
                entry_order_id=order.order_id,
            )
        else:
            position.quantity += filled_quantity
            position.remaining_quantity += filled_quantity
            position.entry_price = fill_price
            position.stop_price = signal.stop_price
            position.target_price = signal.target_price
            position.change_during_buy = signal.change_during_buy
        stop_order = await self._sync_stop_order(
            symbol=order.symbol,
            quantity=position.remaining_quantity,
            stop_price=position.stop_price,
            old_order_id=position.stop_order_id,
        )
        self.orders[stop_order.order_id] = stop_order
        self._state_store.save_order(stop_order)
        position.stop_order_id = stop_order.order_id
        self.positions[position.symbol] = position
        self._state_store.save_position(position)
        self._state_store.append_trade_event(
            TradeEvent(
                timestamp=now,
                symbol=order.symbol,
                event_type="bought",
                order_id=order.order_id,
                quantity=filled_quantity,
                price=position.entry_price,
                note=position.signal_type.value,
            )
        )
        self._trade_journal.append_operation(
            timestamp=now,
            amount=filled_quantity,
            operation="buy",
            stock=order.symbol,
            change_during_buy=signal.change_during_buy,
            profit=Decimal("0"),
        )
        if order.status == "Filled":
            self._pending_signals.pop(order.symbol, None)
            self._order_placed_at.pop(order.order_id, None)
        signal_entry = signal.entry_price
        if order.status == "Filled" and signal_entry and fill_price:
            slippage = float(fill_price - signal_entry)
            slippage_pct = abs(slippage) / float(signal_entry) * 100 if float(signal_entry) > 0 else 0.0
            direction = "worse" if slippage > 0 else "better"
            logger.info("Slippage %s: $%+.4f (%.2f%% %s)", order.symbol, slippage, slippage_pct, direction)
        logger.info("Opened position for %s at %s", position.symbol, position.entry_price)
        return Decimal("0")

    async def _handle_target_fill(self, order: OrderRecord, filled_quantity: int) -> Decimal:
        """Apply target-one fill logic and move the stop to break even."""

        position = self.positions.get(order.symbol)
        if position is None:
            return Decimal("0")
        event_time = datetime.now(tz=UTC)
        fill_price = order.avg_fill_price or order.limit_price or position.entry_price
        pnl_delta = (fill_price - position.entry_price) * Decimal(filled_quantity)
        position.realized_pnl += pnl_delta
        position.remaining_quantity = max(0, position.remaining_quantity - filled_quantity)
        position.target_filled = True
        stage_index = self._target_stage_by_order.get(order.order_id)
        if stage_index is not None:
            position.completed_stages.add(stage_index)
        self._state_store.append_trade_event(
            TradeEvent(
                timestamp=event_time,
                symbol=order.symbol,
                event_type="sold_target",
                order_id=order.order_id,
                quantity=filled_quantity,
                price=fill_price,
                pnl_delta=pnl_delta,
                note="target fill",
            )
        )
        self._trade_journal.append_operation(
            timestamp=event_time,
            amount=filled_quantity,
            operation="sell",
            stock=order.symbol,
            change_during_buy=position.change_during_buy,
            profit=pnl_delta,
        )
        if position.remaining_quantity == 0:
            self._target_stage_by_order.pop(order.order_id, None)
            self._close_position(
                position=position,
                exit_price=fill_price,
                exit_reason="target",
                closed_at=event_time,
            )
            logger.info("Closed position for %s after target fill", order.symbol)
            return pnl_delta
        replacement = await self._sync_stop_order(
            symbol=order.symbol,
            quantity=position.remaining_quantity,
            stop_price=position.entry_price,
            old_order_id=position.stop_order_id,
        )
        self.orders[replacement.order_id] = replacement
        self._state_store.save_order(replacement)
        if order.status == "Filled":
            self._target_stage_by_order.pop(order.order_id, None)
            position.target_order_id = None
        position.stop_price = position.entry_price
        position.stop_order_id = replacement.order_id
        self._state_store.save_position(position)
        logger.info("Locked break-even stop for %s after target fill", order.symbol)
        return pnl_delta

    async def _handle_exit_fill(self, order: OrderRecord, filled_quantity: int) -> Decimal:
        """Close part or all of a position after a stop or exit order fills."""

        position = self.positions.get(order.symbol)
        if position is None:
            return Decimal("0")
        event_time = datetime.now(tz=UTC)
        fill_price = order.avg_fill_price or order.limit_price or order.stop_price or position.entry_price
        pnl_delta = (fill_price - position.entry_price) * Decimal(filled_quantity)
        position.realized_pnl += pnl_delta
        position.remaining_quantity = max(0, position.remaining_quantity - filled_quantity)
        self._state_store.append_trade_event(
            TradeEvent(
                timestamp=event_time,
                symbol=order.symbol,
                event_type="sold_exit" if order.purpose is OrderPurpose.EXIT else "sold_stop",
                order_id=order.order_id,
                quantity=filled_quantity,
                price=fill_price,
                pnl_delta=pnl_delta,
                note=order.purpose.value,
            )
        )
        self._trade_journal.append_operation(
            timestamp=event_time,
            amount=filled_quantity,
            operation="sell",
            stock=order.symbol,
            change_during_buy=position.change_during_buy,
            profit=pnl_delta,
        )
        if position.remaining_quantity == 0:
            if position.target_order_id is not None and position.target_order_id != order.order_id:
                await self._broker.cancel_order(position.target_order_id)
            self._close_position(
                position=position,
                exit_price=fill_price,
                exit_reason=order.purpose.value,
                closed_at=event_time,
            )
            logger.info("Closed position for %s", order.symbol)
        else:
            self._state_store.save_position(position)
        return pnl_delta

    async def cancel_stale_entries(self, timeout_seconds: int = 120) -> None:
        """Cancel entry orders that have been open longer than the timeout."""

        now = datetime.now(tz=UTC)
        for oid, placed_at in list(self._order_placed_at.items()):
            if (now - placed_at).total_seconds() > timeout_seconds:
                record = self.orders.get(oid)
                if record and record.status in ("Submitted", "PreSubmitted"):
                    await self._broker.cancel_order(oid)
                    self._pending_signals.pop(record.symbol, None)
                    del self._order_placed_at[oid]
                    logger.warning("Cancelled stale entry %d (%s)", oid, record.symbol)

    async def convert_to_trailing_stop(
        self,
        symbol: str,
        trail_amount: float,
    ) -> None:
        """Replace the active stop with a trailing stop order."""

        position = self.positions.get(symbol)
        if position is None or symbol in self._trailing_converted:
            return
        if not self._verify_position(symbol):
            logger.warning("Guard: broker has no position for %s, skipping trailing stop", symbol)
            return
        if position.stop_order_id is not None:
            await self._broker.cancel_order(position.stop_order_id)
        replacement = await self._broker.place_trailing_stop(
            symbol=symbol,
            quantity=position.remaining_quantity,
            trail_amount=trail_amount,
        )
        self.orders[replacement.order_id] = replacement
        self._state_store.save_order(replacement)
        position.stop_order_id = replacement.order_id
        self._state_store.save_position(position)
        self._trailing_converted.add(symbol)
        logger.info("Converted stop for %s to trailing (trail $%.2f)", symbol, trail_amount)

    def _has_open_order(self, symbol: str, purpose: OrderPurpose) -> bool:
        """Return whether a non-final order already exists for the symbol and purpose."""

        final_statuses = {"Filled", "Cancelled", "Inactive", "ApiCancelled"}
        return any(
            order.symbol == symbol and order.purpose == purpose and order.status not in final_statuses
            for order in self.orders.values()
        )

    def _should_record_order_event(self, previous: OrderRecord | None, current: OrderRecord) -> bool:
        """Return whether the order update changed meaningfully enough to persist."""

        if previous is None:
            return True
        return (
            previous.status != current.status
            or previous.filled_quantity != current.filled_quantity
            or previous.avg_fill_price != current.avg_fill_price
        )

    async def _sync_stop_order(
        self,
        symbol: str,
        quantity: int,
        stop_price: Decimal,
        old_order_id: int | None,
    ) -> OrderRecord:
        """Place the initial stop or replace the existing one for the new size."""

        if old_order_id is None:
            return await self._broker.place_stop_order(symbol, quantity, stop_price)
        return await self._broker.replace_stop_order(
            symbol=symbol,
            quantity=quantity,
            stop_price=stop_price,
            old_order_id=old_order_id,
        )

    async def _ensure_stop_loss_protection(self, position: ManagedPosition, quote: Quote) -> None:
        """Reinstall a stop or force an exit when a live position is unprotected.

        This cannot guarantee a perfect stop fill in the presence of halts, gaps,
        or exchange outages, but it does ensure the engine always tries to keep a
        protective order live and falls back to an immediate exit when price is
        already through the stop without a working stop order.
        """

        if self._has_open_order(position.symbol, OrderPurpose.STOP) or self._has_open_order(position.symbol, OrderPurpose.EXIT):
            return
        if quote.last <= position.stop_price or (quote.bid and quote.bid <= position.stop_price):
            limit_price = max(Decimal("0.01"), quote.bid or (quote.last - Decimal("0.02")))
            emergency = await self._broker.place_exit_order(position.symbol, position.remaining_quantity, limit_price)
            self.orders[emergency.order_id] = emergency
            self._state_store.save_order(emergency)
            logger.warning("Protective stop missing for %s below stop; submitted emergency exit at %s", position.symbol, limit_price)
            return
        replacement = await self._sync_stop_order(
            symbol=position.symbol,
            quantity=position.remaining_quantity,
            stop_price=position.stop_price,
            old_order_id=position.stop_order_id,
        )
        self.orders[replacement.order_id] = replacement
        self._state_store.save_order(replacement)
        position.stop_order_id = replacement.order_id
        self._state_store.save_position(position)
        logger.warning("Protective stop missing for %s; reinstalled stop at %s", position.symbol, position.stop_price)

    def _close_position(
        self,
        *,
        position: ManagedPosition,
        exit_price: Decimal,
        exit_reason: str,
        closed_at: datetime,
    ) -> None:
        """Persist a closed position and remove it from the open-position set."""

        closed = ClosedPosition(
            symbol=position.symbol,
            quantity=position.quantity,
            entry_price=position.entry_price,
            exit_price=exit_price,
            realized_pnl=position.realized_pnl,
            opened_at=position.opened_at,
            closed_at=closed_at,
            signal_type=position.signal_type,
            exit_reason=exit_reason,
        )
        self.closed_positions.append(closed)
        self.closed_positions = self.closed_positions[-50:]
        self._state_store.append_closed_position(closed)
        self.positions.pop(position.symbol, None)
        self._state_store.delete_position(position.symbol)
