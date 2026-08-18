import pytest

from backend.app.domain.enums import ExecutionState
from backend.app.domain.models import validate_transition


def test_partially_hedged_cannot_return_to_prechecked() -> None:
    with pytest.raises(ValueError, match="invalid transition"):
        validate_transition(
            ExecutionState.PARTIALLY_HEDGED,
            ExecutionState.PRECHECKED,
        )


def test_prechecked_can_be_submitted() -> None:
    validate_transition(ExecutionState.PRECHECKED, ExecutionState.SUBMITTED)


def test_terminal_state_cannot_transition() -> None:
    with pytest.raises(ValueError, match="invalid transition"):
        validate_transition(ExecutionState.PAIRED, ExecutionState.EXCEPTION)

