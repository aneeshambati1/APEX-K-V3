"""Shared data contracts for APEX-K.

Financial quantities use Decimal to avoid binary floating-point
errors in accounting and order calculations.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping, Optional

from apexk.exceptions import ValidationError


def to_decimal(value: Any, field_name: str = "value") -> Decimal:
    """Convert a value to a finite Decimal."""
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError(f"{field_name} must be a valid decimal")

    if not result.is_finite():
        raise ValidationError(f"{field_name} must be finite")

    return result


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class DecisionAction(str, Enum):
    BUY = "BUY"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    EXIT = "EXIT"
    CASH = "CASH"


class OrderStatus(str, Enum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class MarketObservation:
    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Optional[int] = None

    def __post_init__(self):
        symbol = self.symbol.strip().upper()
        if not symbol:
            raise ValidationError("Market observation symbol is required")

        object.__setattr__(self, "symbol", symbol)

        for name in ("open", "high", "low", "close"):
            value = to_decimal(getattr(self, name), name)
            if value <= 0:
                raise ValidationError(f"{name} must be positive")
            object.__setattr__(self, name, value)

        if self.high < max(self.open, self.close, self.low):
            raise ValidationError("High is inconsistent with OHLC values")

        if self.low > min(self.open, self.close, self.high):
            raise ValidationError("Low is inconsistent with OHLC values")

        if self.volume is not None and self.volume < 0:
            raise ValidationError("Volume cannot be negative")


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: Decimal
    average_price: Decimal

    def __post_init__(self):
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(
            self, "quantity", to_decimal(self.quantity, "quantity")
        )
        object.__setattr__(
            self, "average_price", to_decimal(self.average_price, "average_price")
        )

        if not self.symbol:
            raise ValidationError("Position symbol is required")
        if self.quantity < 0:
            raise ValidationError("Long-only position quantity cannot be negative")
        if self.average_price < 0:
            raise ValidationError("Average price cannot be negative")


@dataclass(frozen=True)
class OrderIntent:
    client_order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    created_at: datetime = field(default_factory=utc_now)
    reason: str = ""

    def __post_init__(self):
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(
            self, "quantity", to_decimal(self.quantity, "quantity")
        )

        if not self.client_order_id.strip():
            raise ValidationError("client_order_id is required")
        if not self.symbol:
            raise ValidationError("Order symbol is required")
        if not isinstance(self.side, Side):
            raise ValidationError("side must be a Side enum")
        if self.quantity <= 0:
            raise ValidationError("Order quantity must be positive")


@dataclass(frozen=True)
class Fill:
    execution_id: str
    order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    timestamp: datetime = field(default_factory=utc_now)
    fees: Decimal = Decimal("0")

    def __post_init__(self):
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(
            self, "quantity", to_decimal(self.quantity, "fill quantity")
        )
        object.__setattr__(
            self, "price", to_decimal(self.price, "fill price")
        )
        object.__setattr__(self, "fees", to_decimal(self.fees, "fees"))

        if not self.execution_id.strip():
            raise ValidationError("execution_id is required")
        if not self.order_id.strip():
            raise ValidationError("order_id is required")
        if not self.symbol:
            raise ValidationError("Fill symbol is required")
        if not isinstance(self.side, Side):
            raise ValidationError("Fill side must be a Side enum")
        if self.quantity <= 0 or self.price <= 0:
            raise ValidationError("Fill quantity and price must be positive")
        if self.fees < 0:
            raise ValidationError("Fees cannot be negative")


@dataclass(frozen=True)
class Decision:
    symbol: str
    action: DecisionAction
    reason: str
    target_weight: Decimal = Decimal("0")
    confidence: Optional[Decimal] = None

    def __post_init__(self):
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(
            self, "target_weight", to_decimal(self.target_weight, "target_weight")
        )

        if not self.symbol:
            raise ValidationError("Decision symbol is required")
        if not isinstance(self.action, DecisionAction):
            raise ValidationError("action must be a DecisionAction enum")
        if self.target_weight < 0 or self.target_weight > 1:
            raise ValidationError("target_weight must be between 0 and 1")

        if self.confidence is not None:
            confidence = to_decimal(self.confidence, "confidence")
            if confidence < 0 or confidence > 1:
                raise ValidationError("confidence must be between 0 and 1")
            object.__setattr__(self, "confidence", confidence)


@dataclass(frozen=True)
class AccountSnapshot:
    cash: Decimal
    equity: Decimal
    positions: Mapping[str, Position]
    timestamp: datetime = field(default_factory=utc_now)

    def __post_init__(self):
        object.__setattr__(self, "cash", to_decimal(self.cash, "cash"))
        object.__setattr__(self, "equity", to_decimal(self.equity, "equity"))

        if self.cash < 0:
            raise ValidationError("Cash cannot be negative")
        if self.equity < 0:
            raise ValidationError("Equity cannot be negative")

        normalized = {}
        for symbol, position in self.positions.items():
            key = symbol.strip().upper()
            if key != position.symbol:
                raise ValidationError("Position key and symbol do not match")
            if key in normalized:
                raise ValidationError(f"Duplicate position: {key}")
            normalized[key] = position

        object.__setattr__(self, "positions", normalized)
