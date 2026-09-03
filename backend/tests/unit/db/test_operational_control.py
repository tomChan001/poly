from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from backend.app.db.operational_control import PostgresOperationalControlStore


class Result:
    def __init__(self) -> None:
        self._row = type(
            "Row",
            (),
            {
                "_mapping": {
                    "enabled": True,
                    "reason": "authorized",
                    "version": 3,
                    "changed_by": "operator",
                    "changed_at": datetime.now(UTC),
                }
            },
        )()

    def first(self) -> object:
        return self._row

    def one(self) -> object:
        return self._row


class RecordingSessions:
    def __init__(self) -> None:
        self.transactions = 0
        self.executions: list[tuple[str, dict[str, object]]] = []

    @asynccontextmanager
    async def begin(self) -> AsyncIterator["RecordingSessions"]:
        self.transactions += 1
        yield self

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator["RecordingSessions"]:
        yield self

    async def execute(self, statement: object, parameters: dict[str, object] | None = None) -> Result:
        self.executions.append((str(statement), parameters or {}))
        return Result()


@pytest.mark.asyncio
async def test_postgres_guard_and_save_share_transaction_advisory_lock() -> None:
    sessions = RecordingSessions()
    store = PostgresOperationalControlStore(cast(Any, sessions))

    async with store.opening_submission_guard() as state:
        assert state is not None
        assert state.opening_enabled is True
    await store.save_opening(enabled=False, reason="incident", changed_by="operator")

    advisory_locks = [
        parameters
        for statement, parameters in sessions.executions
        if "pg_advisory_xact_lock" in statement
    ]
    assert sessions.transactions == 2
    assert advisory_locks == [
        {"key": "poly-opening-control"},
        {"key": "poly-opening-control"},
    ]
