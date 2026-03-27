"""Command-line entrypoints for the trading bot."""

from __future__ import annotations

import argparse
import asyncio
from collections import deque

from trader.config import Settings
from trader.logging_utils import configure_logging
from trader.rpc import RpcServer
from trader.runtime import TradingRuntime
from trader.tui import TraderTui


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""

    parser = argparse.ArgumentParser(prog="trader", description="IBKR momentum trading bot.")
    subparsers = parser.add_subparsers(dest="command", required=False)

    bot_parser = subparsers.add_parser("bot", help="Run the trading bot.")
    bot_parser.add_argument("--no-tui", action="store_true", help="Run without the Textual UI.")

    subparsers.add_parser("grpc", help="Run the trading bot headless with gRPC enabled.")
    subparsers.add_parser("rpc", help="Deprecated alias for the gRPC headless mode.")
    serve_parser = subparsers.add_parser("serve", help="Run the TUI as a web app via textual serve.")
    serve_parser.add_argument("--host", default="0.0.0.0", help="Bind address.")
    serve_parser.add_argument("--port", type=int, default=7681, help="Port.")

    subparsers.add_parser("check", help="Validate configuration and print runtime summary.")
    return parser


def main() -> None:
    """Parse arguments and dispatch to the selected command."""

    parser = build_parser()
    args = parser.parse_args()
    command = args.command or "bot"
    settings = Settings()
    log_sink: deque[str] = deque(maxlen=500)
    if command == "check":
        configure_logging(settings.trader_log_level, sink=log_sink)
        _run_check(settings)
        return

    if command == "serve":
        configure_logging(settings.trader_log_level, sink=log_sink, console=True)
        from textual_serve.server import Server
        server = Server("trader.cli:_make_app", host=args.host, port=args.port)
        server.serve()
        return

    enable_rpc = command in {"grpc", "rpc"}
    use_tui = command not in {"grpc", "rpc"} and not getattr(args, "no_tui", False) and settings.trader_enable_tui
    configure_logging(settings.trader_log_level, sink=log_sink, console=not use_tui)

    runtime = TradingRuntime(settings=settings, log_sink=log_sink)
    rpc_server = RpcServer(runtime=runtime, settings=settings) if enable_rpc else None
    if not use_tui:
        asyncio.run(run_headless(runtime=runtime, rpc_server=rpc_server, enable_rpc=enable_rpc))
        return

    app = TraderTui(runtime=runtime)
    app.run()


async def run_headless(runtime: TradingRuntime, rpc_server: RpcServer | None, enable_rpc: bool) -> None:
    """Run the bot without the Textual UI."""

    await runtime.start()
    if enable_rpc and rpc_server is not None:
        await rpc_server.start()
    try:
        while True:
            await asyncio.sleep(1)
    finally:
        if rpc_server is not None:
            await rpc_server.stop()
        await runtime.stop()


def _make_app() -> TraderTui:
    """Factory for textual serve — creates a fresh TUI app instance."""
    settings = Settings()
    log_sink: deque[str] = deque(maxlen=500)
    configure_logging(settings.trader_log_level, sink=log_sink)
    runtime = TradingRuntime(settings=settings, log_sink=log_sink)
    return TraderTui(runtime=runtime)


def _run_check(settings: Settings) -> None:
    """Validate configuration and print a concise summary."""

    settings.validate_runtime_mode()
    print(f"IB host: {settings.ib_host}")
    print(f"IB port: {settings.ib_port}")
    print(f"Mode: {'paper' if settings.ib_paper else 'live'}")
    print(f"gRPC: {settings.trader_grpc_host}:{settings.trader_grpc_port}")
    print(f"Calendar: {settings.trader_market_calendar}")
