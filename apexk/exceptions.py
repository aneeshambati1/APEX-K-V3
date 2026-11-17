"""Custom exceptions for explicit error handling."""

class ApexKError(Exception):
    """Base exception for all APEX-K errors."""
    pass

class InvalidEventError(ApexKError):
    """Raised when an event fails validation."""
    pass

class DuplicateEventError(ApexKError):
    """Raised on duplicate idempotency key (informational, not fatal)."""
    pass

class RiskLimitExceededError(ApexKError):
    """Raised when a risk limit would be breached."""
    pass

class BrokerError(ApexKError):
    """Raised on broker communication failures."""
    pass

class DataValidationError(ApexKError):
    """Raised when market data fails validation."""
    pass

class ReconciliationError(ApexKError):
    """Raised when ledger/broker state diverges."""
    pass

class QuarantineError(ApexKError):
    """Raised when system enters quarantine state."""
    pass
