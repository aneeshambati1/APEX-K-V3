"""Pre-trade risk checks for a long-only paper-trading system."""

from dataclasses import dataclass
from decimal import Decimal

from apexk.contracts import OrderIntent, Side, to_decimal
from apexk.exceptions import RiskRejectedError, ValidationError


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: Decimal = Decimal("25000")
    max_position_notional: Decimal = Decimal("50000")
    max_gross_exposure: Decimal = Decimal("100000")
    minimum_cash_reserve: Decimal = Decimal("0.05")

    def __post_init__(self):
        for name in (
            "max_order_notional",
            "max_position_notional",
            "max_gross_exposure",
            "minimum_cash_reserve",
        ):
            value = to_decimal(getattr(self, name), name)
            object.__setattr__(self, name, value)

        if self.max_order_notional <= 0:
            raise ValidationError("max_order_notional must be positive")
        if self.max_position_notional <= 0:
            raise ValidationError("max_position_notional must be positive")
        if self.max_gross_exposure <= 0:
            raise ValidationError("max_gross_exposure must be positive")
        if not Decimal("0") <= self.minimum_cash_reserve < Decimal("1"):
            raise ValidationError("minimum_cash_reserve must be between 0 and 1")


class RiskController:
    """Rejects orders that violate explicit risk limits.

    This initial controller does not resize or silently clip an order.
    """

    def __init__(self, limits: RiskLimits):
        self.limits = limits

    def check_order(
        self,
        intent: OrderIntent,
        price: Decimal,
        cash: Decimal,
        equity: Decimal,
        current_position_quantity: Decimal = Decimal("0"),
        current_gross_exposure: Decimal = Decimal("0"),
    ) -> None:
        price = to_decimal(price, "price")
        cash = to_decimal(cash, "cash")
        equity = to_decimal(equity, "equity")
        current_position_quantity = to_decimal(
            current_position_quantity, "current_position_quantity"
        )
        current_gross_exposure = to_decimal(
            current_gross_exposure, "current_gross_exposure"
        )

        if price <= 0 or equity <= 0:
            raise RiskRejectedError("Invalid price or equity")

        if cash < 0 or current_position_quantity < 0:
            raise RiskRejectedError("Negative cash or position quantity")

        notional = intent.quantity * price

        if notional > self.limits.max_order_notional:
            raise RiskRejectedError("Order exceeds maximum order notional")

        if intent.side == Side.BUY:
            if notional + cash * Decimal("0") > cash - (
                equity * self.limits.minimum_cash_reserve
            ):
                raise RiskRejectedError("BUY would breach minimum cash reserve")

            resulting_position = (
                current_position_quantity * price + notional
            )

            if resulting_position > self.limits.max_position_notional:
                raise RiskRejectedError("BUY would exceed position notional limit")

            if current_gross_exposure + notional > self.limits.max_gross_exposure:
                raise RiskRejectedError("BUY would exceed gross exposure limit")

        elif intent.side == Side.SELL:
            if intent.quantity > current_position_quantity:
                raise RiskRejectedError("SELL exceeds available long position")

        else:
            raise RiskRejectedError("Unsupported order side")
