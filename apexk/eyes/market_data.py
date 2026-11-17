"""Validated in-memory market observation feed."""

from datetime import datetime, timezone
from threading import RLock
from typing import Dict, Iterable, Optional

from apexk.contracts import MarketObservation
from apexk.exceptions import ValidationError


class MarketDataFeed:
    """Stores the latest validated observation for each symbol."""

    def __init__(self):
        self._latest: Dict[str, MarketObservation] = {}
        self._lock = RLock()

    def ingest(self, observation: MarketObservation) -> None:
        """Ingest an observation, rejecting older timestamps."""
        if not isinstance(observation, MarketObservation):
            raise ValidationError(
                "MarketDataFeed accepts MarketObservation objects only"
            )

        with self._lock:
            existing = self._latest.get(observation.symbol)

            if existing and observation.timestamp < existing.timestamp:
                raise ValidationError(
                    f"Out-of-order market data for {observation.symbol}"
                )

            if existing and observation.timestamp == existing.timestamp:
                if observation != existing:
                    raise ValidationError(
                        f"Conflicting observation timestamp for {observation.symbol}"
                    )
                return

            self._latest[observation.symbol] = observation

    def ingest_many(self, observations: Iterable[MarketObservation]) -> None:
        """Ingest observations individually."""
        for observation in observations:
            self.ingest(observation)

    def get(self, symbol: str) -> Optional[MarketObservation]:
        with self._lock:
            return self._latest.get(symbol.strip().upper())

    def get_all(self) -> Dict[str, MarketObservation]:
        with self._lock:
            return dict(self._latest)

    def is_fresh(
        self,
        symbol: str,
        max_age_seconds: int,
        now: Optional[datetime] = None,
    ) -> bool:
        """Check whether an observation is within the permitted age."""
        if max_age_seconds < 0:
            raise ValidationError("max_age_seconds cannot be negative")

        now = now or datetime.now(timezone.utc)

        if now.tzinfo is None:
            raise ValidationError("now must include a timezone")

        observation = self.get(symbol)
        if observation is None:
            return False

        age = (now - observation.timestamp).total_seconds()
        return 0 <= age <= max_age_seconds

    def clear(self) -> None:
        with self._lock:
            self._latest.clear()
