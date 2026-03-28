"""CLI behavior tests."""

from __future__ import annotations

import sys

from trader import cli
from trader.config import Settings


def test_bot_no_tui_runs_headless(monkeypatch) -> None:
    """Headless bot mode should dispatch directly to the runtime."""

    calls: dict[str, object] = {}

    async def _fake_run_headless(runtime):  # noqa: ANN001
        calls["runtime"] = runtime

    monkeypatch.setattr(cli, "run_headless", _fake_run_headless)
    monkeypatch.setattr(cli, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "TradingRuntime", lambda settings, log_sink: "runtime")
    monkeypatch.setattr(cli, "Settings", lambda: Settings.model_validate({"TRADER_ENABLE_TUI": True}))
    monkeypatch.setattr(sys, "argv", ["trader", "bot", "--no-tui"])

    cli.main()

    assert calls["runtime"] == "runtime"


def test_check_command_prints_runtime_summary(monkeypatch, capsys) -> None:
    """The check command should print a concise configuration summary."""

    monkeypatch.setattr(cli, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "Settings", lambda: Settings.model_validate({"IB_PORT": 7497, "IB_PAPER": True}))
    monkeypatch.setattr(sys, "argv", ["trader", "check"])

    cli.main()

    output = capsys.readouterr().out
    assert "IB host:" in output
    assert "IB port: 7497" in output
    assert "Mode: paper" in output
