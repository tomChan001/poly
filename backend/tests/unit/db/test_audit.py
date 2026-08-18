from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.audit import AuditRecord, append_audit_event
from backend.app.db.tables import AuditEvent


class RecordingSession:
    def __init__(self) -> None:
        self.added: object | None = None

    def add(self, value: object) -> None:
        self.added = value

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_append_audit_event_accepts_immutable_record() -> None:
    session = RecordingSession()
    record = AuditRecord(
        correlation_id="corr-1",
        actor="reviewer@example.com",
        event_type="mapping.reviewed",
        object_type="pair_mapping",
        object_id="mapping-1",
        before_state={"status": "pending_review"},
        after_state={"status": "exact"},
        evidence_hash="a" * 64,
    )

    event = await append_audit_event(cast(AsyncSession, session), record)

    assert isinstance(event, AuditEvent)
    assert event.event_type == "mapping.reviewed"
    assert session.added is event
