import json
from typing import cast

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.services.notifications import OutboxNotification


class PostgresOutbox:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    def add_if_absent(self, event: OutboxNotification) -> OutboxNotification:
        raise RuntimeError("Postgres outbox writes require enqueue_async")

    async def add_if_absent_async(
        self,
        event: OutboxNotification,
    ) -> OutboxNotification:
        async with self._sessions.begin() as session:
            result = await session.execute(
                text(
                    """
                    INSERT INTO outbox_event (
                        id, created_at, idempotency_key, event_type, payload, attempts
                    ) VALUES (
                        gen_random_uuid(), now(), :key, :event_type,
                        CAST(:payload AS jsonb), :attempts
                    )
                    ON CONFLICT (idempotency_key) DO UPDATE SET
                        idempotency_key = outbox_event.idempotency_key
                    RETURNING idempotency_key, event_type, payload, attempts
                    """
                ),
                {
                    "key": event.idempotency_key,
                    "event_type": event.event_type,
                    "payload": json.dumps(event.payload),
                    "attempts": event.attempts,
                },
            )
            return self._event(result.one()._mapping)

    @staticmethod
    def _event(row: RowMapping) -> OutboxNotification:
        return OutboxNotification(
            idempotency_key=cast(str, row["idempotency_key"]),
            event_type=cast(str, row["event_type"]),
            payload=dict(cast(dict, row["payload"])),
            attempts=cast(int, row["attempts"]),
        )
