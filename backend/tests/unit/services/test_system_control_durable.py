import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.exc import ProgrammingError

from backend.app.services.system_control import (
    InMemoryOpeningControlStore,
    OpeningControlState,
    SystemControl,
)


class SharedDurableOpeningStore:
    def __init__(self) -> None:
        self.state = OpeningControlState(False, "safe default")
        self._lock = asyncio.Lock()

    async def load_opening(self) -> OpeningControlState | None:
        return self.state

    async def save_opening(
        self, *, enabled: bool, reason: str, changed_by: str
    ) -> OpeningControlState:
        async with self._lock:
            self.state = OpeningControlState(
                enabled,
                reason,
                self.state.version + 1,
                changed_by,
            )
            return self.state

    @asynccontextmanager
    async def opening_submission_guard(self):
        async with self._lock:
            yield self.state


@pytest.mark.asyncio
async def test_refresh_observes_shared_durable_opening_state() -> None:
    store = SharedDurableOpeningStore()
    first = SystemControl(store=store)
    second = SystemControl(store=store)

    await first.set_opening_async(True, "operator authorized")
    assert (await second.refresh_async()).opening_enabled is True

    await second.disable_opening_async("incident")
    assert (await first.refresh_async()).opening_enabled is False


@pytest.mark.asyncio
async def test_empty_durable_control_fails_closed_even_when_local_cache_is_open() -> None:
    store = InMemoryOpeningControlStore()
    control = SystemControl(opening_enabled=True, reason="stale local cache", store=store)

    state = await control.refresh_async()

    assert state.opening_enabled is False
    assert state.reason == "durable control not initialized"
    async with control.opening_submission_guard() as permission:
        assert permission.allowed is False


@pytest.mark.asyncio
async def test_in_memory_store_serializes_disable_with_submission_guard() -> None:
    store = InMemoryOpeningControlStore()
    control = SystemControl(store=store)
    await control.set_opening_async(True, "operator authorized")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_guard() -> None:
        async with control.opening_submission_guard() as permission:
            assert permission.allowed
            entered.set()
            await release.wait()

    holder = asyncio.create_task(hold_guard())
    await entered.wait()
    disabling = asyncio.create_task(control.disable_opening_async("incident"))
    await asyncio.sleep(0)
    assert not disabling.done()
    release.set()
    await holder
    await disabling


class ProgrammingFailureStore:
    async def load_opening(self) -> OpeningControlState | None:
        raise ProgrammingError("SELECT malformed", {}, Exception("syntax"))

    async def save_opening(self, **_: object) -> OpeningControlState:
        raise ProgrammingError("INSERT malformed", {}, Exception("syntax"))

    @asynccontextmanager
    async def opening_submission_guard(self):
        raise ProgrammingError("SELECT malformed", {}, Exception("syntax"))
        yield None


@pytest.mark.asyncio
async def test_programming_errors_are_not_reclassified_as_persistence_outages() -> None:
    control = SystemControl(store=ProgrammingFailureStore())

    with pytest.raises(ProgrammingError):
        await control.refresh_async()
    with pytest.raises(ProgrammingError):
        await control.set_opening_async(True, "operator authorized")


@pytest.mark.asyncio
async def test_disable_waits_for_active_submission_guard_and_blocks_new_guards() -> None:
    store = SharedDurableOpeningStore()
    control = SystemControl(store=store)
    await control.set_opening_async(True, "operator authorized")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_guard() -> None:
        async with control.opening_submission_guard() as permission:
            assert permission.allowed is True
            entered.set()
            await release.wait()

    holder = asyncio.create_task(hold_guard())
    await entered.wait()
    disabling = asyncio.create_task(control.disable_opening_async("incident"))
    await asyncio.sleep(0)
    assert disabling.done() is False

    release.set()
    await holder
    await disabling

    async with control.opening_submission_guard() as permission:
        assert permission.allowed is False
