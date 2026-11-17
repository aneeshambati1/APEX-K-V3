"""Independent system-health and decision monitoring."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List


@dataclass(frozen=True)
class MonitorFinding:
    severity: str
    category: str
    message: str
    timestamp: datetime


class ThirdEyeMonitor:
    """Records observations and warnings without placing or modifying orders."""

    def __init__(self):
        self._findings: List[MonitorFinding] = []

    def report(
        self,
        severity: str,
        category: str,
        message: str,
    ) -> MonitorFinding:
        severity = severity.strip().upper()

        if severity not in {"INFO", "WARNING", "CRITICAL"}:
            raise ValueError("Invalid monitoring severity")

        finding = MonitorFinding(
            severity=severity,
            category=category.strip(),
            message=message.strip(),
            timestamp=datetime.now(timezone.utc),
        )

        self._findings.append(finding)
        return finding

    def check_market_data(
        self,
        symbol: str,
        is_fresh: bool,
    ) -> MonitorFinding:
        if is_fresh:
            return self.report(
                "INFO",
                "MARKET_DATA",
                f"{symbol}: market data passed freshness check",
            )

        return self.report(
            "CRITICAL",
            "MARKET_DATA",
            f"{symbol}: missing or stale market data",
        )

    def get_findings(self) -> List[MonitorFinding]:
        return list(self._findings)

    def get_critical_findings(self) -> List[MonitorFinding]:
        return [
            finding
            for finding in self._findings
            if finding.severity == "CRITICAL"
        ]

    def clear(self) -> None:
        self._findings.clear()
