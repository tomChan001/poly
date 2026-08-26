from datetime import datetime
from typing import cast

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.domain.enums import ExecutionState
from backend.app.services.execution_supervisor import ExecutionIncident


class PostgresIncidentStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, idempotency_key: str) -> ExecutionIncident | None:
        async with self._sessions() as session:
            result = await session.execute(
                text("SELECT * FROM execution_incident WHERE idempotency_key = :key"),
                {"key": idempotency_key},
            )
            row = result.first()
            return None if row is None else self._incident(row._mapping)

    async def add_if_absent(self, incident: ExecutionIncident) -> ExecutionIncident:
        stored, _claimed = await self.claim(incident)
        return stored

    async def claim(
        self,
        incident: ExecutionIncident,
    ) -> tuple[ExecutionIncident, bool]:
        async with self._sessions.begin() as session:
            result = await session.execute(
                text(
                    """
                    INSERT INTO execution_incident (
                        idempotency_key, correlation_id, state, action, simulated,
                        unhedged_quantity, occurred_at
                    ) VALUES (
                        :key, :correlation_id, :state, :action, :simulated,
                        :unhedged_quantity, :occurred_at
                    )
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING *
                    """
                ),
                {
                    "key": incident.idempotency_key,
                    "correlation_id": incident.correlation_id,
                    "state": incident.state.value,
                    "action": incident.action,
                    "simulated": incident.simulated,
                    "unhedged_quantity": incident.unhedged_quantity,
                    "occurred_at": incident.occurred_at,
                },
            )
            row = result.first()
            if row is not None:
                return self._incident(row._mapping), True
            existing = await session.execute(
                text("SELECT * FROM execution_incident WHERE idempotency_key = :key"),
                {"key": incident.idempotency_key},
            )
            return self._incident(existing.one()._mapping), False

    @staticmethod
    def _incident(row: RowMapping) -> ExecutionIncident:
        return ExecutionIncident(
            idempotency_key=cast(str, row["idempotency_key"]),
            correlation_id=cast(str, row["correlation_id"]),
            state=ExecutionState(cast(str, row["state"])),
            action=cast(str, row["action"]),
            simulated=cast(bool, row["simulated"]),
            unhedged_quantity=cast(str, row["unhedged_quantity"]),
            occurred_at=cast(datetime, row["occurred_at"]),
        )
