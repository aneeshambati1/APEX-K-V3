"""APEX-K MAJOR startup entry point.

Safe startup: validates configuration and initializes paper-only components.
No broker orders are submitted by this file.
"""

import logging
import sys

from apexk.allocator.dynamic import DynamicAllocator
from apexk.brain.strategy import NoOpStrategy
from apexk.config import load_config
from apexk.orchestrator import APEXKOrchestrator
from apexk.risk.controller import RiskController, RiskLimits
from apexk.hands.broker import PaperBroker
from apexk.hands.orders import OrderManager
from apexk.ledger.accounting import AccountingBook


def main() -> int:
    try:
        config = load_config("config/config.yaml")

        logging.basicConfig(
            level=getattr(logging, config.log_level),
            format="%(asctime)s | %(levelname)s | %(message)s",
        )

        # Initialize paper-only components.
        accounting = AccountingBook(
            initial_cash=str(config.initial_cash)
        )
        broker = PaperBroker()
        order_manager = OrderManager(broker)
        risk_controller = RiskController(RiskLimits())

        orchestrator = APEXKOrchestrator(
            strategy=NoOpStrategy(),
            allocator=DynamicAllocator(),
            max_positions=config.max_positions,
        )

        logging.info("APEX-K MAJOR initialized")
        logging.info("Mode: %s", config.mode)
        logging.info("Paper trading: %s", config.paper_trading)
        logging.info("Initial cash: %s %s", config.initial_cash, config.base_currency)
        logging.info("Broker: local in-memory paper broker")
        logging.info("Strategy: NoOpStrategy (no forecasts)")
        logging.info("No orders submitted.")

        # Keep initialized objects referenced during startup.
        _ = (accounting, broker, order_manager, risk_controller, orchestrator)

        print("\nAPEX-K MAJOR initialized successfully.")
        print("Mode: PAPER ONLY")
        print("No market data has been loaded.")
        print("No orders have been submitted.")
        print("The strategy is currently a safe no-op placeholder.")

        return 0

    except Exception as exc:
        logging.exception("APEX-K startup failed: %s", exc)
        print(f"Startup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
