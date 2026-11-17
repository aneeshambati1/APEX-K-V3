"""Validated event contracts for the APEX-K ledger."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Mapping
from uuid import uuid4

from apexk.exceptions import InvalidEventError


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LedgerEvent:
    event_type: str
    sequence: int
    payload: Mapping[str, Any]
    idempotency_key: str
    event_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = field(default_factory=_utc_now)
    schema_version: int = 1

    def __post_init__(self):
        if not isinstance(self.event_type, str) or not self.event_type.strip():
            raise InvalidEventError("event_type is required")

        if not isinstance(self.sequence, int) or self.sequence < 1:
            raise InvalidEventError("sequence must be a positive integer")

        if not isinstance(self.payload, Mapping):
            raise InvalidEventError("payload must be a mapping")

        if not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip():
            raise InvalidEventError("idempotency_key is required")

        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise InvalidEventError("event_id is required")

        if self.schema_version != 1:
            raise InvalidEventError("Unsupported event schema version")

        if not isinstance(self.timestamp, datetime):
            raise InvalidEventError("timestamp must be a datetime")

        if self.timestamp.tzinfo is None:
            raise InvalidEventError("timestamp must include a timezone")

        object.__setattr__(self, "event_type", self.event_type.strip().upper())
        object.__setattr__(self, "payload", dict(self.payload))

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable event representation."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "sequence": self.sequence,
            "timestamp": self.timestamp.isoformat(),
            "idempotency_key": self.idempotency_key,
            "schema_version": self.schema_version,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LedgerEvent":
        """Reconstruct an event from a persisted mapping."""
        if not isinstance(data, Mapping):
            raise InvalidEventError("Persisted event must be a mapping")

        required = {
            "event_type",
            "sequence",
            "timestamp",
            "idempotency_key",
            "payload",
        }

        missing = required.difference(data.keys())
        if missing:
            raise InvalidEventError(
                f"Persisted event is missing fields: {sorted(missing)}"
            )

        try:
            timestamp = datetime.fromisoformat(
                str(data["timestamp"]).replace("Z", "+00:00")
            )

            return cls(
                event_id=str(data.get("event_id") or uuid4()),
                event_type=str(data["event_type"]),
                sequence=int(data["sequence"]),
                timestamp=timestamp,
                idempotency_key=str(data["idempotency_key"]),
                schema_version=int(data.get("schema_version", 1)),
                payload=data["payload"],
            )
        except (TypeError, ValueError) as exc:
            raise InvalidEventError(
                f"Could not reconstruct persisted event: {exc}"
            ) from exc
