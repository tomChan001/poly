from dataclasses import dataclass
from typing import Any, Protocol

_SENSITIVE_KEYS = {
    "api_key",
    "api_secret",
    "authorization",
    "credential",
    "credentials",
    "passphrase",
    "password",
    "private_key",
    "secret",
    "token",
}


@dataclass(frozen=True, slots=True)
class OutboxNotification:
    idempotency_key: str
    event_type: str
    payload: dict[str, Any]
    attempts: int = 0


class OutboxStore(Protocol):
    def add_if_absent(self, event: OutboxNotification) -> OutboxNotification: ...

    async def add_if_absent_async(
        self,
        event: OutboxNotification,
    ) -> OutboxNotification: ...


class InMemoryOutbox:
    def __init__(self) -> None:
        self.events: dict[str, OutboxNotification] = {}

    def add_if_absent(self, event: OutboxNotification) -> OutboxNotification:
        return self.events.setdefault(event.idempotency_key, event)

    async def add_if_absent_async(
        self,
        event: OutboxNotification,
    ) -> OutboxNotification:
        return self.add_if_absent(event)


class NotificationService:
    def __init__(self, outbox: OutboxStore) -> None:
        self._outbox = outbox

    def enqueue(
        self,
        idempotency_key: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> OutboxNotification:
        event = OutboxNotification(idempotency_key, event_type, _sanitize(payload))
        return self._outbox.add_if_absent(event)

    async def enqueue_async(
        self,
        idempotency_key: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> OutboxNotification:
        event = OutboxNotification(idempotency_key, event_type, _sanitize(payload))
        return await self._outbox.add_if_absent_async(event)


def _sanitize(value: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, item in value.items():
        normalized_key = key.lower().replace("-", "_")
        if normalized_key in _SENSITIVE_KEYS:
            sanitized[key] = "[REDACTED]"
        else:
            sanitized[key] = _sanitize_value(item)
    return sanitized


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _sanitize(value)
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(entry) for entry in value]
    return value
