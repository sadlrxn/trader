"""Application configuration models."""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Define the runtime settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    ib_host: str = Field(default="127.0.0.1", alias="IB_HOST")
    ib_port: int = Field(default=4002, alias="IB_PORT")
    ib_client_id: int = Field(default=101, alias="IB_CLIENT_ID")
    ib_account: str = Field(default="", alias="IB_ACCOUNT")
    ib_paper: bool = Field(default=True, alias="IB_PAPER")
    ib_connect_timeout_seconds: int = Field(default=10, alias="IB_CONNECT_TIMEOUT_SECONDS")
    trader_allow_live: bool = Field(default=False, alias="TRADER_ALLOW_LIVE")
    trader_market_calendar: str = Field(default="XNYS", alias="TRADER_MARKET_CALENDAR")
    trader_timezone: str = Field(default="America/New_York", alias="TRADER_TIMEZONE")
    trader_log_level: str = Field(default="INFO", alias="TRADER_LOG_LEVEL")
    trader_state_db: Path = Field(default=Path(".trader/state.db"), alias="TRADER_STATE_DB")
    trader_trade_log_dir: Path = Field(default=Path(".trader/trades"), alias="TRADER_TRADE_LOG_DIR")
    trader_watchlist_dir: Path = Field(default=Path(".trader/watchlists"), alias="TRADER_WATCHLIST_DIR")
    trader_risk_per_trade: Decimal = Field(default=Decimal("0.005"), alias="TRADER_RISK_PER_TRADE")
    trader_max_position_notional_pct: Decimal = Field(default=Decimal("0.50"), alias="TRADER_MAX_POSITION_NOTIONAL_PCT")
    trader_max_daily_loss: Decimal = Field(default=Decimal("0.02"), alias="TRADER_MAX_DAILY_LOSS")
    trader_max_open_positions: int = Field(default=3, alias="TRADER_MAX_OPEN_POSITIONS")
    trader_scan_max_symbols: int = Field(default=20, alias="TRADER_SCAN_MAX_SYMBOLS")
    trader_scan_above_price: Decimal = Field(default=Decimal("2"), alias="TRADER_SCAN_ABOVE_PRICE")
    trader_scan_below_price: Decimal = Field(default=Decimal("20"), alias="TRADER_SCAN_BELOW_PRICE")
    trader_scan_above_volume: int = Field(default=100_000, alias="TRADER_SCAN_ABOVE_VOLUME")
    trader_scan_market_cap_below: Decimal = Field(default=Decimal("0"), alias="TRADER_SCAN_MARKET_CAP_BELOW")
    trader_min_day_gain_pct: Decimal = Field(default=Decimal("10"), alias="TRADER_MIN_DAY_GAIN_PCT")
    trader_scan_code: str = Field(default="TOP_PERC_GAIN", alias="TRADER_SCAN_CODE")
    trader_fallback_symbols: str = Field(default="", alias="TRADER_FALLBACK_SYMBOLS")
    trader_entry_cutoff: time = Field(default=time(hour=11, minute=30), alias="TRADER_ENTRY_CUTOFF")
    trader_flatten_time: time = Field(default=time(hour=15, minute=55), alias="TRADER_FLATTEN_TIME")
    trader_premarket_start: time = Field(default=time(hour=4), alias="TRADER_PREMARKET_START")
    trader_target_r_multiple: Decimal = Field(default=Decimal("2"), alias="TRADER_TARGET_R_MULTIPLE")
    trader_enable_tui: bool = Field(default=True, alias="TRADER_ENABLE_TUI")
    trader_enable_vix_gate: bool = Field(default=True, alias="TRADER_ENABLE_VIX_GATE")
    trader_max_drawdown: Decimal = Field(default=Decimal("0.05"), alias="TRADER_MAX_DRAWDOWN")
    trader_stale_order_timeout: int = Field(default=120, alias="TRADER_STALE_ORDER_TIMEOUT")
    trader_trailing_stop_atr_multiple: Decimal = Field(default=Decimal("2"), alias="TRADER_TRAILING_STOP_ATR_MULTIPLE")
    trader_partial_stages: str = Field(default="1:0.50,2:0.50", alias="TRADER_PARTIAL_STAGES")

    def validate_runtime_mode(self) -> None:
        """Guard against accidentally running a live session without opt-in."""

        if not self.ib_paper and not self.trader_allow_live:
            message = "Live IBKR trading is disabled unless TRADER_ALLOW_LIVE=true."
            raise ValueError(message)

    def fallback_symbols(self) -> list[str]:
        """Return optional seed symbols used only when explicitly configured."""

        return [symbol.strip().upper() for symbol in self.trader_fallback_symbols.split(",") if symbol.strip()]
