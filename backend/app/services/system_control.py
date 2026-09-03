from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.exc import SQLAlchemyError


@dataclass(frozen=True, slots=True)
class OpeningControlState:
    opening_enabled: bool
    reason: str
    version: int = 0
    changed_by: str = "system"
    changed_at: datetime | None = None


class OpeningControlStore(Protocol):
    async def load_opening(self) -> OpeningControlState | None: ...

    async def save_opening(
        self,
        *,
        enabled: bool,
        reason: str,
        changed_by: str,
    ) -> OpeningControlState: ...


class OpeningControlPersistenceError(RuntimeError):
    """The durable opening-control store cannot accept an update."""


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
        if self._store is None:
            return self.snapshot()
        state = await self._store.load_opening()
        if state is None:
            return self.snapshot()
        self._apply(state)
        return state

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
        except SQLAlchemyError as exc:
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
