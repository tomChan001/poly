from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class OutboxNotification:
    idempotency_key: str
    event_type: str
    payload: dict[str, Any]
    attempts: int = 0


class InMemoryOutbox:
    def __init__(self) -> None:
        self.events: dict[str, OutboxNotification] = {}

    def add_if_absent(self, event: OutboxNotification) -> OutboxNotification:
        return self.events.setdefault(event.idempotency_key, event)


class NotificationService:
    def __init__(self, outbox: InMemoryOutbox) -> None:
        self._outbox = outbox

    def enqueue(
        self,
        idempotency_key: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> OutboxNotification:
        event = OutboxNotification(idempotency_key, event_type, dict(payload))
        return self._outbox.add_if_absent(event)

