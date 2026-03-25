"""Embedded gRPC control plane for the trading bot."""

from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation

import grpc

from trader.config import Settings
from trader.proto_codegen import ensure_generated

ensure_generated()

from trader.grpc_gen.grpc.reflection.v1alpha import reflection_pb2, reflection_pb2_grpc
from trader.grpc_gen import trader_pb2, trader_pb2_grpc
from trader.runtime import TradingRuntime


class _TraderControlService(trader_pb2_grpc.TraderControlServicer):
    """Implement the protobuf-defined gRPC service against the runtime."""

    def __init__(self, runtime: TradingRuntime, settings: Settings) -> None:
        """Initialize the gRPC servicer.

        Args:
            runtime: Shared trading runtime.
            settings: Typed application settings.
        """

        self._runtime = runtime
        self._settings = settings

    async def Health(
        self,
        request: trader_pb2.Empty,
        context: grpc.aio.ServicerContext,
    ) -> trader_pb2.HealthResponse:
        """Return a concise health snapshot for liveness checks."""

        status = self._runtime.snapshot_status()
        return trader_pb2.HealthResponse(
            status="ok",
            connected=status.connected,
            market_open=status.market_open,
        )

    async def GetState(
        self,
        request: trader_pb2.Empty,
        context: grpc.aio.ServicerContext,
    ) -> trader_pb2.RuntimeState:
        """Return the latest runtime snapshot encoded as protobuf."""

        return self._runtime_state()

    async def WatchRuntime(
        self,
        request: trader_pb2.StreamRequest,
        context: grpc.aio.ServicerContext,
    ):
        """Stream runtime snapshots at a caller-defined cadence."""

        interval_ms = max(250, request.interval_ms or 1000)
        while not context.done():
            yield self._runtime_state()
            await asyncio.sleep(interval_ms / 1000)

    async def PauseTrading(
        self,
        request: trader_pb2.Empty,
        context: grpc.aio.ServicerContext,
    ) -> trader_pb2.ActionResponse:
        """Pause new entries."""

        self._runtime.pause_trading()
        return trader_pb2.ActionResponse(status="paused")

    async def ResumeTrading(
        self,
        request: trader_pb2.Empty,
        context: grpc.aio.ServicerContext,
    ) -> trader_pb2.ActionResponse:
        """Resume new entries."""

        self._runtime.resume_trading()
        return trader_pb2.ActionResponse(status="running")

    async def UpdateStop(
        self,
        request: trader_pb2.StopUpdateRequest,
        context: grpc.aio.ServicerContext,
    ) -> trader_pb2.RuntimeState:
        """Update a managed position stop price through gRPC."""

        try:
            new_stop = Decimal(request.new_stop)
        except InvalidOperation as error:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"Invalid new_stop value: {request.new_stop}")
            raise error

        try:
            await self._runtime.update_stop(symbol=request.symbol.upper(), new_stop=new_stop)
        except KeyError as error:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(error))
            raise error
        return self._runtime_state()

    def _runtime_state(self) -> trader_pb2.RuntimeState:
        """Convert the runtime snapshot into the protobuf response."""

        status = self._runtime.snapshot_status()
        return trader_pb2.RuntimeState(
            connected=status.connected,
            market_open=status.market_open,
            trading_enabled=status.trading_enabled,
            last_error=status.last_error,
            equity=str(status.equity),
            realized_pnl=str(status.realized_pnl),
            watchlist=status.watchlist,
            market_data_symbols=status.market_data_symbols,
            positions=[
                trader_pb2.PositionSnapshot(
                    symbol=position.symbol,
                    quantity=position.quantity,
                    remaining_quantity=position.remaining_quantity,
                    entry_price=str(position.entry_price),
                    stop_price=str(position.stop_price),
                    target_price=str(position.target_price),
                    signal_type=position.signal_type.value,
                    opened_at=position.opened_at.isoformat(),
                    realized_pnl=str(position.realized_pnl),
                    target_filled=position.target_filled,
                )
                for position in status.positions
            ],
            orders=[
                trader_pb2.OrderSnapshot(
                    order_id=order.order_id,
                    symbol=order.symbol,
                    purpose=order.purpose.value,
                    side=order.side,
                    quantity=order.quantity,
                    status=order.status,
                    limit_price=str(order.limit_price) if order.limit_price is not None else "",
                    stop_price=str(order.stop_price) if order.stop_price is not None else "",
                    filled_quantity=str(order.filled_quantity),
                    avg_fill_price=str(order.avg_fill_price),
                )
                for order in status.orders
            ],
            quotes=[
                trader_pb2.QuoteSnapshot(
                    symbol=quote.symbol,
                    bid=str(quote.bid),
                    ask=str(quote.ask),
                    last=str(quote.last),
                    volume=str(quote.volume),
                    updated_at=quote.updated_at.isoformat(),
                )
                for quote in self._runtime.snapshot_quotes()
            ],
            logs=self._runtime.snapshot_logs(),
            grpc_target=f"{self._settings.trader_grpc_host}:{self._settings.trader_grpc_port}",
        )


class _ReflectionService(reflection_pb2_grpc.ServerReflectionServicer):
    """Implement enough of the reflection API for grpcurl and other protobuf clients."""

    def __init__(self) -> None:
        """Initialize the reflection service descriptor registry."""

        self._services = [
            "grpc.reflection.v1alpha.ServerReflection",
            trader_pb2.DESCRIPTOR.services_by_name["TraderControl"].full_name,
        ]
        self._descriptors_by_filename = {
            "grpc/reflection/v1alpha/reflection.proto": reflection_pb2.DESCRIPTOR.serialized_pb,
            "trader.proto": trader_pb2.DESCRIPTOR.serialized_pb,
        }
        self._descriptors_by_symbol = {
            "grpc.reflection.v1alpha.ServerReflection": reflection_pb2.DESCRIPTOR.serialized_pb,
            "trader.v1.TraderControl": trader_pb2.DESCRIPTOR.serialized_pb,
        }

    async def ServerReflectionInfo(self, request_iterator, context):
        """Respond to reflection requests from grpcurl-compatible clients."""

        async for request in request_iterator:
            if request.HasField("list_services"):
                yield reflection_pb2.ServerReflectionResponse(
                    valid_host=request.host,
                    original_request=request,
                    list_services_response=reflection_pb2.ListServiceResponse(
                        service=[
                            reflection_pb2.ServiceResponse(name=service_name)
                            for service_name in self._services
                        ]
                    ),
                )
                continue

            if request.HasField("file_by_filename"):
                descriptor = self._descriptors_by_filename.get(request.file_by_filename)
                yield self._file_descriptor_response(request=request, descriptor=descriptor)
                continue

            if request.HasField("file_containing_symbol"):
                descriptor = self._descriptors_by_symbol.get(request.file_containing_symbol)
                yield self._file_descriptor_response(request=request, descriptor=descriptor)
                continue

            yield reflection_pb2.ServerReflectionResponse(
                valid_host=request.host,
                original_request=request,
                error_response=reflection_pb2.ErrorResponse(
                    error_code=grpc.StatusCode.UNIMPLEMENTED.value[0],
                    error_message="Reflection request type is not implemented by this server.",
                ),
            )

    def _file_descriptor_response(
        self,
        request: reflection_pb2.ServerReflectionRequest,
        descriptor: bytes | None,
    ) -> reflection_pb2.ServerReflectionResponse:
        """Build a reflection response for one descriptor lookup."""

        if descriptor is None:
            return reflection_pb2.ServerReflectionResponse(
                valid_host=request.host,
                original_request=request,
                error_response=reflection_pb2.ErrorResponse(
                    error_code=grpc.StatusCode.NOT_FOUND.value[0],
                    error_message="Descriptor not found.",
                ),
            )
        return reflection_pb2.ServerReflectionResponse(
            valid_host=request.host,
            original_request=request,
            file_descriptor_response=reflection_pb2.FileDescriptorResponse(
                file_descriptor_proto=[descriptor],
            ),
        )


class RpcServer:
    """Serve a localhost gRPC control plane for operator control and state streaming."""

    def __init__(self, runtime: TradingRuntime, settings: Settings) -> None:
        """Initialize the gRPC server.

        Args:
            runtime: Shared trading runtime.
            settings: Typed application settings.
        """

        self._runtime = runtime
        self._settings = settings
        self._server: grpc.aio.Server | None = None

    async def start(self) -> None:
        """Start the gRPC server if it is not already running."""

        if self._server is not None:
            return
        self._server = grpc.aio.server()
        trader_pb2_grpc.add_TraderControlServicer_to_server(
            _TraderControlService(runtime=self._runtime, settings=self._settings),
            self._server,
        )
        reflection_pb2_grpc.add_ServerReflectionServicer_to_server(
            _ReflectionService(),
            self._server,
        )
        self._server.add_insecure_port(self.target)
        await self._server.start()

    async def stop(self) -> None:
        """Stop the gRPC server."""

        if self._server is None:
            return
        await self._server.stop(grace=1)
        self._server = None

    @property
    def target(self) -> str:
        """Return the gRPC bind target."""

        return f"{self._settings.trader_grpc_host}:{self._settings.trader_grpc_port}"
