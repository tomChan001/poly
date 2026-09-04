import json

import pytest

from backend.app.desktop.protocol import (
    PROTOCOL_VERSION,
    RuntimeEvent,
    RuntimeState,
    StartCommand,
    parse_command,
)


def test_start_command_accepts_only_current_protocol_and_absolute_paths() -> None:
    command = parse_command(json.dumps({
        "version": PROTOCOL_VERSION,
        "command": "start",
        "data_dir": "/Users/operator/Library/Application Support/Poly",
        "runtime_dir": "/private/tmp/poly-123",
        "launch_token": "a" * 43,
    }))

    assert isinstance(command, StartCommand)
    assert command.runtime_dir.is_absolute()


def test_protocol_rejects_relative_paths_and_unknown_fields() -> None:
    with pytest.raises(ValueError, match="absolute"):
        parse_command(json.dumps({
            "version": PROTOCOL_VERSION,
            "command": "start",
            "data_dir": "relative",
            "runtime_dir": "/private/tmp/poly-123",
            "launch_token": "a" * 43,
        }))


def test_event_serialization_never_contains_launch_token() -> None:
    event = RuntimeEvent(RuntimeState.READY, {"port": 49152})

    encoded = event.to_json()

    assert json.loads(encoded) == {
        "version": PROTOCOL_VERSION,
        "state": "ready",
        "port": 49152,
    }
    assert "launch_token" not in encoded
