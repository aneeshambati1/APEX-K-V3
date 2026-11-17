"""Constraint-aware baseline portfolio allocation."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Mapping

from apexk.contracts import to_decimal
from apexk.exceptions import ValidationError


@dataclass(frozen=True)
class AllocationResult:
    weights: Dict[str, Decimal]
    cash_weight: Decimal
    rationale: str


class DynamicAllocator:
    """Allocates capital across eligible opportunities subject to constraints.

    The initial implementation is deliberately conservative and transparent.
    It does not claim to solve a full portfolio optimization problem.
    """

    def __init__(
        self,
        max_position_weight: Decimal = Decimal("0.40"),
        max_portfolio_investment: Decimal = Decimal("0.95"),
        minimum_weight: Decimal = Decimal("0.01"),
    ):
        self.max_position_weight = to_decimal(
            max_position_weight, "max_position_weight"
        )
        self.max_portfolio_investment = to_decimal(
            max_portfolio_investment, "max_portfolio_investment"
        )
        self.minimum_weight = to_decimal(minimum_weight, "minimum_weight")

        if not Decimal("0") < self.max_position_weight <= Decimal("1"):
            raise ValidationError("Invalid maximum position weight")

        if not Decimal("0") < self.max_portfolio_investment <= Decimal("1"):
            raise ValidationError("Invalid portfolio investment limit")

        if not Decimal("0") <= self.minimum_weight < self.max_position_weight:
            raise ValidationError("Invalid minimum weight")

    def allocate(
        self,
        scores: Mapping[str, Decimal],
        max_positions: int,
    ) -> AllocationResult:
        if max_positions < 1:
            raise ValidationError("max_positions must be positive")

        eligible = []

        for symbol, score in scores.items():
            value = to_decimal(score, f"{symbol} score")
            if value > 0:
                eligible.append((symbol.strip().upper(), value))

        eligible.sort(key=lambda item: (-item[1], item[0]))
        eligible = eligible[:max_positions]

        if not eligible:
            return AllocationResult(
                weights={},
                cash_weight=Decimal("1"),
                rationale="No positive eligible opportunities",
            )

        # Proportional initial weights.
        total_score = sum((score for _, score in eligible), Decimal("0"))

        proposed = {
            symbol: score / total_score * self.max_portfolio_investment
            for symbol, score in eligible
        }

        # Cap positions. Do not redistribute unused capital silently.
        weights = {
            symbol: min(weight, self.max_position_weight)
            for symbol, weight in proposed.items()
        }

        # Remove positions below the minimum threshold.
        weights = {
            symbol: weight
            for symbol, weight in weights.items()
            if weight >= self.minimum_weight
        }

        invested = sum(weights.values(), Decimal("0"))

        if invested > self.max_portfolio_investment:
            raise ValidationError("Allocator exceeded portfolio investment limit")

        return AllocationResult(
            weights=weights,
            cash_weight=Decimal("1") - invested,
            rationale=(
                "Positive-score proportional allocation, position caps applied; "
                "unused capital retained as cash"
            ),
        )
