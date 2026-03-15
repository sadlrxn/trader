"""gRPC service tests."""

from __future__ import annotations

import asyncio
from collections import deque

from trader.config import Settings
from trader.grpc_gen.grpc.reflection.v1alpha import reflection_pb2
from trader.rpc import _ReflectionService
from trader.runtime import TradingRuntime


class _EmptyContext:
    """Provide the minimal async context surface used by the reflection tests."""


async def _single_request(request):
    """Yield a single request for the async reflection stream."""

    yield request


def test_reflection_lists_services() -> None:
    """Expose both the trader service and the reflection service through the reflection API."""

    async def _run() -> list[str]:
        service = _ReflectionService()
        request = reflection_pb2.ServerReflectionRequest(list_services="*")
        responses = [
            response
            async for response in service.ServerReflectionInfo(_single_request(request), _EmptyContext())
        ]
        return [item.name for item in responses[0].list_services_response.service]

    service_names = asyncio.run(_run())
    assert "trader.v1.TraderControl" in service_names
    assert "grpc.reflection.v1alpha.ServerReflection" in service_names


def test_runtime_check_uses_grpc_settings() -> None:
    """Keep the runtime config compatible with the gRPC endpoint fields."""

    settings = Settings()
    runtime = TradingRuntime(settings=settings, log_sink=deque(maxlen=10))
    assert runtime.settings.trader_grpc_host == "127.0.0.1"
    assert runtime.settings.trader_grpc_port == 8765
    runtime.state_store.close()
