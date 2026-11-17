"""Main orchestrator coordinating the complete cycle.

CORRECTIONS:
- Proper recovery from interruption
- Reconciliation before risk-increasing activity
- Quarantine for uncertain state
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Dict, List

from apexk.config import Config
from apexk.contracts import (
    EventType, AccountState, Decision, DecisionAction, to_decimal
)
from apexk.ledger.events import EventLog
from apexk.ledger.accounting import replay_to_state
from apexk.risk.controller import RiskController
from apexk.hands.broker import AlpacaPaperBroker, PostStatus
from apexk.hands.orders import OrderManager
from apexk.eyes.market_data import MarketDataFeed
from apexk.brain.decision import DecisionEngine
from apexk.brain.strategy import StrategyProvider, NoOpStrategy
from apexk.allocator.dynamic import DynamicAllocator

log = logging.getLogger("apexk.orchestrator")

class Orchestrator:
    """Coordinates the complete operating cycle."""
    
    def __init__(
        self,
        config: Config,
        event_log: EventLog,
        broker: Optional[AlpacaPaperBroker],
        strategy: Optional[StrategyProvider] = None
    ):
        self.config = config
        self.event_log = event_log
        self.broker = broker
        self.strategy = strategy or NoOpStrategy()
        
        self.risk = RiskController(config)
        self.decision_engine = DecisionEngine(config, self.strategy)
        self.allocator = DynamicAllocator(config)
        self.market_data = MarketDataFeed(config)
        
        if broker:
            self.order_manager = OrderManager(event_log, broker)
        else:
            self.order_manager = None
    
    def get_state(self) -> AccountState:
        """Replay event log to get current state."""
        return replay_to_state(self.event_log.replay())
    
    def seed_account(self, amount: Decimal) -> None:
        """Seed paper account with initial cash."""
        now = datetime.now(timezone.utc).isoformat()
        self.event_log.make(
            idempotency_key=f"seed-{now}",
            type=EventType.CASH_MOVEMENT,
            occurred_at=now,
            recorded_at=now,
            payload={"amount": str(amount), "reason": "paper_seed"},
            config_hash=self.config.hash()
        )
        log.info(f"Seeded account with ${amount}")
    
    def reconcile_with_broker(self) -> bool:
        """Reconcile ledger with broker state.
        
        Returns True if reconciliation is complete and reliable.
        """
        if not self.broker:
            return True  # No broker, nothing to reconcile
        
        # Get broker positions
        broker_positions = self.broker.get_positions()
        if broker_positions is None:
            log.warning("Cannot fetch broker positions - reconciliation incomplete")
            return False
        
        # Get ledger state
        state = self.get_state()
        
        # Compare positions
        broker_symbols = {p['symbol'] for p in broker_positions}
        ledger_symbols = set(state.positions.keys())
        
        # Check for positions in broker but not in ledger
        for pos in broker_positions:
            symbol = pos['symbol']
            broker_qty = to_decimal(pos['qty'])
            
            if symbol not in state.positions:
                log.warning(f"Position {symbol} in broker but not in ledger")
                # This requires reconciliation correction
                return False
            
            ledger_qty = state.positions[symbol].qty
            if abs(broker_qty - ledger_qty) > Decimal('0.000001'):
                log.warning(
                    f"Position mismatch for {symbol}: "
                    f"broker={broker_qty}, ledger={ledger_qty}"
                )
                return False
        
        return True
    
    def run_cycle(self, observations: Dict) -> None:
        """Run one complete decision cycle."""
        now = datetime.now(timezone.utc).isoformat()
        
        # 1. Reconcile before any risk-increasing activity
        if not self.reconcile_with_broker():
            log.warning("Reconciliation incomplete - risk-reducing actions only")
            # Only allow exits/reductions
            allow_risk_increasing = False
        else:
            allow_risk_increasing = True
        
        # 2. Get current state
        state = self.get_state()
        log.info(
            f"Cycle start: NAV=${state.nav()}, cash=${state.cash}, "
            f"positions={len(state.positions)}"
        )
        
        # 3. Process market data
        for symbol, obs in observations.items():
            self.market_data.process_observation(obs)
        
        valid_observations = self.market_data.get_all_observations()
        
        # 4. Make decisions
        decisions = self.decision_engine.make_decisions(valid_observations, state)
        
        # 5. Allocate capital
        forecasts = {}
        if self.strategy.is_available():
            for f in self.strategy.get_forecasts(valid_observations, state):
                forecasts[f.symbol] = f
        
        decisions = self.allocator.allocate(state, decisions, forecasts)
        
        # 6. Execute decisions
        for decision in decisions:
            self._execute_decision(decision, state, allow_risk_increasing)
        
        log.info("Cycle complete")
    
    def _execute_decision(
        self, 
        decision: Decision, 
        state: AccountState,
        allow_risk_increasing: bool
    ) -> None:
        """Execute a single decision."""
        # Check if action is allowed
        if decision.is_risk_increasing and not allow_risk_increasing:
            log.warning(
                f"{decision.symbol}: Risk-increasing action blocked "
                f"(reconciliation incomplete)"
            )
            return
        
        # Risk check
        verdict = self.risk.check_decision(state, decision)
        
        if not verdict.approved:
            log.warning(f"{decision.symbol}: Risk rejected: {verdict.reason}")
            
            # Record risk veto
            now = datetime.now(timezone.utc).isoformat()
            self.event_log.make(
                idempotency_key=f"veto-{decision.symbol}-{now}",
                type=EventType.RISK_VETO,
                occurred_at=now,
                recorded_at=now,
                payload={
                    "symbol": decision.symbol,
                    "action": decision.action.value,
                    "reason": verdict.reason
                },
                config_hash=self.config.hash()
            )
            return
        
        # Execute based on action type
        if decision.action == DecisionAction.BUY:
            self._execute_buy(decision, verdict.adjusted_qty)
        elif decision.action == DecisionAction.REDUCE:
            self._execute_sell(decision, verdict.adjusted_qty)
        elif decision.action == DecisionAction.EXIT:
            self._execute_sell(decision, verdict.adjusted_qty)
        elif decision.action == DecisionAction.HOLD:
            log.info(f"{decision.symbol}: HOLD")
        elif decision.action == DecisionAction.CASH:
            log.info(f"{decision.symbol}: Stay in cash")
    
    def _execute_buy(self, decision: Decision, qty: Decimal) -> None:
        """Execute a BUY decision."""
        if not self.order_manager:
            log.warning("No broker available - dry run")
            return
        
        # Create intent first
        record = self.order_manager.create_intent(
            symbol=decision.symbol,
            side=decision.action,  # Will be converted to Side.BUY
            qty=qty,
            price=decision.price
        )
        
        # Submit
        success = self.order_manager.submit(record)
        if success:
            log.info(f"{decision.symbol}: BUY {qty} submitted")
        else:
            log.warning(f"{decision.symbol}: BUY submission failed")
    
    def _execute_sell(self, decision: Decision, qty: Decimal) -> None:
        """Execute a SELL decision."""
        if not self.order_manager:
            log.warning("No broker available - dry run")
            return
        
        from apexk.contracts import Side
        
        record = self.order_manager.create_intent(
            symbol=decision.symbol,
            side=Side.SELL,
            qty=qty,
            price=decision.price
        )
        
        success = self.order_manager.submit(record)
        if success:
            log.info(f"{decision.symbol}: SELL {qty} submitted")
        else:
            log.warning(f"{decision.symbol}: SELL submission failed")
