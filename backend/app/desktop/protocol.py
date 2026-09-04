from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePath, PurePosixPath
from typing import Any

PROTOCOL_VERSION = 1


class RuntimeState(StrEnum):
    INITIALIZING = "initializing"
    PREPARING_DATABASE = "preparing_database"
    MIGRATING = "migrating"
    STARTING_SERVICES = "starting_services"
    READY = "ready"
    SHUTTING_DOWN = "shutting_down"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StartCommand:
    data_dir: PurePath
    runtime_dir: PurePath
    launch_token: str


@dataclass(frozen=True, slots=True)
class ShutdownCommand:
    reason: str = "desktop requested shutdown"


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    state: RuntimeState
    fields: dict[str, object]

    def to_json(self) -> str:
        return json.dumps(
            {"version": PROTOCOL_VERSION, "state": self.state, **self.fields},
            separators=(",", ":"),
        )


def parse_command(line: str) -> StartCommand | ShutdownCommand:
    payload: dict[str, Any] = json.loads(line)
    if payload.get("version") != PROTOCOL_VERSION:
        raise ValueError("unsupported protocol version")
    command = payload.get("command")
    if command == "shutdown":
        allowed = {"version", "command", "reason"}
        if set(payload) - allowed:
            raise ValueError("unknown shutdown command field")
        return ShutdownCommand(str(payload.get("reason", "desktop requested shutdown")))
    if command != "start":
        raise ValueError("unsupported command")
    allowed = {"version", "command", "data_dir", "runtime_dir", "launch_token"}
    if set(payload) != allowed:
        raise ValueError("invalid start command fields")
    data_dir = _desktop_path(payload["data_dir"])
    runtime_dir = _desktop_path(payload["runtime_dir"])
    if not data_dir.is_absolute() or not runtime_dir.is_absolute():
        raise ValueError("desktop paths must be absolute")
    token = str(payload["launch_token"])
    if len(token) < 43:
        raise ValueError("launch token is too short")
    return StartCommand(data_dir, runtime_dir, token)


def _desktop_path(value: object) -> PurePosixPath:
    return PurePosixPath(str(value))
