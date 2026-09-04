from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class OpportunityRecord:
    id: str
    event: str
    kalshi_outcome: str
    polymarket_outcome: str
    mapping_status: str
    quantity: Decimal | None
    kalshi_vwap: Decimal | None
    polymarket_vwap: Decimal | None
    total_fees: Decimal | None
    deployed_capital: Decimal | None
    payout: Decimal | None
    profit_floor: Decimal | None
    conservative_roi: Decimal | None
    expected_settlement_at: datetime
    worst_case_settlement_at: datetime
    book_age_ms: int | None
    rejection_reasons: tuple[str, ...]
    rule_versions: tuple[str, str] = field(default_factory=lambda: ("unavailable", "unavailable"))
    book_sequences: tuple[str, str] = field(
        default_factory=lambda: ("unavailable", "unavailable")
    )
    balance_versions: tuple[str, str] = field(
        default_factory=lambda: ("unavailable", "unavailable")
    )
    risk_policy_version: str = "unavailable"
    fee_status: str = "unknown"

    @classmethod
    def example(
        cls,
        identifier: str,
        roi: Decimal,
        rejection_reasons: tuple[str, ...],
    ) -> "OpportunityRecord":
        now = datetime.now(UTC)
        quantity = Decimal(10)
        deployed = Decimal("9.00")
        return cls(
            id=identifier,
            event=f"Example opportunity {identifier}",
            kalshi_outcome="NO",
            polymarket_outcome="YES",
            mapping_status="exact",
            quantity=quantity,
            kalshi_vwap=Decimal("0.70"),
            polymarket_vwap=Decimal("0.20"),
            total_fees=Decimal(0),
            deployed_capital=deployed,
            payout=quantity,
            profit_floor=quantity - deployed,
            conservative_roi=roi,
            expected_settlement_at=now + timedelta(days=14),
            worst_case_settlement_at=now + timedelta(days=21),
            book_age_ms=180,
            rejection_reasons=rejection_reasons,
        )


class InMemoryOpportunityStore:
    def __init__(self) -> None:
        self._records: dict[str, OpportunityRecord] = {}

    def replace(self, records: list[OpportunityRecord]) -> None:
        self._records = {record.id: record for record in records}

    def list_ranked(self) -> list[OpportunityRecord]:
        return sorted(
            self._records.values(),
            key=lambda item: (
                item.conservative_roi is not None,
                item.conservative_roi or Decimal(0),
                item.profit_floor is not None,
                item.profit_floor or Decimal(0),
            ),
            reverse=True,
        )

    def get(self, identifier: str) -> OpportunityRecord:
        return self._records[identifier]
