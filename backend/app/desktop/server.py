import asyncio
import socket
from contextlib import suppress

import uvicorn
from fastapi import FastAPI

STARTUP_TIMEOUT_SECONDS = 10.0
_STARTUP_POLL_SECONDS = 0.01


def bind_loopback_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(socket.SOMAXCONN)
        sock.setblocking(False)
    except BaseException:
        sock.close()
        raise
    return sock


class LoopbackServer:
    def __init__(self, app: FastAPI) -> None:
        self.socket = bind_loopback_socket()
        try:
            self.server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    access_log=False,
                    log_level="warning",
                )
            )
        except BaseException:
            self.socket.close()
            raise
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    @property
    def port(self) -> int:
        return int(self.socket.getsockname()[1])

    async def start(self) -> None:
        if self._stopped:
            raise RuntimeError("loopback server is already stopped")
        if self._task is None:
            self._task = asyncio.create_task(
                self.server.serve(sockets=[self.socket])
            )
        elif self.server.started and not self._task.done():
            return

        try:
            async with asyncio.timeout(STARTUP_TIMEOUT_SECONDS):
                while not self.server.started:
                    if self._task.done():
                        await self._task
                        raise RuntimeError("loopback server stopped before startup")
                    await asyncio.sleep(_STARTUP_POLL_SECONDS)
        except TimeoutError as exc:
            await self._abort_startup()
            raise TimeoutError("loopback server did not start before timeout") from exc
        except BaseException:
            await self._abort_startup()
            raise

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.server.should_exit = True
        try:
            if self._task is not None:
                await self._task
        finally:
            self.socket.close()

    async def _abort_startup(self) -> None:
        self._stopped = True
        self.server.should_exit = True
        try:
            if self._task is None:
                return
            if self._task.done():
                with suppress(BaseException):
                    self._task.exception()
                return

            current_task = asyncio.current_task()
            cancellation_count = (
                current_task.cancelling() if current_task is not None else 0
            )
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                externally_cancelled = (
                    current_task is not None
                    and current_task.cancelling() > cancellation_count
                )
                if externally_cancelled or not self._task.cancelled():
                    raise
            except BaseException:  # noqa: BLE001 - preserve the startup failure
                return
        finally:
            self.socket.close()
