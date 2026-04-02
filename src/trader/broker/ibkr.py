"""Interactive Brokers adapter built on the vendored ``ibga`` client."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from itertools import count
from typing import Any

from ibapi.client import EClient
from ibapi.common import BarData
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.order_cancel import OrderCancel
from ibapi.scanner import ScannerSubscription
from ibapi.wrapper import EWrapper

from trader.config import Settings
from trader.models import (
    Bar,
    BrokerEvent,
    BrokerEventKind,
    OrderPurpose,
    OrderRecord,
    Quote,
    ScannerCandidate,
)

logger = logging.getLogger(__name__)

_BID_TICK = 1
_ASK_TICK = 2
_LAST_TICK = 4
_BID_SIZE_TICK = 0
_ASK_SIZE_TICK = 3
_LAST_SIZE_TICK = 5
_VOLUME_TICK = 8


class _IBApp(EWrapper, EClient):
    """Bridge low-level IBKR callbacks into normalized broker events."""

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[BrokerEvent]) -> None:
        """Initialize the bridge application.

        Args:
            loop: Target asyncio loop for event delivery.
            queue: Async queue receiving normalized broker events.
        """

        EWrapper.__init__(self)
        EClient.__init__(self, self)
        self._loop = loop
        self._queue = queue
        self._request_counter = count(start=10_000)
        self._request_lock = threading.Lock()
        self._order_lock = threading.Lock()
        self._next_order_id: int | None = None
        self._ready = threading.Event()
        self._quotes: dict[str, Quote] = {}
        self._market_data_symbols: dict[int, str] = {}
        self._historical_symbols: dict[int, str] = {}
        self._scanner_requests: set[int] = set()
        self._orders: dict[int, OrderRecord] = {}

    def next_request_id(self) -> int:
        """Return a unique request identifier."""

        with self._request_lock:
            return next(self._request_counter)

    def next_order_id(self) -> int:
        """Return the next valid IBKR order identifier."""

        self._ready.wait(timeout=10)
        with self._order_lock:
            if self._next_order_id is None:
                raise RuntimeError("Interactive Brokers did not publish a next valid order ID.")
            order_id = self._next_order_id
            self._next_order_id += 1
            return order_id

    def wait_until_ready(self, timeout: float) -> bool:
        """Return whether IBKR published the initial order-id readiness signal."""

        return self._ready.wait(timeout=timeout)

    def register_market_data(self, req_id: int, symbol: str) -> None:
        """Track the symbol attached to a market data request."""

        self._market_data_symbols[req_id] = symbol

    def register_historical(self, req_id: int, symbol: str) -> None:
        """Track the symbol attached to a historical data request."""

        self._historical_symbols[req_id] = symbol

    def register_scanner(self, req_id: int) -> None:
        """Track an active scanner request."""

        self._scanner_requests.add(req_id)

    def register_order(self, record: OrderRecord) -> None:
        """Track a submitted order so later callbacks can be enriched."""

        self._orders[record.order_id] = record

    def _emit(self, event: BrokerEvent) -> None:
        """Push one normalized event into the asyncio queue."""

        if self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    def _update_quote(self, symbol: str, **changes: Any) -> None:
        """Apply quote changes and emit the updated snapshot."""

        quote = self._quotes.get(
            symbol,
            Quote(symbol=symbol, updated_at=datetime.now(tz=UTC)),
        )
        payload = quote.model_dump()
        payload.update(changes)
        payload["updated_at"] = datetime.now(tz=UTC)
        updated = Quote.model_validate(payload)
        self._quotes[symbol] = updated
        self._emit(
            BrokerEvent(
                kind=BrokerEventKind.QUOTE,
                timestamp=datetime.now(tz=UTC),
                symbol=symbol,
                quote=updated,
            )
        )

    def connectAck(self) -> None:
        """Emit a connection-established event."""

        super().connectAck()
        self._emit(BrokerEvent(kind=BrokerEventKind.CONNECTED, timestamp=datetime.now(tz=UTC)))

    def connectionClosed(self) -> None:
        """Emit a disconnect event."""

        super().connectionClosed()
        self._emit(BrokerEvent(kind=BrokerEventKind.DISCONNECTED, timestamp=datetime.now(tz=UTC)))

    def error(
        self,
        reqId: int,
        errorTime: int,
        errorCode: int,
        errorString: str,
        advancedOrderRejectJson: str = "",
    ) -> None:
        """Translate IBKR errors into broker events."""

        if errorCode == 162 and reqId in self._scanner_requests:
            logger.debug("Ignoring expected scanner cancellation callback for request %s", reqId)
            self._scanner_requests.discard(reqId)
            return
        super().error(reqId, errorTime, errorCode, errorString, advancedOrderRejectJson)
        message = f"{errorCode}: {errorString}"
        self._emit(
            BrokerEvent(
                kind=BrokerEventKind.ERROR,
                timestamp=datetime.now(tz=UTC),
                message=message,
            )
        )

    def nextValidId(self, orderId: int) -> None:
        """Capture the next valid order identifier from IBKR."""

        super().nextValidId(orderId)
        with self._order_lock:
            self._next_order_id = orderId
        self._ready.set()

    def tickPrice(self, reqId: int, tickType: int, price: float, attrib: Any) -> None:
        """Update the quote cache from price ticks."""

        super().tickPrice(reqId, tickType, price, attrib)
        symbol = self._market_data_symbols.get(reqId)
        if not symbol:
            return
        decimal_price = Decimal(str(price))
        if tickType == _BID_TICK:
            self._update_quote(symbol, bid=decimal_price)
        elif tickType == _ASK_TICK:
            self._update_quote(symbol, ask=decimal_price)
        elif tickType == _LAST_TICK:
            self._update_quote(symbol, last=decimal_price)

    def tickSize(self, reqId: int, tickType: int, size: Decimal) -> None:
        """Update the quote cache from size ticks."""

        super().tickSize(reqId, tickType, size)
        symbol = self._market_data_symbols.get(reqId)
        if not symbol:
            return
        if tickType in {_BID_SIZE_TICK, _ASK_SIZE_TICK, _LAST_SIZE_TICK, _VOLUME_TICK}:
            self._update_quote(symbol, volume=Decimal(size))

    def historicalData(self, reqId: int, bar: BarData) -> None:
        """Forward completed historical bars into the normalized event queue."""

        super().historicalData(reqId, bar)
        self._emit_bar(reqId=reqId, bar=bar, is_complete=True)

    def historicalDataUpdate(self, reqId: int, bar: BarData) -> None:
        """Forward historical streaming updates into the normalized event queue."""

        super().historicalDataUpdate(reqId, bar)
        self._emit_bar(reqId=reqId, bar=bar, is_complete=False)

    def _emit_bar(self, reqId: int, bar: BarData, is_complete: bool) -> None:
        """Normalize one historical bar."""

        symbol = self._historical_symbols.get(reqId)
        if not symbol:
            return
        timestamp = _parse_bar_timestamp(bar.date)
        normalized = Bar(
            symbol=symbol,
            timestamp=timestamp,
            open=Decimal(str(bar.open)),
            high=Decimal(str(bar.high)),
            low=Decimal(str(bar.low)),
            close=Decimal(str(bar.close)),
            volume=Decimal(str(bar.volume)),
            is_complete=is_complete,
        )
        self._emit(
            BrokerEvent(
                kind=BrokerEventKind.BAR,
                timestamp=datetime.now(tz=UTC),
                symbol=symbol,
                bar=normalized,
            )
        )

    def scannerData(
        self,
        reqId: int,
        rank: int,
        contractDetails: Any,
        distance: str,
        benchmark: str,
        projection: str,
        legsStr: str,
    ) -> None:
        """Forward scanner results into the normalized event queue."""

        super().scannerData(reqId, rank, contractDetails, distance, benchmark, projection, legsStr)
        symbol = contractDetails.contract.symbol
        candidate = ScannerCandidate(
            symbol=symbol,
            rank=rank,
            distance=distance,
            benchmark=benchmark,
            projection=projection,
        )
        self._emit(
            BrokerEvent(
                kind=BrokerEventKind.SCANNER,
                timestamp=datetime.now(tz=UTC),
                symbol=symbol,
                scanner=candidate,
            )
        )

    def scannerDataEnd(self, reqId: int) -> None:
        """Emit the scanner end marker."""

        super().scannerDataEnd(reqId)
        self._emit(BrokerEvent(kind=BrokerEventKind.SCANNER_END, timestamp=datetime.now(tz=UTC)))

    def accountSummary(self, reqId: int, account: str, tag: str, value: str, currency: str) -> None:
        """Forward account summary values into the normalized event queue."""

        super().accountSummary(reqId, account, tag, value, currency)
        self._emit(
            BrokerEvent(
                kind=BrokerEventKind.ACCOUNT,
                timestamp=datetime.now(tz=UTC),
                account_tag=tag,
                account_value=value,
            )
        )

    def accountSummaryEnd(self, reqId: int) -> None:
        """Emit the account summary completion event."""

        super().accountSummaryEnd(reqId)
        self._emit(BrokerEvent(kind=BrokerEventKind.ACCOUNT_END, timestamp=datetime.now(tz=UTC)))

    def position(self, account: str, contract: Contract, pos: Decimal, avgCost: float) -> None:
        """Forward live broker positions into the normalized event queue."""

        super().position(account, contract, pos, avgCost)
        self._emit(
            BrokerEvent(
                kind=BrokerEventKind.POSITION,
                timestamp=datetime.now(tz=UTC),
                symbol=contract.symbol,
                position_quantity=Decimal(pos),
                position_avg_cost=Decimal(str(avgCost)),
            )
        )

    def positionEnd(self) -> None:
        """Emit the broker position snapshot completion event."""

        super().positionEnd()
        self._emit(BrokerEvent(kind=BrokerEventKind.POSITION_END, timestamp=datetime.now(tz=UTC)))

    def orderStatus(
        self,
        orderId: int,
        status: str,
        filled: Decimal,
        remaining: Decimal,
        avgFillPrice: float,
        permId: int,
        parentId: int,
        lastFillPrice: float,
        clientId: int,
        whyHeld: str,
        mktCapPrice: float,
    ) -> None:
        """Forward tracked order status changes into the normalized event queue."""

        super().orderStatus(
            orderId,
            status,
            filled,
            remaining,
            avgFillPrice,
            permId,
            parentId,
            lastFillPrice,
            clientId,
            whyHeld,
            mktCapPrice,
        )
        record = self._orders.get(orderId)
        if record is None:
            return
        payload = record.model_copy(
            update={
                "status": status,
                "filled_quantity": Decimal(filled),
                "avg_fill_price": Decimal(str(avgFillPrice)),
            }
        )
        self._orders[orderId] = payload
        self._emit(
            BrokerEvent(
                kind=BrokerEventKind.ORDER,
                timestamp=datetime.now(tz=UTC),
                symbol=payload.symbol,
                order=payload,
            )
        )


class IBBrokerAdapter:
    """Provide async-friendly access to the low-level IBKR client."""

    def __init__(self, settings: Settings) -> None:
        """Initialize the adapter.

        Args:
            settings: Typed application settings.
        """

        self._settings = settings
        self._queue: asyncio.Queue[BrokerEvent] = asyncio.Queue()
        self._app: _IBApp | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._run_thread: threading.Thread | None = None
        self._market_data_requests: dict[str, int] = {}
        self._historical_requests: dict[str, int] = {}
        self._scanner_request_id: int | None = None
        self._vix_request_id: int | None = None
        self.last_vix: float | None = None

    async def connect(self) -> None:
        """Connect to IB Gateway or TWS and start the reader loop."""

        self._loop = asyncio.get_running_loop()
        self._app = _IBApp(loop=self._loop, queue=self._queue)
        timeout_seconds = max(1, self._settings.ib_connect_timeout_seconds)
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    self._app.connect,
                    self._settings.ib_host,
                    self._settings.ib_port,
                    self._settings.ib_client_id,
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as error:
            await self.disconnect()
            message = (
                f"Timed out connecting to IBKR at {self._settings.ib_host}:{self._settings.ib_port} "
                f"after {timeout_seconds}s."
            )
            raise TimeoutError(message) from error
        if not self._app.isConnected():
            message = f"Unable to connect to IBKR at {self._settings.ib_host}:{self._settings.ib_port}."
            raise ConnectionError(message)
        self._run_thread = threading.Thread(target=self._app.run, daemon=True, name="ibkr-reader")
        self._run_thread.start()
        ready = await asyncio.to_thread(self._app.wait_until_ready, timeout_seconds)
        if not ready or not self._app.isConnected():
            await self.disconnect()
            message = (
                f"IBKR connected at {self._settings.ib_host}:{self._settings.ib_port} "
                f"but never became API-ready within {timeout_seconds}s."
            )
            raise ConnectionError(message)

    async def disconnect(self) -> None:
        """Disconnect from IBKR."""

        app = self._app
        run_thread = self._run_thread
        self._app = None
        self._run_thread = None
        if app is None:
            return
        await asyncio.to_thread(app.disconnect)
        if run_thread is not None and run_thread.is_alive():
            await asyncio.to_thread(run_thread.join, 1)

    def is_connected(self) -> bool:
        """Return whether the IBKR socket session is currently connected."""

        return self._app is not None and self._app.isConnected()

    async def next_event(self) -> BrokerEvent:
        """Return the next broker event."""

        return await self._queue.get()

    async def _call_app(self, func, *args) -> None:
        """Run one IB API request and normalize socket-send failures."""

        try:
            await asyncio.to_thread(func, *args)
        except (BrokenPipeError, OSError, AttributeError) as error:
            await self.disconnect()
            raise ConnectionError("IBKR connection dropped while sending a request.") from error

    async def sync_account(self) -> None:
        """Request account summary and open positions."""

        if self._app is None or not self.is_connected():
            return
        req_id = self._app.next_request_id()
        await self._call_app(self._app.reqAccountSummary, req_id, "All", "NetLiquidation")
        await self._call_app(self._app.reqPositions)

    async def refresh_scanner(self) -> None:
        """Request a fresh top-movers scanner snapshot."""

        if self._app is None or not self.is_connected():
            return
        subscription = ScannerSubscription()
        subscription.numberOfRows = self._settings.trader_scan_max_symbols
        subscription.instrument = "STK"
        subscription.locationCode = "STK.US.MAJOR"
        subscription.scanCode = self._settings.trader_scan_code
        subscription.abovePrice = float(self._settings.trader_scan_above_price)
        subscription.belowPrice = float(self._settings.trader_scan_below_price)
        subscription.aboveVolume = self._settings.trader_scan_above_volume
        req_id = self._app.next_request_id()
        self._scanner_request_id = req_id
        self._app.register_scanner(req_id)
        await self._call_app(self._app.reqScannerSubscription, req_id, subscription, [], [])

    async def cancel_scanner(self) -> None:
        """Cancel the active scanner subscription."""

        if self._app is None or self._scanner_request_id is None or not self.is_connected():
            return
        await self._call_app(self._app.cancelScannerSubscription, self._scanner_request_id)
        self._scanner_request_id = None

    async def subscribe_symbol(self, symbol: str) -> None:
        """Subscribe to market data and minute bars for one symbol."""

        if self._app is None or not self.is_connected():
            return
        contract = build_stock_contract(symbol)
        market_req_id = self._market_data_requests.get(symbol)
        if market_req_id is None:
            market_req_id = self._app.next_request_id()
            self._market_data_requests[symbol] = market_req_id
            self._app.register_market_data(market_req_id, symbol)
            await self._call_app(self._app.reqMktData, market_req_id, contract, "", False, False, [])

        historical_req_id = self._historical_requests.get(symbol)
        if historical_req_id is None:
            historical_req_id = self._app.next_request_id()
            self._historical_requests[symbol] = historical_req_id
            self._app.register_historical(historical_req_id, symbol)
            await self._call_app(
                self._app.reqHistoricalData,
                historical_req_id,
                contract,
                "",
                "1 D",
                "1 min",
                "TRADES",
                0,
                2,
                True,
                [],
            )

    async def unsubscribe_symbol(self, symbol: str) -> None:
        """Cancel market data and historical subscriptions for one symbol."""

        if self._app is None or not self.is_connected():
            return
        market_req_id = self._market_data_requests.pop(symbol, None)
        if market_req_id is not None:
            await self._call_app(self._app.cancelMktData, market_req_id)
        historical_req_id = self._historical_requests.pop(symbol, None)
        if historical_req_id is not None:
            await self._call_app(self._app.cancelHistoricalData, historical_req_id)

    async def place_entry_order(self, symbol: str, quantity: int, limit_price: Decimal) -> OrderRecord:
        """Submit a limit buy order for an entry."""

        return await self._place_order(
            symbol=symbol,
            purpose=OrderPurpose.ENTRY,
            side="BUY",
            quantity=quantity,
            order=build_limit_order("BUY", quantity, limit_price, tif="DAY"),
        )

    async def place_stop_order(self, symbol: str, quantity: int, stop_price: Decimal) -> OrderRecord:
        """Submit a protective stop order."""

        return await self._place_order(
            symbol=symbol,
            purpose=OrderPurpose.STOP,
            side="SELL",
            quantity=quantity,
            order=build_stop_order("SELL", quantity, stop_price),
        )

    async def place_target_order(self, symbol: str, quantity: int, limit_price: Decimal) -> OrderRecord:
        """Submit the first take-profit limit order."""

        return await self._place_order(
            symbol=symbol,
            purpose=OrderPurpose.TARGET,
            side="SELL",
            quantity=quantity,
            order=build_limit_order("SELL", quantity, limit_price, tif="GTC"),
        )

    async def place_target_bracket_orders(
        self,
        symbol: str,
        target_quantity: int,
        target_price: Decimal,
        stop_quantity: int,
        stop_price: Decimal,
        oca_group: str,
    ) -> tuple[OrderRecord, OrderRecord]:
        """Submit a target order and protective stop in the same OCA group."""

        target_order = build_limit_order(
            "SELL",
            target_quantity,
            target_price,
            tif="GTC",
            transmit=False,
            oca_group=oca_group,
            oca_type=2,
        )
        stop_order = build_stop_order(
            "SELL",
            stop_quantity,
            stop_price,
            transmit=True,
            oca_group=oca_group,
            oca_type=2,
        )
        target_record = await self._place_order(
            symbol=symbol,
            purpose=OrderPurpose.TARGET,
            side="SELL",
            quantity=target_quantity,
            order=target_order,
        )
        stop_record = await self._place_order(
            symbol=symbol,
            purpose=OrderPurpose.STOP,
            side="SELL",
            quantity=stop_quantity,
            order=stop_order,
        )
        return target_record, stop_record

    async def place_exit_order(self, symbol: str, quantity: int, limit_price: Decimal) -> OrderRecord:
        """Submit a marketable limit order to exit a position immediately."""

        return await self._place_order(
            symbol=symbol,
            purpose=OrderPurpose.EXIT,
            side="SELL",
            quantity=quantity,
            order=build_limit_order("SELL", quantity, limit_price, tif="DAY"),
        )

    async def cancel_order(self, order_id: int) -> None:
        """Cancel one active order."""

        if self._app is None or not self.is_connected():
            return
        await self._call_app(self._app.cancelOrder, order_id, OrderCancel())

    async def subscribe_vix(self) -> None:
        """Subscribe to VIX index ticks on CBOE."""

        if self._app is None or not self.is_connected():
            return
        if self._vix_request_id is not None:
            return
        contract = build_vix_contract()
        req_id = self._app.next_request_id()
        self._vix_request_id = req_id
        self._app.register_market_data(req_id, "VIX")
        await self._call_app(self._app.reqMktData, req_id, contract, "", False, False, [])

    async def place_trailing_stop(self, symbol: str, quantity: int, trail_amount: float) -> OrderRecord:
        """Submit a trailing stop order."""

        order = build_trailing_stop_order("SELL", quantity, trail_amount)
        return await self._place_order(
            symbol=symbol,
            purpose=OrderPurpose.STOP,
            side="SELL",
            quantity=quantity,
            order=order,
        )

    async def replace_stop_order(self, symbol: str, quantity: int, stop_price: Decimal, old_order_id: int | None) -> OrderRecord:
        """Replace an existing stop with a new stop order."""

        if old_order_id is not None:
            await self.cancel_order(old_order_id)
        return await self.place_stop_order(symbol=symbol, quantity=quantity, stop_price=stop_price)

    async def _place_order(
        self,
        symbol: str,
        purpose: OrderPurpose,
        side: str,
        quantity: int,
        order: Order,
    ) -> OrderRecord:
        """Submit one order and return the tracked order record."""

        if self._app is None or not self.is_connected():
            raise RuntimeError("Broker is not connected.")
        order_id = self._app.next_order_id()
        record = OrderRecord(
            order_id=order_id,
            symbol=symbol,
            purpose=purpose,
            side=side,
            quantity=quantity,
            limit_price=Decimal(str(order.lmtPrice)) if getattr(order, "lmtPrice", None) not in (None, 1.7976931348623157e308) else None,
            stop_price=Decimal(str(order.auxPrice)) if getattr(order, "auxPrice", None) not in (None, 1.7976931348623157e308) else None,
        )
        self._app.register_order(record)
        contract = build_stock_contract(symbol)
        await self._call_app(self._app.placeOrder, order_id, contract, order)
        return record


def build_stock_contract(symbol: str) -> Contract:
    """Build a SMART-routed US stock contract."""

    contract = Contract()
    contract.symbol = symbol
    contract.secType = "STK"
    contract.exchange = "SMART"
    contract.currency = "USD"
    return contract


def build_limit_order(
    action: str,
    quantity: int,
    limit_price: Decimal,
    tif: str,
    transmit: bool = True,
    oca_group: str = "",
    oca_type: int = 0,
) -> Order:
    """Build a standard limit order."""

    order = Order()
    order.action = action
    order.orderType = "LMT"
    order.totalQuantity = quantity
    order.lmtPrice = float(limit_price)
    order.tif = tif
    order.outsideRth = True
    order.transmit = transmit
    order.ocaGroup = oca_group
    order.ocaType = oca_type
    return order


def build_stop_order(
    action: str,
    quantity: int,
    stop_price: Decimal,
    transmit: bool = True,
    oca_group: str = "",
    oca_type: int = 0,
) -> Order:
    """Build a protective stop-market order."""

    order = Order()
    order.action = action
    order.orderType = "STP"
    order.totalQuantity = quantity
    order.auxPrice = float(stop_price)
    order.tif = "GTC"
    order.outsideRth = True
    order.transmit = transmit
    order.ocaGroup = oca_group
    order.ocaType = oca_type
    return order


def build_vix_contract() -> Contract:
    """Build a contract for the CBOE VIX index."""

    contract = Contract()
    contract.symbol = "VIX"
    contract.secType = "IND"
    contract.exchange = "CBOE"
    contract.currency = "USD"
    return contract


def build_trailing_stop_order(
    action: str,
    quantity: int,
    trail_amount: float,
    transmit: bool = True,
) -> Order:
    """Build a trailing stop order."""

    order = Order()
    order.action = action
    order.orderType = "TRAIL"
    order.totalQuantity = quantity
    order.auxPrice = trail_amount
    order.tif = "GTC"
    order.outsideRth = True
    order.transmit = transmit
    return order


def _parse_bar_timestamp(raw_value: Any) -> datetime:
    """Parse IBKR bar timestamps into timezone-aware datetimes."""

    if isinstance(raw_value, int):
        return datetime.fromtimestamp(raw_value, tz=UTC)
    if isinstance(raw_value, str) and raw_value.isdigit():
        return datetime.fromtimestamp(int(raw_value), tz=UTC)
    return datetime.strptime(str(raw_value), "%Y%m%d  %H:%M:%S").replace(tzinfo=UTC)
