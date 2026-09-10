from __future__ import annotations

import json
import re
import sys
from collections import deque
from typing import TextIO

from backend.app.desktop.protocol import RuntimeState

DIAGNOSTIC_SCHEMA = 1
MAX_EXCEPTION_CHAIN = 4
_TYPE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,15}\Z")
_SQLSTATE = re.compile(r"[0-9A-Z]{5}\Z")
_MIN_ERRNO = -(2**31)
_MAX_ERRNO = 2**31 - 1


def _attribute(value: object, name: str) -> object | None:
    try:
        return getattr(value, name, None)
    except BaseException:  # noqa: BLE001 - diagnostics cannot alter failure handling
        return None


def _exception_graph(error: BaseException) -> list[BaseException]:
    pending: deque[BaseException] = deque([error])
    found: list[BaseException] = []
    seen: set[int] = set()
    while pending and len(found) < MAX_EXCEPTION_CHAIN:
        current = pending.popleft()
        if id(current) in seen:
            continue
        seen.add(id(current))
        found.append(current)
        for name in ("orig", "__cause__", "__context__"):
            related = _attribute(current, name)
            if isinstance(related, BaseException) and id(related) not in seen:
                pending.append(related)
    return found


def _type_name(error: BaseException) -> str:
    error_type = type(error)
    module = _attribute(error_type, "__module__")
    qualified = _attribute(error_type, "__qualname__")
    if type(module) is not str or type(qualified) is not str:
        return "unknown"
    candidate = module + "." + qualified
    if len(candidate) > 192 or _TYPE_NAME.fullmatch(candidate) is None:
        return "unknown"
    return candidate


def encode_runtime_failure_diagnostic(
    error: BaseException,
    phase: RuntimeState,
) -> str:
    chain = _exception_graph(error)
    payload: dict[str, object] = {
        "diagnostic_schema": DIAGNOSTIC_SCHEMA,
        "event": "runtime_failure",
        "phase": phase.value,
        "exception_chain": [_type_name(item) for item in chain],
    }
    for item in chain:
        sqlstate = _attribute(item, "sqlstate")
        if type(sqlstate) is str and _SQLSTATE.fullmatch(sqlstate) is not None:
            payload["sqlstate"] = sqlstate
            break
    for item in chain:
        errno = _attribute(item, "errno")
        if type(errno) is int and _MIN_ERRNO <= errno <= _MAX_ERRNO:
            payload["errno"] = errno
            break
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def write_runtime_failure_diagnostic(
    error: BaseException,
    phase: RuntimeState,
    stream: TextIO | None = None,
) -> None:
    try:
        target = stream if stream is not None else sys.stderr
        target.write(encode_runtime_failure_diagnostic(error, phase) + "\n")
        target.flush()
    except BaseException:  # noqa: BLE001 - diagnostics cannot replace real failure
        return
