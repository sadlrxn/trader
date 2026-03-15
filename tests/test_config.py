"""Configuration tests."""

from __future__ import annotations

import pytest

from trader.config import Settings


def test_live_mode_requires_explicit_opt_in() -> None:
    """Reject live mode when the live-trading guard is disabled."""

    settings = Settings.model_validate(
        {
            "IB_PAPER": False,
            "TRADER_ALLOW_LIVE": False,
        }
    )
    with pytest.raises(ValueError):
        settings.validate_runtime_mode()


def test_paper_mode_passes_validation() -> None:
    """Allow the default paper-trading configuration."""

    settings = Settings()
    settings.validate_runtime_mode()
