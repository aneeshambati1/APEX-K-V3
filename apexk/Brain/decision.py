"""Translate forecasts and portfolio state into target decisions."""

from decimal import Decimal
from typing import Dict, Mapping

from apexk.contracts import (
    AccountSnapshot,
    Decision,
    DecisionAction,
    to_decimal,
)
from apexk.exceptions import ValidationError


class DecisionEngine:
    """Produces ranked long-only ETF target weights."""

    def __init__(
        self,
        max_positions: int = 3,
        minimum_forecast: Decimal = Decimal("0"),
    ):
        if max_positions < 1:
            raise ValidationError("max_positions must be positive")

        self.max_positions = max_positions
        self.minimum_forecast = to_decimal(
            minimum_forecast, "minimum_forecast"
        )

    def decide(
        self,
        forecasts: Mapping[str, Decimal],
        account: AccountSnapshot,
    ) -> Dict[str, Decision]:
        """Rank eligible forecasts and produce equal-weight candidate targets.

        This is a transparent baseline, not an optimized final strategy.
        """
        if not isinstance(forecasts, Mapping):
            raise ValidationError("forecasts must be a mapping")

        normalized = {}

        for symbol, forecast in forecasts.items():
            key = symbol.strip().upper()
            value = to_decimal(forecast, f"{key} forecast")

            if not key:
                raise ValidationError("Forecast symbol cannot be empty")

            if value.is_finite():
                normalized[key] = value

        eligible = [
            (symbol, value)
            for symbol, value in normalized.items()
            if value > self.minimum_forecast
        ]

        eligible.sort(key=lambda item: (-item[1], item[0]))
        selected = eligible[: self.max_positions]

        target_weight = (
            Decimal("1") / Decimal(len(selected))
            if selected
            else Decimal("0")
        )

        selected_symbols = {symbol for symbol, _ in selected}
        decisions: Dict[str, Decision] = {}

        all_symbols = set(normalized) | set(account.positions)

        for symbol in sorted(all_symbols):
            current = account.positions.get(symbol)
            current_quantity = current.quantity if current else Decimal("0")

            if symbol in selected_symbols:
                decisions[symbol] = Decision(
                    symbol=symbol,
                    action=DecisionAction.BUY if current_quantity == 0
                    else DecisionAction.HOLD,
                    reason="Selected by forecast ranking",
                    target_weight=target_weight,
                )
            elif current_quantity > 0:
                decisions[symbol] = Decision(
                    symbol=symbol,
                    action=DecisionAction.EXIT,
                    reason="No longer selected by forecast ranking",
                    target_weight=Decimal("0"),
                )
            else:
                decisions[symbol] = Decision(
                    symbol=symbol,
                    action=DecisionAction.CASH,
                    reason="Not selected",
                    target_weight=Decimal("0"),
                )

        return decisions
