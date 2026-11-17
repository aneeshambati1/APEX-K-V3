
"""Protective price monitoring for paper positions."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict

from apexk.contracts import to_decimal
from apexk.exceptions import ValidationError


@dataclass(frozen=True)
class StopLevel:
    symbol: str
    entry_price: Decimal
    stop_price: Decimal
    triggered: bool = False


class ProtectionManager:
    """Tracks stop levels and detects stop-price breaches."""

    def __init__(self):
        self._stops: Dict[str, StopLevel] = {}

    def set_stop(
        self,
        symbol: str,
        entry_price: Decimal,
        stop_price: Decimal,
    ) -> StopLevel:
        symbol = symbol.strip().upper()
        entry = to_decimal(entry_price, "entry_price")
        stop = to_decimal(stop_price, "stop_price")

        if not symbol:
            raise ValidationError("Stop symbol is required")

        if entry <= 0 or stop <= 0:
            raise ValidationError("Entry and stop prices must be positive")

        if stop >= entry:
            raise ValidationError("Long-position stop must be below entry")

        level = StopLevel(
            symbol=symbol,
            entry_price=entry,
            stop_price=stop,
            triggered=False,
        )

        self._stops[symbol] = level
        return level

    def check_price(self, symbol: str, price: Decimal) -> bool:
        """Return True once price reaches or falls below the stop."""
        symbol = symbol.strip().upper()
        price = to_decimal(price, "price")

        if price <= 0:
            raise ValidationError("Price must be positive")

        level = self._stops.get(symbol)

        if level is None:
            return False

        if level.triggered:
            return True

        if price <= level.stop_price:
            self._stops[symbol] = StopLevel(
                symbol=level.symbol,
                entry_price=level.entry_price,
                stop_price=level.stop_price,
                triggered=True,
            )
            return True

        return False

    def remove_stop(self, symbol: str) -> None:
        self._stops.pop(symbol.strip().upper(), None)

    def get_stop(self, symbol: str):
        return self._stops.get(symbol.strip().upper())

    def get_all_stops(self) -> Dict[str, StopLevel]:
        return dict(self._stops)
