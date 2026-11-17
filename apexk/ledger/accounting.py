"""Portfolio accounting based on confirmed execution fills."""

from dataclasses import dataclass, field
from decimal import Decimal
from threading import RLock
from typing import Dict, Set

from apexk.contracts import Fill, Side, to_decimal
from apexk.exceptions import DuplicateEventError, ValidationError


@dataclass
class PositionBalance:
    quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")


@dataclass
class AccountingBook:
    initial_cash: Decimal
    cash: Decimal = field(init=False)
    positions: Dict[str, PositionBalance] = field(default_factory=dict)
    realized_pnl: Decimal = Decimal("0")
    total_fees: Decimal = Decimal("0")
    applied_execution_ids: Set[str] = field(default_factory=set)
    _lock: RLock = field(default_factory=RLock, repr=False)

    def __post_init__(self):
        self.initial_cash = to_decimal(self.initial_cash, "initial_cash")
        if self.initial_cash <= 0:
            raise ValidationError("initial_cash must be positive")
        self.cash = self.initial_cash

    def apply_fill(self, fill: Fill) -> None:
        """Apply one confirmed broker fill exactly once."""
        if not isinstance(fill, Fill):
            raise ValidationError("apply_fill requires a Fill object")

        with self._lock:
            if fill.execution_id in self.applied_execution_ids:
                raise DuplicateEventError(
                    f"Execution already applied: {fill.execution_id}"
                )

            symbol = fill.symbol
            current = self.positions.get(symbol, PositionBalance())
            quantity = fill.quantity
            notional = quantity * fill.price

            if fill.side == Side.BUY:
                total_cost = notional + fill.fees

                if total_cost > self.cash:
                    raise ValidationError(
                        f"Insufficient cash for BUY {symbol}: "
                        f"required {total_cost}, available {self.cash}"
                    )

                old_quantity = current.quantity
                new_quantity = old_quantity + quantity

                if new_quantity <= 0:
                    raise ValidationError("Invalid resulting position quantity")

                new_average = (
                    (old_quantity * current.average_price + notional)
                    / new_quantity
                )

                self.cash -= total_cost
                self.positions[symbol] = PositionBalance(
                    quantity=new_quantity,
                    average_price=new_average,
                )

            elif fill.side == Side.SELL:
                if quantity > current.quantity:
                    raise ValidationError(
                        f"SELL quantity {quantity} exceeds {symbol} "
                        f"position {current.quantity}"
                    )

                if current.quantity <= 0:
                    raise ValidationError(f"No position exists for {symbol}")

                self.cash += notional - fill.fees
                self.realized_pnl += (
                    quantity * (fill.price - current.average_price)
                ) - fill.fees

                remaining = current.quantity - quantity

                if remaining == 0:
                    self.positions.pop(symbol, None)
                else:
                    self.positions[symbol] = PositionBalance(
                        quantity=remaining,
                        average_price=current.average_price,
                    )

            else:
                raise ValidationError(f"Unsupported fill side: {fill.side}")

            self.total_fees += fill.fees
            self.applied_execution_ids.add(fill.execution_id)

    def market_value(self, prices: Dict[str, Decimal]) -> Decimal:
        """Calculate the market value of current positions."""
        total = Decimal("0")

        with self._lock:
            for symbol, position in self.positions.items():
                if symbol not in prices:
                    raise ValidationError(f"Missing valuation price for {symbol}")

                price = to_decimal(prices[symbol], f"{symbol} price")
                if price <= 0:
                    raise ValidationError(f"Invalid valuation price for {symbol}")

                total += position.quantity * price

        return total

    def equity(self, prices: Dict[str, Decimal]) -> Decimal:
        """Calculate cash plus marked-to-market positions."""
        with self._lock:
            return self.cash + self.market_value(prices)

    def unrealized_pnl(self, prices: Dict[str, Decimal]) -> Decimal:
        """Calculate unrealized profit and loss."""
        total = Decimal("0")

        with self._lock:
            for symbol, position in self.positions.items():
                if symbol not in prices:
                    raise ValidationError(f"Missing valuation price for {symbol}")

                price = to_decimal(prices[symbol], f"{symbol} price")
                total += position.quantity * (price - position.average_price)

        return total

    def snapshot(self) -> dict:
        """Return a serializable accounting snapshot."""
        with self._lock:
            return {
                "initial_cash": str(self.initial_cash),
                "cash": str(self.cash),
                "realized_pnl": str(self.realized_pnl),
                "total_fees": str(self.total_fees),
                "positions": {
                    symbol: {
                        "quantity": str(position.quantity),
                        "average_price": str(position.average_price),
                    }
                    for symbol, position in self.positions.items()
                },
                "applied_execution_ids": sorted(self.applied_execution_ids),
            }
