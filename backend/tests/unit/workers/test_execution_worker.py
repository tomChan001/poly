from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from backend.app.core.config import TradingMode
from backend.app.domain.enums import MappingStatus, Venue
from backend.app.services.execution import (
    AuthorizationRejected,
    ControlledExecutionService,
    ExecutionAuthorizationService,
    ExecutionEvidence,
    OrderSubmissionResult,
)
from backend.app.services.system_control import SystemControl
from backend.app.workers.execution import ExecutionWorker

NOW = datetime(2026, 8, 18, 4, 0, tzinfo=UTC)


def evidence(book_sequence: str = "k-book") -> ExecutionEvidence:
    return ExecutionEvidence(
        quote_evaluation_id="quote-worker",
        rule_versions=("k-rule", "p-rule"),
        book_sequences=(book_sequence, "p-book"),
        balance_versions=("k-balance", "p-balance"),
        risk_policy_version="risk-1",
        capital_reservation_id="reserve-worker",
        quantity=Decimal(10),
        kalshi_market_id="K",
        polymarket_market_id="P",
        kalshi_outcome="NO",
        polymarket_outcome="YES",
        kalshi_limit_price=Decimal("0.70"),
        polymarket_limit_price=Decimal("0.20"),
        conservative_roi=Decimal("0.08"),
        minimum_roi=Decimal("0.03"),
    )


class NoSubmissionPort:
    def __init__(self) -> None:
        self.calls = 0

    async def submit_fok(self, request: object) -> OrderSubmissionResult:
        self.calls += 1
        raise AssertionError("stale authorization must not submit")

    async def find_by_client_order_id(self, client_order_id: str) -> OrderSubmissionResult | None:
        return None


@pytest.mark.asyncio
async def test_worker_discards_authorization_when_refreshed_evidence_changes() -> None:
    ports = {Venue.KALSHI: NoSubmissionPort(), Venue.POLYMARKET: NoSubmissionPort()}
    service = ControlledExecutionService(
        ports,
        SystemControl(opening_enabled=True),
        TradingMode.LIMITED_AUTO,
    )
    authorization = ExecutionAuthorizationService().issue(MappingStatus.EXACT, evidence(), NOW)
    worker = ExecutionWorker(service, lambda _: evidence("new-k-book"))

    with pytest.raises(AuthorizationRejected, match="evidence changed"):
        await worker.process(authorization, NOW + timedelta(seconds=1))

    assert all(port.calls == 0 for port in ports.values())
