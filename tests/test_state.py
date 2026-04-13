"""State-store tests."""

from __future__ import annotations

from decimal import Decimal

from trader.models import RuntimeStatus
from trader.state import StateStore


def test_state_store_migrates_legacy_sqlite3_path(tmp_path) -> None:
    """Copy the old state.sqlite3 file into the new state.db path when needed."""

    legacy_path = tmp_path / ".trader" / "state.sqlite3"
    new_path = tmp_path / ".trader" / "state.db"

    legacy_store = StateStore(legacy_path)
    legacy_store.save_status(RuntimeStatus(equity=Decimal("1234.56")))
    legacy_store.close()

    new_store = StateStore(new_path)
    status = new_store.load_status()

    assert status.equity == Decimal("1234.56")
    assert new_path.exists()
    new_store.close()
