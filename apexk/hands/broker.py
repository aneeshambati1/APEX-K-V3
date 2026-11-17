"""Local paper broker.

This implementation never connects to an external brokerage.
It accepts simulated orders and supports explicit simulated fills.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from threading import RLock
from typing import Dict, List, Optional
from uuid import uuid4

from apexk.contracts import OrderIntent, OrderStatus, Side, to_decimal
from apexk.exceptions import BrokerError, ValidationError


@dataclass
class BrokerOrder:
    order_id: str
    client_order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    filled_quantity: Decimal
    status: OrderStatus
    submitted_at: datetime
    average_fill_price: Optional[Decimal] = None


class PaperBroker:
    """Deterministic local paper broker; no live network access."""

    def __init__(self):
        self._orders: Dict[str, BrokerOrder] = {}
        self._client_ids = set()
        self._lock = RLock()

    def submit_order(self, intent: OrderIntent) -> BrokerOrder:
        if not isinstance(intent, OrderIntent):
            raise ValidationError("submit_order requires OrderIntent")

        with self._lock:
            if intent.client_order_id in self._client_ids:
                raise BrokerError(
                    f"Duplicate client order ID: {intent.client_order_id}"
                )

            order = BrokerOrder(
                order_id=str(uuid4()),
                client_order_id=intent.client_order_id,
                symbol=intent.symbol,
                side=intent.side,
                quantity=intent.quantity,
                filled_quantity=Decimal("0"),
                status=OrderStatus.ACCEPTED,
                submitted_at=datetime.now(timezone.utc),
            )

            self._orders[order.order_id] = order
            self._client_ids.add(intent.client_order_id)
            return order

    def get_order(self, order_id: str) -> BrokerOrder:
        with self._lock:
            if order_id not in self._orders:
                raise BrokerError(f"Unknown paper order: {order_id}")
            return self._copy_order(self._orders[order_id])

    def list_orders(self) -> List[BrokerOrder]:
        with self._lock:
            return [
                self._copy_order(order)
                for order in self._orders.values()
            ]

    def cancel_order(self, order_id: str) -> BrokerOrder:
        with self._lock:
            if order_id not in self._orders:
                raise BrokerError(f"Unknown paper order: {order_id}")

            order = self._orders[order_id]

            if order.status in {
                OrderStatus.FILLED,
                OrderStatus.CANCELED,
                OrderStatus.REJECTED,
                OrderStatus.EXPIRED,
            }:
                return self._copy_order(order)

            order.status = OrderStatus.CANCELED
            return self._copy_order(order)

    def simulate_fill(
        self,
        order_id: str,
        quantity: Decimal,
        price: Decimal,
    ) -> BrokerOrder:
        """Apply an explicit simulated fill for paper testing."""
        quantity = to_decimal(quantity, "fill quantity")
        price = to_decimal(price, "fill price")

        if quantity <= 0 or price <= 0:
            raise ValidationError("Fill quantity and price must be positive")

        with self._lock:
            if order_id not in self._orders:
                raise BrokerError(f"Unknown paper order: {order_id}")

            order = self._orders[order_id]

            if order.status not in {
                OrderStatus.ACCEPTED,
                OrderStatus.PARTIALLY_FILLED,
            }:
                raise BrokerError(
                    f"Order cannot be filled in status {order.status.value}"
                )

            remaining = order.quantity - order.filled_quantity

            if quantity > remaining:
                raise BrokerError("Fill exceeds remaining order quantity")

            previous_notional = (
                order.filled_quantity * (order.average_fill_price or Decimal("0"))
            )
            new_filled = order.filled_quantity + quantity
            new_average = (
                previous_notional + quantity * price
            ) / new_filled

            order.filled_quantity = new_filled
            order.average_fill_price = new_average

            order.status = (
                OrderStatus.FILLED
                if new_filled == order.quantity
                else OrderStatus.PARTIALLY_FILLED
            )

            return self._copy_order(order)

    @staticmethod
    def _copy_order(order: BrokerOrder) -> BrokerOrder:
        return BrokerOrder(
            order_id=order.order_id,
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            filled_quantity=order.filled_quantity,
            status=order.status,
            submitted_at=order.submitted_at,
            average_fill_price=order.average_fill_price,
        )
