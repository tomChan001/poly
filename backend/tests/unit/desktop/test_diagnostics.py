import io
import json

import pytest

from backend.app.desktop.diagnostics import (
    encode_runtime_failure_diagnostic,
    write_runtime_failure_diagnostic,
)
from backend.app.desktop.protocol import RuntimeState

HOSTILE_FRAGMENTS = (
    "postgresql://operator:database-password@localhost/poly",
    "launch-token-secret-value",
    "SELECT secret FROM credentials",
    "parameter-secret-value",
    "/Users/operator/Library/Application Support/Poly",
)


class WrapperFailure(Exception):
    orig: BaseException
    sqlstate: object
    errno: object


class DriverFailure(Exception):
    sqlstate: object
    errno: object


class GraphFailure(Exception):
    orig: BaseException


class GraphRootFailure(GraphFailure):
    pass


class GraphOrigFailure(GraphFailure):
    pass


class GraphCauseFailure(GraphFailure):
    pass


class GraphContextFailure(GraphFailure):
    pass


class GraphFifthFailure(GraphFailure):
    pass


class GraphSixthFailure(GraphFailure):
    pass


class GraphSeventhFailure(GraphFailure):
    pass


class GraphEighthFailure(GraphFailure):
    pass


class DiagnosticInterruption(BaseException):
    pass


class HostileAttributeFailure(Exception):
    def __getattribute__(self, name: str) -> object:
        if name in {"orig", "__cause__", "__context__", "sqlstate", "errno"}:
            raise DiagnosticInterruption("SELECT secret from /Users/operator")
        return super().__getattribute__(name)


class InvalidTypeNameFailure(Exception):
    pass


class RecordingStream(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.writes: list[str] = []
        self.flush_count = 0

    def write(self, value: str) -> int:
        self.writes.append(value)
        return super().write(value)

    def flush(self) -> None:
        self.flush_count += 1
        super().flush()


class RaisingStream(io.StringIO):
    def write(self, value: str) -> int:
        raise DiagnosticInterruption(value)

    def flush(self) -> None:
        raise AssertionError("flush must not run after a failed write")


class RaisingFlushStream(RecordingStream):
    def flush(self) -> None:
        self.flush_count += 1
        raise DiagnosticInterruption("flush failed")


def _qualified_type_name(error: BaseException) -> str:
    error_type = type(error)
    return f"{error_type.__module__}.{error_type.__qualname__}"


def _hostile_message() -> str:
    return " | ".join(HOSTILE_FRAGMENTS)


def test_encoder_emits_only_allowlisted_failure_metadata() -> None:
    driver = DriverFailure(_hostile_message())
    driver.sqlstate = "08006"
    driver.errno = 13
    error = WrapperFailure(_hostile_message())
    error.sqlstate = "0800-secret"
    error.errno = True
    error.orig = driver

    encoded = encode_runtime_failure_diagnostic(error, RuntimeState.MIGRATING)
    expected = {
        "diagnostic_schema": 1,
        "event": "runtime_failure",
        "phase": "migrating",
        "exception_chain": [
            f"{WrapperFailure.__module__}.WrapperFailure",
            f"{DriverFailure.__module__}.DriverFailure",
        ],
        "sqlstate": "08006",
        "errno": 13,
    }

    assert json.loads(encoded) == expected
    assert encoded == json.dumps(expected, ensure_ascii=True, separators=(",", ":"))
    for fragment in HOSTILE_FRAGMENTS:
        assert fragment not in encoded


def test_encoder_breadth_first_traversal_is_cycle_safe_and_bounded() -> None:
    nodes: list[GraphFailure] = [
        GraphRootFailure("root secret"),
        GraphOrigFailure("orig secret"),
        GraphCauseFailure("cause secret"),
        GraphContextFailure("context secret"),
        GraphFifthFailure("fifth secret"),
        GraphSixthFailure("sixth secret"),
        GraphSeventhFailure("seventh secret"),
        GraphEighthFailure("eighth secret"),
    ]
    nodes[0].orig = nodes[1]
    nodes[1].orig = nodes[2]
    nodes[1].__cause__ = nodes[0]
    nodes[1].__context__ = nodes[3]
    nodes[2].orig = nodes[4]
    nodes[3].__cause__ = nodes[5]
    nodes[4].__context__ = nodes[6]
    nodes[5].orig = nodes[7]

    decoded = json.loads(
        encode_runtime_failure_diagnostic(nodes[0], RuntimeState.MIGRATING)
    )

    assert decoded["exception_chain"] == [
        _qualified_type_name(node) for node in nodes[:4]
    ]
    assert len(decoded["exception_chain"]) <= 4


@pytest.mark.parametrize(
    "sqlstate",
    [None, 8006, "0800", "08006X", "08a06", "0800!", "É08006"],
    ids=["none", "integer", "short", "long", "lowercase", "punctuation", "unicode"],
)
def test_encoder_omits_invalid_sqlstate(sqlstate: object) -> None:
    error = DriverFailure(_hostile_message())
    error.sqlstate = sqlstate

    encoded = encode_runtime_failure_diagnostic(error, RuntimeState.MIGRATING)

    assert "sqlstate" not in json.loads(encoded)
    for fragment in HOSTILE_FRAGMENTS:
        assert fragment not in encoded


@pytest.mark.parametrize(
    "errno",
    [None, True, 13.0, "13", -(2**31) - 1, 2**31],
    ids=["none", "boolean", "float", "string", "below-minimum", "above-maximum"],
)
def test_encoder_omits_invalid_errno(errno: object) -> None:
    error = DriverFailure(_hostile_message())
    error.errno = errno

    encoded = encode_runtime_failure_diagnostic(error, RuntimeState.MIGRATING)

    assert "errno" not in json.loads(encoded)
    for fragment in HOSTILE_FRAGMENTS:
        assert fragment not in encoded


def test_encoder_survives_attributes_that_raise_base_exceptions() -> None:
    error = HostileAttributeFailure(_hostile_message())

    encoded = encode_runtime_failure_diagnostic(error, RuntimeState.MIGRATING)

    assert json.loads(encoded) == {
        "diagnostic_schema": 1,
        "event": "runtime_failure",
        "phase": "migrating",
        "exception_chain": [_qualified_type_name(error)],
    }
    for fragment in HOSTILE_FRAGMENTS:
        assert fragment not in encoded


def test_encoder_uses_unknown_for_an_invalid_type_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        InvalidTypeNameFailure,
        "__module__",
        "postgresql://operator:database-password@localhost/poly",
    )

    encoded = encode_runtime_failure_diagnostic(
        InvalidTypeNameFailure(_hostile_message()), RuntimeState.MIGRATING
    )

    assert json.loads(encoded)["exception_chain"] == ["unknown"]
    for fragment in HOSTILE_FRAGMENTS:
        assert fragment not in encoded


def test_writer_outputs_one_line_and_flushes() -> None:
    error = DriverFailure(_hostile_message())
    error.sqlstate = "08006"
    error.errno = 13
    stream = RecordingStream()

    write_runtime_failure_diagnostic(error, RuntimeState.MIGRATING, stream=stream)

    encoded = encode_runtime_failure_diagnostic(error, RuntimeState.MIGRATING)
    assert stream.writes == [encoded + "\n"]
    assert stream.flush_count == 1
    assert stream.writes[0].count("\n") == 1
    for fragment in HOSTILE_FRAGMENTS:
        assert fragment not in stream.writes[0]


def test_writer_swallows_every_exception_from_stream_write() -> None:
    write_runtime_failure_diagnostic(
        DriverFailure(_hostile_message()),
        RuntimeState.MIGRATING,
        stream=RaisingStream(),
    )


def test_writer_swallows_every_exception_from_stream_flush() -> None:
    error = DriverFailure(_hostile_message())
    stream = RaisingFlushStream()

    write_runtime_failure_diagnostic(error, RuntimeState.MIGRATING, stream=stream)

    encoded = encode_runtime_failure_diagnostic(error, RuntimeState.MIGRATING)
    assert stream.writes == [encoded + "\n"]
    assert stream.flush_count == 1
    for fragment in HOSTILE_FRAGMENTS:
        assert fragment not in stream.writes[0]
