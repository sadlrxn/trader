"""Logging helpers for the trading bot."""

from __future__ import annotations

import logging
from collections import deque


class DequeLogHandler(logging.Handler):
    """Mirror formatted log messages into an in-memory deque."""

    def __init__(self, sink: deque[str], capacity: int) -> None:
        """Initialize the log handler.

        Args:
            sink: Shared deque used by the TUI and RPC layer.
            capacity: Maximum number of log lines to keep.
        """

        super().__init__()
        self._sink = sink
        self._capacity = capacity

    def emit(self, record: logging.LogRecord) -> None:
        """Append a formatted log record to the sink."""

        message = self.format(record)
        self._sink.append(message)
        while len(self._sink) > self._capacity:
            self._sink.popleft()


def configure_logging(
    level: str,
    sink: deque[str] | None = None,
    capacity: int = 500,
    console: bool = True,
) -> None:
    """Configure process-wide logging for terminal output and log streaming.

    Args:
        level: Root logging level name.
        sink: Optional in-memory sink mirrored into the TUI.
        capacity: Maximum number of sink lines to keep.
        console: Whether logs should also be written to stdout/stderr.
    """

    handlers: list[logging.Handler] = []
    if console:
        handlers.append(logging.StreamHandler())
    if sink is not None:
        handlers.append(DequeLogHandler(sink=sink, capacity=capacity))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=handlers,
        force=True,
    )
