import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.exc import ProgrammingError, SQLAlchemyError


@dataclass(frozen=True, slots=True)
class OpeningControlState:
    opening_enabled: bool
    reason: str
    version: int = 0
    changed_by: str = "system"
    changed_at: datetime | None = None


class OpeningControlStore(Protocol):
    async def load_opening(self) -> OpeningControlState | None: ...

    def opening_submission_guard(
        self,
    ) -> AbstractAsyncContextManager[OpeningControlState | None]: ...

    async def save_opening(
        self,
        *,
        enabled: bool,
        reason: str,
        changed_by: str,
    ) -> OpeningControlState: ...


class OpeningControlPersistenceError(RuntimeError):
    """The durable opening-control store cannot accept an update."""


@dataclass(frozen=True, slots=True)
class OpeningSubmissionPermission:
    """The durable state observed while a submission lock is held."""

    allowed: bool
    state: OpeningControlState | None


class InMemoryOpeningControlStore:
    """Test/local durable-control substitute with guard and write serialization."""

    def __init__(self, state: OpeningControlState | None = None) -> None:
        self._state = state
        self._lock = asyncio.Lock()

    async def load_opening(self) -> OpeningControlState | None:
        async with self._lock:
            return self._state

    async def save_opening(
        self,
        *,
        enabled: bool,
        reason: str,
        changed_by: str,
    ) -> OpeningControlState:
        async with self._lock:
            version = 1 if self._state is None else self._state.version + 1
            self._state = OpeningControlState(
                enabled,
                reason,
                version,
                changed_by,
            )
            return self._state

    @asynccontextmanager
    async def opening_submission_guard(
        self,
    ) -> AsyncIterator[OpeningControlState | None]:
        async with self._lock:
            yield self._state


class SystemControl:
    def __init__(
        self,
        *,
        opening_enabled: bool = False,
        reason: str = "safe default",
        version: int = 0,
        changed_by: str = "system",
        changed_at: datetime | None = None,
        store: OpeningControlStore | None = None,
    ) -> None:
        self.opening_enabled = opening_enabled
        self.reason = reason
        self.version = version
        self.changed_by = changed_by
        self.changed_at = changed_at
        self._store = store
        self._submission_lock = asyncio.Lock()

    def snapshot(self) -> OpeningControlState:
        return OpeningControlState(
            opening_enabled=self.opening_enabled,
            reason=self.reason,
            version=self.version,
            changed_by=self.changed_by,
            changed_at=self.changed_at,
        )

    def disable_opening(
        self,
        reason: str,
        changed_by: str = "system",
    ) -> OpeningControlState:
        return self.set_opening(False, reason, changed_by)

    def set_opening(
        self,
        enabled: bool,
        reason: str,
        changed_by: str = "system",
    ) -> OpeningControlState:
        state = OpeningControlState(
            opening_enabled=enabled,
            reason=reason,
            version=self.version + 1,
            changed_by=changed_by,
        )
        self._apply(state)
        return state

    async def load_async(self) -> OpeningControlState:
        return await self.refresh_async()

    async def refresh_async(self) -> OpeningControlState:
        if self._store is None:
            return self.snapshot()
        try:
            state = await self._store.load_opening()
        except (OSError, SQLAlchemyError) as exc:
            if isinstance(exc, ProgrammingError):
                raise
            raise OpeningControlPersistenceError(
                "opening control persistence unavailable"
            ) from exc
        if state is None:
            return self._apply_uninitialized_durable_state()
        self._apply(state)
        return state

    @asynccontextmanager
    async def opening_submission_guard(
        self,
    ) -> AsyncIterator[OpeningSubmissionPermission]:
        """Hold the durable opening-control lock across an external write.

        Callers must submit only when the yielded permission is allowed.  A
        durable store re-reads its state after acquiring its cross-process lock;
        without one, this instance lock serializes local state changes.
        """
        if self._store is None:
            async with self._submission_lock:
                state = self.snapshot()
                yield OpeningSubmissionPermission(state.opening_enabled, state)
            return
        try:
            async with self._store.opening_submission_guard() as durable_state:
                if durable_state is None:
                    durable_state = self._apply_uninitialized_durable_state()
                else:
                    self._apply(durable_state)
                yield OpeningSubmissionPermission(
                    durable_state.opening_enabled,
                    durable_state,
                )
        except (OSError, SQLAlchemyError) as exc:
            if isinstance(exc, ProgrammingError):
                raise
            raise OpeningControlPersistenceError(
                "opening control persistence unavailable"
            ) from exc

    async def disable_opening_async(
        self,
        reason: str,
        *,
        changed_by: str = "system",
    ) -> OpeningControlState:
        return await self.set_opening_async(False, reason, changed_by=changed_by)

    async def set_opening_async(
        self,
        enabled: bool,
        reason: str,
        *,
        changed_by: str = "system",
    ) -> OpeningControlState:
        if self._store is None:
            return self.set_opening(enabled, reason, changed_by)
        if not enabled:
            self.set_opening(False, reason, changed_by)
        try:
            state = await self._store.save_opening(
                enabled=enabled,
                reason=reason,
                changed_by=changed_by,
            )
        # TimeoutError and connection failures such as ConnectionRefusedError
        # are OSErrors; programming and cancellation errors remain visible.
        except (OSError, SQLAlchemyError) as exc:
            if isinstance(exc, ProgrammingError):
                raise
            raise OpeningControlPersistenceError(
                "opening control persistence unavailable"
            ) from exc
        self._apply(state)
        return state

    def _apply(self, state: OpeningControlState) -> None:
        self.opening_enabled = state.opening_enabled
        self.reason = state.reason
        self.version = state.version
        self.changed_by = state.changed_by
        self.changed_at = state.changed_at

    def _apply_uninitialized_durable_state(self) -> OpeningControlState:
        state = OpeningControlState(
            opening_enabled=False,
            reason="durable control not initialized",
            version=self.version,
            changed_by="system",
        )
        self._apply(state)
        return state
