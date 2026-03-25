"""CLI behavior tests."""

from __future__ import annotations

import sys

from trader import cli
from trader.config import Settings


def test_bot_no_tui_does_not_enable_rpc(monkeypatch) -> None:
    """Keep bot mode on the direct IBKR path unless gRPC was explicitly requested."""

    calls: dict[str, object] = {}

    async def _fake_run_headless(runtime, rpc_server, enable_rpc):  # noqa: ANN001
        calls["runtime"] = runtime
        calls["rpc_server"] = rpc_server
        calls["enable_rpc"] = enable_rpc

    monkeypatch.setattr(cli, "run_headless", _fake_run_headless)
    monkeypatch.setattr(cli, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "TradingRuntime", lambda settings, log_sink: "runtime")
    monkeypatch.setattr(cli, "RpcServer", lambda runtime, settings: "rpc")
    monkeypatch.setattr(cli, "Settings", lambda: Settings.model_validate({"TRADER_ENABLE_TUI": True}))
    monkeypatch.setattr(sys, "argv", ["trader", "bot", "--no-tui"])

    cli.main()

    assert calls["runtime"] == "runtime"
    assert calls["rpc_server"] is None
    assert calls["enable_rpc"] is False


def test_grpc_command_enables_rpc(monkeypatch) -> None:
    """Start the RPC server only for the explicit gRPC command."""

    calls: dict[str, object] = {}

    async def _fake_run_headless(runtime, rpc_server, enable_rpc):  # noqa: ANN001
        calls["runtime"] = runtime
        calls["rpc_server"] = rpc_server
        calls["enable_rpc"] = enable_rpc

    monkeypatch.setattr(cli, "run_headless", _fake_run_headless)
    monkeypatch.setattr(cli, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "TradingRuntime", lambda settings, log_sink: "runtime")
    monkeypatch.setattr(cli, "RpcServer", lambda runtime, settings: "rpc")
    monkeypatch.setattr(cli, "Settings", lambda: Settings.model_validate({"TRADER_ENABLE_TUI": True}))
    monkeypatch.setattr(sys, "argv", ["trader", "grpc"])

    cli.main()

    assert calls["runtime"] == "runtime"
    assert calls["rpc_server"] == "rpc"
    assert calls["enable_rpc"] is True
