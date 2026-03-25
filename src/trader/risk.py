"""Risk management rules."""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN

from trader.config import Settings
from trader.models import RiskDecision, SignalDecision


class RiskManager:
    """Apply trade-level and day-level risk limits."""

    def __init__(self, settings: Settings) -> None:
        """Initialize the risk manager.

        Args:
            settings: Typed application settings.
        """

        self._settings = settings

    def can_trade(
        self,
        equity: Decimal,
        realized_pnl: Decimal,
        open_positions: int,
        trading_enabled: bool,
    ) -> RiskDecision:
        """Validate account-level trading guards before sizing a new trade.

        Args:
            equity: Current account net liquidation estimate.
            realized_pnl: Realized PnL for the current session.
            open_positions: Number of active positions.
            trading_enabled: Operator-controlled trading switch.

        Returns:
            A decision explaining whether new entries are allowed.
        """

        if not trading_enabled:
            return RiskDecision(approved=False, reason="Trading is paused.")
        if equity <= Decimal("0"):
            return RiskDecision(approved=False, reason="Account equity is unavailable.")
        if open_positions >= self._settings.trader_max_open_positions:
            return RiskDecision(approved=False, reason="Max open positions reached.")
        if realized_pnl <= -(equity * self._settings.trader_max_daily_loss):
            return RiskDecision(approved=False, reason="Daily loss limit exceeded.")
        return RiskDecision(approved=True, reason="Risk checks passed.")

    def size_signal(
        self,
        signal: SignalDecision,
        equity: Decimal,
        realized_pnl: Decimal,
        open_positions: int,
        trading_enabled: bool,
    ) -> RiskDecision:
        """Calculate order size for a signal under the configured limits.

        Args:
            signal: Candidate trade from the strategy engine.
            equity: Current account net liquidation estimate.
            realized_pnl: Realized PnL for the current session.
            open_positions: Number of active positions.
            trading_enabled: Operator-controlled trading switch.

        Returns:
            A decision containing the approved quantity when sizing succeeds.
        """

        account_gate = self.can_trade(
            equity=equity,
            realized_pnl=realized_pnl,
            open_positions=open_positions,
            trading_enabled=trading_enabled,
        )
        if not account_gate.approved:
            return account_gate

        risk_per_share = signal.risk_per_share()
        if risk_per_share <= Decimal("0"):
            return RiskDecision(approved=False, reason="Signal stop is not below entry.")

        risk_budget = equity * self._settings.trader_risk_per_trade
        quantity = int((risk_budget / risk_per_share).to_integral_value(rounding=ROUND_DOWN))
        if quantity <= 0:
            return RiskDecision(approved=False, reason="Risk budget does not allow a minimum size.")

        return RiskDecision(approved=True, quantity=quantity, reason="Risk sizing approved.")
