from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.db.submission_fence import lock_submission_fence
from backend.app.services.settings import RiskPolicy, RiskPolicyInput


class PostgresRiskPolicyStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self.current: RiskPolicy | None = None

    async def initialize(self) -> RiskPolicy:
        async with self._sessions.begin() as session:
            await lock_submission_fence(session)
            result = await session.execute(
                text(
                    "SELECT * FROM risk_policy_version "
                    "ORDER BY created_at DESC, version DESC LIMIT 1"
                )
            )
            row = result.first()
            if row is None:
                policy = _snapshot(RiskPolicyInput.defaults())
                await _insert(session, policy)
            else:
                policy = self._policy(row._mapping)
        self.current = policy
        return policy

    async def refresh(self) -> RiskPolicy | None:
        """Load the current durable version without seeding an empty database."""
        async with self._sessions() as session:
            result = await session.execute(
                text(
                    "SELECT * FROM risk_policy_version "
                    "ORDER BY created_at DESC, version DESC LIMIT 1"
                )
            )
            row = result.first()
        self.current = None if row is None else self._policy(row._mapping)
        return self.current

    async def create(self, value: RiskPolicyInput) -> RiskPolicy:
        policy = _snapshot(value)
        async with self._sessions.begin() as session:
            await lock_submission_fence(session)
            await _insert(session, policy)
        self.current = policy
        return policy

    async def get(self, version: UUID) -> RiskPolicy:
        async with self._sessions() as session:
            result = await session.execute(
                text("SELECT * FROM risk_policy_version WHERE version = :version"),
                {"version": version},
            )
            row = result.first()
        if row is None:
            raise KeyError(version)
        return self._policy(row._mapping)

    @staticmethod
    def _policy(row: RowMapping) -> RiskPolicy:
        return RiskPolicy(
            version=UUID(str(row["version"])),
            created_at=cast(datetime, row["created_at"]),
            minimum_roi=Decimal(str(row["minimum_roi"])),
            maximum_settlement_days=int(row["maximum_settlement_days"]),
            maximum_book_age_seconds=Decimal(str(row["maximum_book_age_seconds"])),
            per_trade_limit=Decimal(str(row["per_trade_limit"])),
            per_event_limit=Decimal(str(row["per_event_limit"])),
            portfolio_limit=Decimal(str(row["portfolio_limit"])),
            explicit_cost=Decimal(str(row["explicit_cost"])),
            risk_buffer=Decimal(str(row["risk_buffer"])),
            maximum_unhedged_seconds=Decimal(str(row["maximum_unhedged_seconds"])),
            maximum_unhedged_loss=Decimal(str(row["maximum_unhedged_loss"])),
            maximum_arrival_gap_seconds=Decimal(
                str(row["maximum_arrival_gap_seconds"])
            ),
        )


def _parameters(policy: RiskPolicy) -> dict[str, object]:
    return {
        "version": policy.version,
        "created_at": policy.created_at,
        "minimum_roi": policy.minimum_roi,
        "maximum_settlement_days": policy.maximum_settlement_days,
        "maximum_book_age_seconds": policy.maximum_book_age_seconds,
        "per_trade_limit": policy.per_trade_limit,
        "per_event_limit": policy.per_event_limit,
        "portfolio_limit": policy.portfolio_limit,
        "explicit_cost": policy.explicit_cost,
        "risk_buffer": policy.risk_buffer,
        "maximum_unhedged_seconds": policy.maximum_unhedged_seconds,
        "maximum_unhedged_loss": policy.maximum_unhedged_loss,
        "maximum_arrival_gap_seconds": policy.maximum_arrival_gap_seconds,
    }


def _snapshot(value: RiskPolicyInput) -> RiskPolicy:
    return RiskPolicy(
        version=uuid4(),
        created_at=datetime.now(UTC),
        minimum_roi=value.minimum_roi,
        maximum_settlement_days=value.maximum_settlement_days,
        maximum_book_age_seconds=value.maximum_book_age_seconds,
        per_trade_limit=value.per_trade_limit,
        per_event_limit=value.per_event_limit,
        portfolio_limit=value.portfolio_limit,
        explicit_cost=value.explicit_cost,
        risk_buffer=value.risk_buffer,
        maximum_unhedged_seconds=value.maximum_unhedged_seconds,
        maximum_unhedged_loss=value.maximum_unhedged_loss,
        maximum_arrival_gap_seconds=value.maximum_arrival_gap_seconds,
    )


async def _insert(session: AsyncSession, policy: RiskPolicy) -> None:
    await session.execute(
        text(
            """
            INSERT INTO risk_policy_version (
                version, created_at, minimum_roi, maximum_settlement_days,
                maximum_book_age_seconds, per_trade_limit, per_event_limit,
                portfolio_limit, explicit_cost, risk_buffer,
                maximum_unhedged_seconds, maximum_unhedged_loss,
                maximum_arrival_gap_seconds
            ) VALUES (
                :version, :created_at, :minimum_roi, :maximum_settlement_days,
                :maximum_book_age_seconds, :per_trade_limit, :per_event_limit,
                :portfolio_limit, :explicit_cost, :risk_buffer,
                :maximum_unhedged_seconds, :maximum_unhedged_loss,
                :maximum_arrival_gap_seconds
            )
            """
        ),
        _parameters(policy),
    )
