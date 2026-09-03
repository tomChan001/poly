from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID, uuid4


@dataclass(slots=True)
class RiskPolicyInput:
    minimum_roi: Decimal
    maximum_settlement_days: int
    maximum_book_age_seconds: Decimal
    per_trade_limit: Decimal
    per_event_limit: Decimal
    portfolio_limit: Decimal
    explicit_cost: Decimal
    risk_buffer: Decimal
    maximum_unhedged_seconds: Decimal
    maximum_unhedged_loss: Decimal
    maximum_arrival_gap_seconds: Decimal = Decimal("0.5")

    @classmethod
    def defaults(cls) -> "RiskPolicyInput":
        return cls(
            minimum_roi=Decimal("0.03"),
            maximum_settlement_days=30,
            maximum_book_age_seconds=Decimal(2),
            per_trade_limit=Decimal(10),
            per_event_limit=Decimal(25),
            portfolio_limit=Decimal(100),
            explicit_cost=Decimal(0),
            risk_buffer=Decimal("0.25"),
            maximum_unhedged_seconds=Decimal(2),
            maximum_unhedged_loss=Decimal(2),
            maximum_arrival_gap_seconds=Decimal("0.5"),
        )


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    version: UUID
    created_at: datetime
    minimum_roi: Decimal
    maximum_settlement_days: int
    maximum_book_age_seconds: Decimal
    per_trade_limit: Decimal
    per_event_limit: Decimal
    portfolio_limit: Decimal
    explicit_cost: Decimal
    risk_buffer: Decimal
    maximum_unhedged_seconds: Decimal
    maximum_unhedged_loss: Decimal
    maximum_arrival_gap_seconds: Decimal = Decimal("0.5")


class RiskPolicyStore(Protocol):
    current: RiskPolicy | None

    async def initialize(self) -> RiskPolicy: ...

    async def refresh(self) -> RiskPolicy | None: ...

    async def create(self, value: RiskPolicyInput) -> RiskPolicy: ...

    async def get(self, version: UUID) -> RiskPolicy: ...


class InMemoryRiskPolicyStore:
    def __init__(self, initial: RiskPolicyInput | None = None) -> None:
        self._versions: dict[UUID, RiskPolicy] = {}
        self.current: RiskPolicy | None = None
        if initial is not None:
            self._create(initial)

    async def initialize(self) -> RiskPolicy:
        if self.current is None:
            return self._create(RiskPolicyInput.defaults())
        return self.current

    async def refresh(self) -> RiskPolicy | None:
        return self.current

    async def create(self, value: RiskPolicyInput) -> RiskPolicy:
        return self._create(value)

    async def get(self, version: UUID) -> RiskPolicy:
        return self._versions[version]

    def _create(self, value: RiskPolicyInput) -> RiskPolicy:
        # Copying isolates historical policy versions from later UI edits.
        snapshot = replace(value)
        policy = RiskPolicy(uuid4(), datetime.now(UTC), **asdict(snapshot))
        self._versions[policy.version] = policy
        self.current = policy
        return policy
