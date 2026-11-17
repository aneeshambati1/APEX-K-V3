"""Versioned, hashed configuration with explicit assumptions."""
from __future__ import annotations
import hashlib
import json
import yaml
from dataclasses import dataclass, asdict
from decimal import Decimal
from pathlib import Path
from typing import Dict, Any, Optional

@dataclass
class Config:
    config_version: str = "0.2.0-provisional"
    
    # ---- hard safety limits (NOT optimisable) ----
    # ASSUMPTION (PROVISIONAL): long-only, unlevered paper account
    max_gross_notional_frac: Decimal = Decimal('1.00')
    max_single_position_frac: Decimal = Decimal('0.25')
    min_cash_reserve_frac: Decimal = Decimal('0.02')
    max_adv_participation: Decimal = Decimal('0.05')
    
    # ---- optimisable parameters (provisional defaults) ----
    risk_aversion: Decimal = Decimal('3.0')
    target_vol_annual: Decimal = Decimal('0.15')
    cost_bps_per_side: Decimal = Decimal('5.0')
    
    # ---- research toggles (OFF until evidence) ----
    enable_tactical: bool = False
    enable_vol_targeting: bool = False
    enable_regime_derisk: bool = False
    
    # ---- broker ----
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_data_url: str = "https://data.alpaca.markets"
    
    # ---- data validation ----
    max_data_age_minutes: int = 15
    min_price: Decimal = Decimal('0.01')
    max_price: Decimal = Decimal('100000.00')
    
    def __post_init__(self):
        # Ensure Decimal fields are actually Decimal
        for field_name in ['max_gross_notional_frac', 'max_single_position_frac',
                          'min_cash_reserve_frac', 'max_adv_participation',
                          'risk_aversion', 'target_vol_annual', 'cost_bps_per_side',
                          'min_price', 'max_price']:
            if not isinstance(getattr(self, field_name), Decimal):
                setattr(self, field_name, Decimal(str(getattr(self, field_name))))
    
    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Convert Decimals to strings for JSON serialization
        for k, v in d.items():
            if isinstance(v, Decimal):
                d[k] = str(v)
        return d
    
    def hash(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()
    
    @classmethod
    def from_yaml(cls, path: str) -> "Config":
        """Load config from YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)
    
    def save_yaml(self, path: str) -> None:
        """Save config to YAML file."""
        with open(path, 'w') as f:
            yaml.dump(self.to_dict(), f, default_flow_style=False)
