"""Typed contracts for APEX-K MAJOR.

CORRECTION: Use Decimal for all money/quantity to avoid overflow and
precision issues. Explicit unit tracking via type hints.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Dict, Optional, Any
import uuid

# Precision context for financial calculations
PRECISION = Decimal('0.000001')  # 6 decimal places

def to_decimal(x: float | str | int) -> Decimal:
    """Convert to Decimal with proper rounding."""
    return Decimal(str(x)).quantize(PRECISION, rounding=ROUND_HALF_UP)

class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

class DecisionAction(str, Enum):
    """All possible decision actions — not just BUY."""
    BUY = "BUY"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    EXIT = "EXIT"
    CASH = "CASH"  # Move to cash / stay in cash

class EventType(str, Enum):
    # ---- order lifecycle ----
    ORDER_INTENT            = "ORDER_INTENT"
    ORDER_SUBMITTED         = "ORDER_SUBMITTED"
    ORDER_ACK               = "ORDER_ACK"
    ORDER_REJECTED          = "ORDER_REJECTED"
    CANCEL_REQUESTED        = "CANCEL_REQUESTED"
    CANCEL_CONFIRMED        = "CANCEL_CONFIRMED"
    CANCEL_REJECTED         = "CANCEL_REJECTED"
    ORDER_REPLACED          = "ORDER_REPLACED"
    FILL                    = "FILL"
    PARTIAL_FILL            = "PARTIAL_FILL"
    FEE                     = "FEE"
    # ---- cash / corporate actions ----
    CASH_MOVEMENT           = "CASH_MOVEMENT"
    DISTRIBUTION            = "DISTRIBUTION"
    SPLIT                   = "SPLIT"
    # ---- valuation / reconciliation ----
    POSITION_VALUATION      = "POSITION_VALUATION"
    RECONCILIATION_CORRECTION = "RECONCILIATION_CORRECTION"
    # ---- governance ----
    DECISION_RECORD         = "DECISION_RECORD"
    QUARANTINE_OPENED       = "QUARANTINE_OPENED"
    QUARANTINE_REPAIRED     = "QUARANTINE_REPAIRED"
    RISK_VETO               = "RISK_VETO"

@dataclass(frozen=True)
class Event:
    """Immutable append-only event."""
    seq: int
    event_id: str
    idempotency_key: str
    type: EventType
    occurred_at: str
    recorded_at: str
    payload: Dict[str, Any]
    code_hash: str = ""
    config_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Event":
        return cls(**{**d, "type": EventType(d["type"])})

@dataclass
class Position:
    """A held ETF position with average-cost basis."""
    symbol: str
    qty: Decimal = field(default_factory=lambda: Decimal('0'))
    cost_basis: Decimal = field(default_factory=lambda: Decimal('0'))
    last_price: Decimal = field(default_factory=lambda: Decimal('0'))
    
    @property
    def avg_cost(self) -> Decimal:
        if self.qty == 0:
            return Decimal('0')
        return (self.cost_basis / self.qty).quantize(PRECISION)
    
    @property
    def market_value(self) -> Decimal:
        return (self.qty * self.last_price).quantize(PRECISION)
    
    @property
    def unrealized_pnl(self) -> Decimal:
        return (self.market_value - self.cost_basis).quantize(PRECISION)

@dataclass
class AccountState:
    """Complete account projection from events."""
    cash: Decimal = field(default_factory=lambda: Decimal('0'))
    realized_pnl: Decimal = field(default_factory=lambda: Decimal('0'))
    distributions: Decimal = field(default_factory=lambda: Decimal('0'))
    fees: Decimal = field(default_factory=lambda: Decimal('0'))
    positions: Dict[str, Position] = field(default_factory=dict)
    
    def nav(self) -> Decimal:
        position_value = sum(p.market_value for p in self.positions.values())
        return (self.cash + position_value).quantize(PRECISION)
    
    def equity(self) -> Decimal:
        return self.nav()
    
    def gross_notional(self) -> Decimal:
        return sum(p.market_value for p in self.positions.values())

@dataclass
class MarketObservation:
    """Validated market data for a single symbol."""
    symbol: str
    timestamp: str  # ISO-8601 UTC
    price: Decimal
    volume: int
    is_stale: bool = False
    data_source: str = ""
    
    def is_valid(self) -> bool:
        return self.price > 0 and self.volume >= 0 and not self.is_stale

@dataclass
class Decision:
    """A complete decision with rationale."""
    action: DecisionAction
    symbol: str
    qty: Decimal
    price: Decimal
    expected_edge: Decimal = field(default_factory=lambda: Decimal('0'))
    cost_estimate: Decimal = field(default_factory=lambda: Decimal('0'))
    reason: str = ""
    confidence: float = 0.0  # 0.0 to 1.0
    
    @property
    def is_risk_increasing(self) -> bool:
        return self.action in (DecisionAction.BUY,)
    
    @property
    def is_risk_reducing(self) -> bool:
        return self.action in (DecisionAction.REDUCE, DecisionAction.EXIT, DecisionAction.CASH)

@dataclass
class RiskVerdict:
    """Output of risk controller."""
    approved: bool
    reason: str
    adjusted_qty: Decimal = field(default_factory=lambda: Decimal('0'))
    limits_checked: Dict[str, bool] = field(default_factory=dict)
