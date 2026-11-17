"""Durable JSONL persistence for the APEX-K event ledger."""

import json
import os
from pathlib import Path
from threading import RLock
from typing import List

from apexk.contracts import Side
from apexk.exceptions import PersistenceError
from apexk.ledger.events import LedgerEvent


class EventStore:
    """Append-only event store with strict recovery validation."""

    def __init__(self, path: str = "data/ledger/events.jsonl"):
        self.path = Path(path)
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: LedgerEvent) -> None:
        """Append one event and flush it to disk."""
        if not isinstance(event, LedgerEvent):
            raise PersistenceError("Only LedgerEvent objects can be persisted")

        encoded = json.dumps(
            event.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(encoded + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except (OSError, TypeError, ValueError) as exc:
                raise PersistenceError(
                    f"Could not persist event {event.event_id}: {exc}"
                ) from exc

    def load_all(self) -> List[LedgerEvent]:
        """Load all events; reject corruption, duplicate IDs or sequence gaps."""
        with self._lock:
            if not self.path.exists():
                return []

            events = []
            seen_ids = set()
            seen_keys = set()
            expected_sequence = 1

            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        if not line.strip():
                            raise PersistenceError(
                                f"Blank ledger line at line {line_number}"
                            )

                        try:
                            raw = json.loads(line)
                            event = LedgerEvent.from_dict(raw)
                        except Exception as exc:
                            raise PersistenceError(
                                f"Invalid ledger event at line {line_number}: {exc}"
                            ) from exc

                        if event.sequence != expected_sequence:
                            raise PersistenceError(
                                f"Sequence error at line {line_number}: "
                                f"expected {expected_sequence}, got {event.sequence}"
                            )

                        if event.event_id in seen_ids:
                            raise PersistenceError(
                                f"Duplicate event ID at line {line_number}"
                            )

                        if event.idempotency_key in seen_keys:
                            raise PersistenceError(
                                f"Duplicate idempotency key at line {line_number}"
                            )

                        seen_ids.add(event.event_id)
                        seen_keys.add(event.idempotency_key)
                        events.append(event)
                        expected_sequence += 1

            except OSError as exc:
                raise PersistenceError(f"Could not read event ledger: {exc}") from exc

            return events

    def next_sequence(self) -> int:
        """Return the next sequence number after validating the existing ledger."""
        events = self.load_all()
        return len(events) + 1

    def backup(self, destination: str) -> Path:
        """Create a consistent copy of the current event file."""
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            self.load_all()

            if not self.path.exists():
                raise PersistenceError("Cannot back up a ledger that does not exist")

            temporary = target.with_suffix(target.suffix + ".tmp")

            try:
                with self.path.open("rb") as source:
                    with temporary.open("wb") as output:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())

                os.replace(temporary, target)

                directory_fd = os.open(str(target.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)

            except OSError as exc:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                raise PersistenceError(f"Backup failed: {exc}") from exc

        return target
