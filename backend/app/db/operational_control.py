from datetime import UTC, datetime
from typing import cast

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.services.system_control import OpeningControlState

_OPENING_CONTROL = "opening"


class PostgresOperationalControlStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def load_opening(self) -> OpeningControlState | None:
        async with self._sessions() as session:
            result = await session.execute(
                text("SELECT * FROM system_control WHERE name = :name"),
                {"name": _OPENING_CONTROL},
            )
            row = result.first()
            return None if row is None else self._state(row._mapping)

    async def save_opening(
        self,
        *,
        enabled: bool,
        reason: str,
        changed_by: str,
    ) -> OpeningControlState:
        changed_at = datetime.now(UTC)
        async with self._sessions.begin() as session:
            result = await session.execute(
                text(
                    """
                    INSERT INTO system_control (
                        name, enabled, version, changed_at, changed_by, reason
                    ) VALUES (
                        :name, :enabled, 1, :changed_at, :changed_by, :reason
                    )
                    ON CONFLICT (name) DO UPDATE SET
                        enabled = EXCLUDED.enabled,
                        version = system_control.version + 1,
                        changed_at = EXCLUDED.changed_at,
                        changed_by = EXCLUDED.changed_by,
                        reason = EXCLUDED.reason
                    RETURNING *
                    """
                ),
                {
                    "name": _OPENING_CONTROL,
                    "enabled": enabled,
                    "changed_at": changed_at,
                    "changed_by": changed_by,
                    "reason": reason,
                },
            )
            return self._state(result.one()._mapping)

    @staticmethod
    def _state(row: RowMapping) -> OpeningControlState:
        return OpeningControlState(
            opening_enabled=cast(bool, row["enabled"]),
            reason=cast(str, row["reason"]),
            version=cast(int, row["version"]),
            changed_by=cast(str, row["changed_by"]),
            changed_at=cast(datetime, row["changed_at"]),
        )
