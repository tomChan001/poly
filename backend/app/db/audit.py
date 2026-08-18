from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.tables import AuditEvent


@dataclass(frozen=True, slots=True)
class AuditRecord:
    correlation_id: str
    actor: str
    event_type: str
    object_type: str
    object_id: str
    before_state: dict[str, Any] | None
    after_state: dict[str, Any] | None
    evidence_hash: str


async def append_audit_event(session: AsyncSession, record: AuditRecord) -> AuditEvent:
    """Append one audit row; callers are never given update/delete helpers."""
    event = AuditEvent(**asdict(record))
    session.add(event)
    await session.flush()
    return event
