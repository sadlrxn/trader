"""Command-line entrypoints for the trading bot."""

from __future__ import annotations

import argparse
import asyncio
from collections import deque

from trader.config import Settings
from trader.logging_utils import configure_logging
from trader.runtime import TradingRuntime
from trader.tui import TraderTui


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""

    parser = argparse.ArgumentParser(
        prog="trader", description="IBKR momentum trading bot."
    )
    subparsers = parser.add_subparsers(dest="command", required=False)

    bot_parser = subparsers.add_parser("bot", help="Run the trading bot.")
    bot_parser.add_argument(
        "--no-tui", action="store_true", help="Run without the Textual UI."
    )

    subparsers.add_parser(
        "check", help="Validate configuration and print runtime summary."
    )
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

    use_tui = not getattr(args, "no_tui", False) and settings.trader_enable_tui
    configure_logging(settings.trader_log_level, sink=log_sink, console=not use_tui)

    runtime = TradingRuntime(settings=settings, log_sink=log_sink)
    if not use_tui:
        asyncio.run(run_headless(runtime=runtime))
        return

    app = TraderTui(runtime=runtime)
    app.run()


async def run_headless(runtime: TradingRuntime) -> None:
    """Run the bot without the Textual UI."""

    await runtime.start()
    try:
        while True:
            await asyncio.sleep(1)
    finally:
        await runtime.stop()


def _run_check(settings: Settings) -> None:
    """Validate configuration and print a concise summary."""

    settings.validate_runtime_mode()
    print(f"IB host: {settings.ib_host}")
    print(f"IB port: {settings.ib_port}")
    print(f"Mode: {'paper' if settings.ib_paper else 'live'}")
    print(f"Calendar: {settings.trader_market_calendar}")
