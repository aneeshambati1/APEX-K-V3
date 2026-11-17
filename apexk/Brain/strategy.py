"""Strategy interfaces and safe placeholder implementation."""

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Dict, Mapping

from apexk.contracts import MarketObservation


class StrategyProvider(ABC):
    """Interface for a replaceable forecasting strategy."""

    @abstractmethod
    def generate_forecasts(
        self,
        observations: Mapping[str, MarketObservation],
    ) -> Dict[str, Decimal]:
        """Return expected-return forecasts by ETF symbol."""
        raise NotImplementedError


class NoOpStrategy(StrategyProvider):
    """Safe strategy that produces no forecasts."""

    def generate_forecasts(
        self,
        observations: Mapping[str, MarketObservation],
    ) -> Dict[str, Decimal]:
        return {}


class FixedForecastStrategy(StrategyProvider):
    """Deterministic test strategy; not intended for real trading decisions."""

    def __init__(self, forecasts: Mapping[str, Decimal]):
        self.forecasts = {
            symbol.strip().upper(): Decimal(str(value))
            for symbol, value in forecasts.items()
        }

    def generate_forecasts(
        self,
        observations: Mapping[str, MarketObservation],
    ) -> Dict[str, Decimal]:
        available = set(observations)
        return {
            symbol: value
            for symbol, value in self.forecasts.items()
            if symbol in available
        }
