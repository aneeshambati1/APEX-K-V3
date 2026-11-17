"""Configuration loading and validation."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import yaml

from apexk.exceptions import ConfigurationError


@dataclass(frozen=True)
class AppConfig:
    mode: str
    paper_trading: bool
    base_currency: str
    initial_cash: float
    data_provider: str
    broker_provider: str
    max_positions: int
    log_level: str


def _required(data: Dict[str, Any], key: str) -> Any:
    if key not in data:
        raise ConfigurationError(f"Missing configuration key: {key}")
    return data[key]


def load_config(path: str = "config/config.yaml") -> AppConfig:
    """Load and validate application configuration."""
    config_path = Path(path)

    if not config_path.is_file():
        raise ConfigurationError(
            f"Configuration file does not exist: {config_path}"
        )

    try:
        with config_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Invalid YAML configuration: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigurationError("Configuration root must be a mapping")

    try:
        mode = str(_required(raw, "mode")).lower()
        paper_trading = bool(_required(raw, "paper_trading"))
        base_currency = str(_required(raw, "base_currency")).upper()
        initial_cash = float(_required(raw, "initial_cash"))
        data_provider = str(_required(raw, "data_provider"))
        broker_provider = str(_required(raw, "broker_provider"))
        max_positions = int(_required(raw, "max_positions"))
        log_level = str(raw.get("log_level", "INFO")).upper()
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"Invalid configuration value: {exc}") from exc

    if mode != "paper":
        raise ConfigurationError("Only paper mode is permitted")
    if paper_trading is not True:
        raise ConfigurationError("paper_trading must be true")
    if not base_currency:
        raise ConfigurationError("base_currency cannot be empty")
    if initial_cash <= 0:
        raise ConfigurationError("initial_cash must be positive")
    if max_positions < 1:
        raise ConfigurationError("max_positions must be at least 1")
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError("Invalid log_level")

    return AppConfig(
        mode=mode,
        paper_trading=True,
        base_currency=base_currency,
        initial_cash=initial_cash,
        data_provider=data_provider,
        broker_provider=broker_provider,
        max_positions=max_positions,
        log_level=log_level,
    )
