"""Execution-service tests."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal

from trader.execution import ExecutionService
from trader.models import ManagedPosition, OrderPurpose, OrderRecord, Quote, SignalDecision, SignalType
from trader.state import StateStore
from trader.trade_journal import TradeJournal


class FakeBroker:
    """Minimal broker stub for execution-service tests."""

    def __init__(self) -> None:
        self._next_order_id = 1
        self.cancelled_orders: list[int] = []
        self.exit_orders: list[OrderRecord] = []
        self.stop_orders: list[OrderRecord] = []

    async def place_entry_order(self, symbol: str, quantity: int, limit_price: Decimal) -> OrderRecord:
        return self._record(symbol, OrderPurpose.ENTRY, "BUY", quantity, limit_price=limit_price)

    async def place_stop_order(self, symbol: str, quantity: int, stop_price: Decimal) -> OrderRecord:
        order = self._record(symbol, OrderPurpose.STOP, "SELL", quantity, stop_price=stop_price, status="Submitted")
        self.stop_orders.append(order)
        return order

    async def replace_stop_order(
        self, symbol: str, quantity: int, stop_price: Decimal, old_order_id: int | None,
    ) -> OrderRecord:
        if old_order_id is not None:
            self.cancelled_orders.append(old_order_id)
        order = self._record(symbol, OrderPurpose.STOP, "SELL", quantity, stop_price=stop_price, status="Submitted")
        self.stop_orders.append(order)
        return order

    async def place_target_bracket_orders(
        self,
        symbol: str,
        target_quantity: int,
        target_price: Decimal,
        stop_quantity: int,
        stop_price: Decimal,
        oca_group: str,
    ) -> tuple[OrderRecord, OrderRecord]:
        del oca_group
        return (
            self._record(symbol, OrderPurpose.TARGET, "SELL", target_quantity, limit_price=target_price, status="Submitted"),
            self._record(symbol, OrderPurpose.STOP, "SELL", stop_quantity, stop_price=stop_price, status="Submitted"),
        )

    async def place_exit_order(self, symbol: str, quantity: int, limit_price: Decimal) -> OrderRecord:
        order = self._record(symbol, OrderPurpose.EXIT, "SELL", quantity, limit_price=limit_price, status="Submitted")
        self.exit_orders.append(order)
        return order

    async def place_trailing_stop(self, symbol: str, quantity: int, trail_amount: float) -> OrderRecord:
        return self._record(
            symbol,
            OrderPurpose.STOP,
            "SELL",
            quantity,
            stop_price=Decimal(str(trail_amount)),
            status="Submitted",
        )

    async def cancel_order(self, order_id: int) -> None:
        self.cancelled_orders.append(order_id)

    def _record(
        self,
        symbol: str,
        purpose: OrderPurpose,
        side: str,
        quantity: int,
        *,
        status: str = "Created",
        limit_price: Decimal | None = None,
        stop_price: Decimal | None = None,
    ) -> OrderRecord:
        order = OrderRecord(
            order_id=self._next_order_id,
            symbol=symbol,
            purpose=purpose,
            side=side,
            quantity=quantity,
            status=status,
            limit_price=limit_price,
            stop_price=stop_price,
        )
        self._next_order_id += 1
        return order


def test_execution_writes_trade_journal_and_completes_stage_on_fill(tmp_path) -> None:
    """Journal buy/sell operations and mark a stage complete only after the target fills."""

    broker = FakeBroker()
    state_store = StateStore(tmp_path / "state.sqlite3")
    journal = TradeJournal(tmp_path / "trades", "America/New_York")
    service = ExecutionService(
        broker=broker,
        state_store=state_store,
        broker_positions={"AMD": Decimal("10")},
        trade_journal=journal,
    )
    signal = SignalDecision(
        symbol="AMD",
        signal_type=SignalType.ORB,
        timestamp=datetime.now(tz=UTC),
        entry_price=Decimal("10.00"),
        stop_price=Decimal("9.50"),
        target_price=Decimal("11.00"),
        change_during_buy=Decimal("12.50"),
        reason="orb test",
    )

    asyncio.run(service.enter_signal(signal=signal, quantity=10))
    entry_order = service.orders[1].model_copy(
        update={
            "status": "Filled",
            "filled_quantity": Decimal("10"),
            "avg_fill_price": Decimal("10.02"),
        }
    )

    asyncio.run(service.handle_order_update(entry_order))

    position = service.positions["AMD"]
    quote = Quote(
        symbol="AMD",
        bid=Decimal("11.09"),
        ask=Decimal("11.11"),
        last=Decimal("11.10"),
        volume=Decimal("100000"),
        updated_at=datetime.now(tz=UTC),
    )
    asyncio.run(service.manage_open_position("AMD", quote, []))

    assert position.completed_stages == set()
    assert position.target_order_id is not None
    target_order_id = position.target_order_id
    assert service._target_stage_by_order[target_order_id] == 0

    target_fill = service.orders[target_order_id].model_copy(
        update={
            "status": "Filled",
            "filled_quantity": Decimal("5"),
            "avg_fill_price": Decimal("11.10"),
        }
    )
    pnl_delta = asyncio.run(service.handle_order_update(target_fill))

    assert pnl_delta == Decimal("5.40")
    assert service.positions["AMD"].completed_stages == {0}
    assert service.positions["AMD"].remaining_quantity == 5
    journal_file = next((tmp_path / "trades").glob("trades-*.json"))
    payload = json.loads(journal_file.read_text())
    assert payload[0]["operation"] == "buy"
    assert payload[0]["amount"] == 10
    assert payload[0]["stock"] == "AMD"
    assert payload[0]["change_during_buy"] == 12.5
    assert payload[1]["operation"] == "sell"
    assert payload[1]["profit"] == 5.4
    event_types = [event.event_type for event in state_store.load_trade_events(limit=20)]
    assert "signal_submitted" in event_types
    assert "stop_submitted" in event_types
    assert "target_submitted" in event_types
    state_store.close()


def test_partial_profit_waits_for_two_r_target(tmp_path) -> None:
    """Do not scale out before the trade reaches the minimum 2:1 target."""

    broker = FakeBroker()
    state_store = StateStore(tmp_path / "state.sqlite3")
    service = ExecutionService(
        broker=broker,
        state_store=state_store,
        broker_positions={"AMD": Decimal("10")},
    )
    position = ManagedPosition(
        symbol="AMD",
        quantity=10,
        remaining_quantity=10,
        entry_price=Decimal("10.00"),
        stop_price=Decimal("9.50"),
        target_price=Decimal("11.00"),
        signal_type=SignalType.ORB,
        opened_at=datetime.now(tz=UTC),
        stop_order_id=1,
    )
    service.positions["AMD"] = position
    service.orders[1] = OrderRecord(
        order_id=1,
        symbol="AMD",
        purpose=OrderPurpose.STOP,
        side="SELL",
        quantity=10,
        status="Submitted",
        stop_price=Decimal("9.50"),
    )
    quote = Quote(
        symbol="AMD",
        bid=Decimal("10.59"),
        ask=Decimal("10.61"),
        last=Decimal("10.60"),
        volume=Decimal("100000"),
        updated_at=datetime.now(tz=UTC),
    )

    asyncio.run(service.manage_open_position("AMD", quote, []))

    assert position.target_order_id is None
    state_store.close()


def test_duplicate_filled_updates_do_not_double_count_pnl(tmp_path) -> None:
    """Ignore repeated filled callbacks for the same shares."""

    broker = FakeBroker()
    state_store = StateStore(tmp_path / "state.sqlite3")
    service = ExecutionService(
        broker=broker,
        state_store=state_store,
        broker_positions={"AMD": Decimal("10")},
    )
    position = ManagedPosition(
        symbol="AMD",
        quantity=10,
        remaining_quantity=10,
        entry_price=Decimal("10.00"),
        stop_price=Decimal("9.50"),
        target_price=Decimal("11.00"),
        change_during_buy=Decimal("8.00"),
        signal_type=SignalType.ORB,
        opened_at=datetime.now(tz=UTC),
        stop_order_id=9,
        target_order_id=2,
    )
    service.positions["AMD"] = position
    state_store.save_position(position)
    submitted_target = OrderRecord(
        order_id=2,
        symbol="AMD",
        purpose=OrderPurpose.TARGET,
        side="SELL",
        quantity=5,
        status="Submitted",
        limit_price=Decimal("10.50"),
    )
    service.orders[2] = submitted_target
    state_store.save_order(submitted_target)
    service._target_stage_by_order[2] = 0

    filled_target = submitted_target.model_copy(
        update={
            "status": "Filled",
            "filled_quantity": Decimal("5"),
            "avg_fill_price": Decimal("10.50"),
        }
    )

    first_delta = asyncio.run(service.handle_order_update(filled_target))
    second_delta = asyncio.run(service.handle_order_update(filled_target))

    assert first_delta == Decimal("2.50")
    assert second_delta == Decimal("0")
    assert service.positions["AMD"].remaining_quantity == 5
    assert service.positions["AMD"].realized_pnl == Decimal("2.50")
    state_store.close()


def test_full_exit_records_closed_position(tmp_path) -> None:
    """Persist fully closed trades so the TUI can display them."""

    broker = FakeBroker()
    state_store = StateStore(tmp_path / "state.sqlite3")
    service = ExecutionService(
        broker=broker,
        state_store=state_store,
        broker_positions={"AMD": Decimal("10")},
    )
    position = ManagedPosition(
        symbol="AMD",
        quantity=10,
        remaining_quantity=10,
        entry_price=Decimal("10.00"),
        stop_price=Decimal("9.50"),
        target_price=Decimal("11.00"),
        change_during_buy=Decimal("8.00"),
        signal_type=SignalType.ORB,
        opened_at=datetime.now(tz=UTC),
        stop_order_id=9,
    )
    service.positions["AMD"] = position
    state_store.save_position(position)
    stop_order = OrderRecord(
        order_id=9,
        symbol="AMD",
        purpose=OrderPurpose.STOP,
        side="SELL",
        quantity=10,
        status="Filled",
        stop_price=Decimal("9.50"),
        filled_quantity=Decimal("10"),
        avg_fill_price=Decimal("9.48"),
    )

    pnl_delta = asyncio.run(service.handle_order_update(stop_order))

    assert pnl_delta == Decimal("-5.20")
    assert "AMD" not in service.positions
    assert service.closed_positions[-1].symbol == "AMD"
    assert service.closed_positions[-1].exit_reason == "stop"
    assert service.closed_positions[-1].realized_pnl == Decimal("-5.20")
    assert state_store.load_closed_positions(limit=1)[0].symbol == "AMD"
    state_store.close()


def test_missing_stop_is_reinstalled_for_open_position(tmp_path) -> None:
    """Reinstall a protective stop when a live position has none working."""

    broker = FakeBroker()
    state_store = StateStore(tmp_path / "state.sqlite3")
    service = ExecutionService(
        broker=broker,
        state_store=state_store,
        broker_positions={"AMD": Decimal("10")},
    )
    position = ManagedPosition(
        symbol="AMD",
        quantity=10,
        remaining_quantity=10,
        entry_price=Decimal("10.00"),
        stop_price=Decimal("9.70"),
        target_price=Decimal("10.60"),
        signal_type=SignalType.ORB,
        opened_at=datetime.now(tz=UTC),
    )
    service.positions["AMD"] = position
    state_store.save_position(position)
    quote = Quote(
        symbol="AMD",
        bid=Decimal("10.05"),
        ask=Decimal("10.07"),
        last=Decimal("10.06"),
        volume=Decimal("100000"),
        updated_at=datetime.now(tz=UTC),
    )

    asyncio.run(service.manage_open_position("AMD", quote, []))

    assert broker.stop_orders
    assert service.positions["AMD"].stop_order_id is not None
    state_store.close()


def test_missing_stop_below_trigger_submits_emergency_exit(tmp_path) -> None:
    """Force an immediate exit when price is already through the stop and no stop exists."""

    broker = FakeBroker()
    state_store = StateStore(tmp_path / "state.sqlite3")
    service = ExecutionService(
        broker=broker,
        state_store=state_store,
        broker_positions={"AMD": Decimal("10")},
    )
    position = ManagedPosition(
        symbol="AMD",
        quantity=10,
        remaining_quantity=10,
        entry_price=Decimal("10.00"),
        stop_price=Decimal("9.70"),
        target_price=Decimal("10.60"),
        signal_type=SignalType.ORB,
        opened_at=datetime.now(tz=UTC),
    )
    service.positions["AMD"] = position
    state_store.save_position(position)
    quote = Quote(
        symbol="AMD",
        bid=Decimal("9.60"),
        ask=Decimal("9.62"),
        last=Decimal("9.61"),
        volume=Decimal("100000"),
        updated_at=datetime.now(tz=UTC),
    )

    asyncio.run(service.manage_open_position("AMD", quote, []))

    assert broker.exit_orders
    assert broker.exit_orders[-1].purpose is OrderPurpose.EXIT
    state_store.close()


def test_manual_exit_position_submits_marketable_exit_order(tmp_path) -> None:
    """Allow the TUI to request a manual close for an open day-trade position."""

    broker = FakeBroker()
    state_store = StateStore(tmp_path / "state.sqlite3")
    service = ExecutionService(
        broker=broker,
        state_store=state_store,
        broker_positions={"AMD": Decimal("10")},
    )
    position = ManagedPosition(
        symbol="AMD",
        quantity=10,
        remaining_quantity=10,
        entry_price=Decimal("10.00"),
        stop_price=Decimal("9.70"),
        target_price=Decimal("10.60"),
        signal_type=SignalType.ORB,
        opened_at=datetime.now(tz=UTC),
        stop_order_id=7,
    )
    service.positions["AMD"] = position
    state_store.save_position(position)
    submitted_stop = OrderRecord(
        order_id=7,
        symbol="AMD",
        purpose=OrderPurpose.STOP,
        side="SELL",
        quantity=10,
        status="Submitted",
        stop_price=Decimal("9.70"),
    )
    service.orders[7] = submitted_stop
    state_store.save_order(submitted_stop)
    quote = Quote(
        symbol="AMD",
        bid=Decimal("10.12"),
        ask=Decimal("10.15"),
        last=Decimal("10.13"),
        volume=Decimal("100000"),
        updated_at=datetime.now(tz=UTC),
    )

    order = asyncio.run(service.manual_exit_position("AMD", quote))

    assert order.purpose is OrderPurpose.EXIT
    assert order.limit_price == Decimal("10.12")
    assert broker.cancelled_orders == [7]
    state_store.close()
