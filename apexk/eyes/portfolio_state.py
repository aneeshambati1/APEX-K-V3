"""Portfolio-state projection from the accounting book."""

from decimal import Decimal
from typing import Dict

from apexk.contracts import AccountSnapshot, Position, to_decimal
from apexk.exceptions import ValidationError
from apexk.ledger.accounting import AccountingBook


class PortfolioState:
    """Builds a current portfolio snapshot using confirmed accounting data."""

    def __init__(self, accounting: AccountingBook):
        self.accounting = accounting

    def snapshot(self, prices: Dict[str, Decimal]) -> AccountSnapshot:
        if not isinstance(prices, dict):
            raise ValidationError("prices must be a dictionary")

        normalized_prices = {
            symbol.strip().upper(): to_decimal(price, f"{symbol} price")
            for symbol, price in prices.items()
        }

        for symbol, price in normalized_prices.items():
            if price <= 0:
                raise ValidationError(f"Invalid price for {symbol}")

        positions = {
            symbol: Position(
                symbol=symbol,
                quantity=balance.quantity,
                average_price=balance.average_price,
            )
            for symbol, balance in self.accounting.positions.items()
        }

        equity = self.accounting.equity(normalized_prices)

        return AccountSnapshot(
            cash=self.accounting.cash,
            equity=equity,
            positions=positions,
        )
