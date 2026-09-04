import json
from pathlib import PurePosixPath

import pytest

from backend.app.desktop.protocol import (
    PROTOCOL_VERSION,
    RuntimeEvent,
    RuntimeState,
    StartCommand,
    parse_command,
)


@pytest.mark.parametrize("payload", ["[]", "null", "1"])
def test_protocol_rejects_non_object_payloads(payload: str) -> None:
    with pytest.raises(ValueError, match="object"):
        parse_command(payload)


def test_protocol_rejects_boolean_version() -> None:
    with pytest.raises(ValueError, match="version"):
        parse_command(json.dumps({"version": True, "command": "shutdown"}))


@pytest.mark.parametrize("field", ["data_dir", "runtime_dir", "launch_token"])
@pytest.mark.parametrize(
    "invalid_value",
    [None, True, 1, [], {}],
    ids=["null", "boolean", "number", "list", "object"],
)
def test_start_command_rejects_non_string_fields(
    field: str,
    invalid_value: object,
) -> None:
    payload: dict[str, object] = {
        "version": PROTOCOL_VERSION,
        "command": "start",
        "data_dir": "/Users/operator/Library/Application Support/Poly",
        "runtime_dir": "/private/tmp/poly-123",
        "launch_token": "a" * 43,
    }
    payload[field] = invalid_value

    with pytest.raises(ValueError, match="string"):
        parse_command(json.dumps(payload))


@pytest.mark.parametrize(
    "invalid_reason",
    [None, True, 1, [], {}],
    ids=["null", "boolean", "number", "list", "object"],
)
def test_shutdown_command_rejects_non_string_reason(invalid_reason: object) -> None:
    with pytest.raises(ValueError, match="string"):
        parse_command(
            json.dumps(
                {
                    "version": PROTOCOL_VERSION,
                    "command": "shutdown",
                    "reason": invalid_reason,
                }
            )
        )


def test_start_command_accepts_only_current_protocol_and_absolute_paths() -> None:
    command = parse_command(
        json.dumps(
            {
                "version": PROTOCOL_VERSION,
                "command": "start",
                "data_dir": "/Users/operator/Library/Application Support/Poly",
                "runtime_dir": "/private/tmp/poly-123",
                "launch_token": "a" * 43,
            }
        )
    )

    assert isinstance(command, StartCommand)
    assert command.runtime_dir.is_absolute()


def test_protocol_rejects_relative_paths_and_unknown_fields() -> None:
    with pytest.raises(ValueError, match="absolute"):
        parse_command(
            json.dumps(
                {
                    "version": PROTOCOL_VERSION,
                    "command": "start",
                    "data_dir": "relative",
                    "runtime_dir": "/private/tmp/poly-123",
                    "launch_token": "a" * 43,
                }
            )
        )


def test_protocol_rejects_windows_absolute_paths() -> None:
    with pytest.raises(ValueError, match="absolute"):
        parse_command(
            json.dumps(
                {
                    "version": PROTOCOL_VERSION,
                    "command": "start",
                    "data_dir": r"C:\Users\alice\Library\Application Support\Poly",
                    "runtime_dir": "/private/tmp/poly-123",
                    "launch_token": "a" * 43,
                }
            )
        )


@pytest.mark.parametrize("reserved_field", ["version", "state"])
def test_runtime_event_rejects_reserved_fields(reserved_field: str) -> None:
    with pytest.raises(ValueError, match="reserved"):
        RuntimeEvent(RuntimeState.READY, {reserved_field: "overridden"})


@pytest.mark.parametrize("reserved_field", ["version", "state"])
def test_runtime_event_rejects_reserved_fields_added_after_creation(
    reserved_field: str,
) -> None:
    event = RuntimeEvent(RuntimeState.READY, {})
    event.fields[reserved_field] = "overridden"

    with pytest.raises(ValueError, match="reserved"):
        event.to_json()


def test_event_serialization_never_contains_launch_token() -> None:
    event = RuntimeEvent(RuntimeState.READY, {"port": 49152})

    encoded = event.to_json()

    assert json.loads(encoded) == {
        "version": PROTOCOL_VERSION,
        "state": "ready",
        "port": 49152,
    }
    assert "launch_token" not in encoded


def test_start_command_repr_does_not_contain_launch_token() -> None:
    launch_token = "sensitive-launch-token-xxxxxxxxxxxxxxxxxxxxx"
    command = StartCommand(
        PurePosixPath("/data"),
        PurePosixPath("/run"),
        launch_token,
    )

    assert launch_token not in repr(command)
