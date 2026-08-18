from backend.app.domain.enums import ExecutionState

# This graph is intentionally closed: adding a state without defining its exits
# must fail loudly instead of creating an accidental execution path.
ALLOWED_TRANSITIONS: dict[ExecutionState, set[ExecutionState]] = {
    ExecutionState.DISCOVERED: {
        ExecutionState.ELIGIBLE,
        ExecutionState.CANCELLED,
    },
    ExecutionState.ELIGIBLE: {
        ExecutionState.PRECHECKED,
        ExecutionState.CANCELLED,
    },
    ExecutionState.PRECHECKED: {
        ExecutionState.SUBMITTED,
        ExecutionState.CANCELLED,
    },
    ExecutionState.SUBMITTED: {
        ExecutionState.PAIRED,
        ExecutionState.PARTIALLY_HEDGED,
        ExecutionState.EXCEPTION,
    },
    ExecutionState.PARTIALLY_HEDGED: {
        ExecutionState.PAIRED,
        ExecutionState.EXCEPTION,
    },
    ExecutionState.PAIRED: set(),
    ExecutionState.EXCEPTION: set(),
    ExecutionState.CANCELLED: set(),
}


def validate_transition(current: ExecutionState, target: ExecutionState) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"invalid transition: {current} -> {target}")

