"""Paper order lifecycle management."""

from decimal import Decimal
from threading import RLock
from typing import Dict
from uuid import uuid4

from apexk.contracts import OrderIntent, Side, OrderStatus, to_decimal
from apexk.exceptions import BrokerError, ValidationError
from apexk.hands.broker import PaperBroker, BrokerOrder


class OrderManager:
    """Tracks submitted paper orders and their broker-reported states."""

    def __init__(self, broker: PaperBroker):
        self.broker = broker
        self._orders: Dict[str, BrokerOrder] = {}
        self._lock = RLock()

    def create_intent(
        self,
        symbol: str,
        side: Side,
        quantity: Decimal,
        reason: str = "",
    ) -> OrderIntent:
        return OrderIntent(
            client_order_id=str(uuid4()),
            symbol=symbol,
            side=side,
            quantity=to_decimal(quantity, "quantity"),
            reason=reason,
        )

    def submit(self, intent: OrderIntent) -> BrokerOrder:
        with self._lock:
            if any(
                item.client_order_id == intent.client_order_id
                for item in self._orders.values()
            ):
                raise BrokerError("Order intent already submitted")

            order = self.broker.submit_order(intent)
            self._orders[order.order_id] = order
            return order

    def refresh(self, order_id: str) -> BrokerOrder:
        with self._lock:
            order = self.broker.get_order(order_id)
            self._orders[order_id] = order
            return order

    def cancel(self, order_id: str) -> BrokerOrder:
        """Cancel and verify the paper broker's resulting order state."""
        with self._lock:
            current = self.broker.get_order(order_id)

            if current.status == OrderStatus.FILLED:
                return current

            result = self.broker.cancel_order(order_id)
            verified = self.broker.get_order(order_id)

            if result.status != verified.status:
                raise BrokerError("Cancel result did not match verified state")

            if verified.status not in {
                OrderStatus.CANCELED,
                OrderStatus.FILLED,
                OrderStatus.REJECTED,
                OrderStatus.EXPIRED,
            }:
                raise BrokerError(
                    f"Cancellation not verified: {verified.status.value}"
                )

            self._orders[order_id] = verified
            return verified

    def get_active_orders(self):
        with self._lock:
            active = []

            for order_id in list(self._orders):
                order = self.refresh(order_id)

                if order.status in {
                    OrderStatus.ACCEPTED,
                    OrderStatus.PARTIALLY_FILLED,
                }:
                    active.append(order)

            return active
