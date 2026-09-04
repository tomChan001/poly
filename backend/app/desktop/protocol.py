from __future__ import annotations

import json
from dataclasses import dataclass, field
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
    launch_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ShutdownCommand:
    reason: str = "desktop requested shutdown"


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    state: RuntimeState
    fields: dict[str, object]

    def __post_init__(self) -> None:
        self._validate_fields()

    def to_json(self) -> str:
        self._validate_fields()
        return json.dumps(
            {"version": PROTOCOL_VERSION, "state": self.state, **self.fields},
            separators=(",", ":"),
        )

    def _validate_fields(self) -> None:
        if self.fields.keys() & {"version", "state"}:
            raise ValueError("runtime event contains reserved fields")


def parse_command(line: str) -> StartCommand | ShutdownCommand:
    decoded: Any = json.loads(line)
    if not isinstance(decoded, dict):
        raise ValueError("command payload must be an object")  # noqa: TRY004
    payload: dict[str, Any] = decoded
    version = payload.get("version")
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise ValueError("unsupported protocol version")
    command = payload.get("command")
    if command == "shutdown":
        allowed = {"version", "command", "reason"}
        if set(payload) - allowed:
            raise ValueError("unknown shutdown command field")
        reason = _require_string(
            payload.get("reason", "desktop requested shutdown"),
            "reason",
        )
        return ShutdownCommand(reason)
    if command != "start":
        raise ValueError("unsupported command")
    allowed = {"version", "command", "data_dir", "runtime_dir", "launch_token"}
    if set(payload) != allowed:
        raise ValueError("invalid start command fields")
    data_dir = _desktop_path(_require_string(payload["data_dir"], "data_dir"))
    runtime_dir = _desktop_path(_require_string(payload["runtime_dir"], "runtime_dir"))
    if not data_dir.is_absolute() or not runtime_dir.is_absolute():
        raise ValueError("desktop paths must be absolute")
    token = _require_string(payload["launch_token"], "launch_token")
    if len(token) < 43:
        raise ValueError("launch token is too short")
    return StartCommand(data_dir, runtime_dir, token)


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")  # noqa: TRY004
    return value


def _desktop_path(value: str) -> PurePosixPath:
    return PurePosixPath(value)
