from __future__ import annotations

import pytest

from app.agent.router import StateTransitionError, route_signal
from app.contracts import SkillError, SkillResult


def _result(signal: str | None, error: SkillError | None = None) -> SkillResult:
    return SkillResult(
        signal=signal,
        student_visible_text="请检查这一项。" if signal else None,
        teaching_progress={"checkpoint": "examining"} if signal else None,
        error=error,
    )


def test_continue_keeps_a_tutoring_session_in_tutoring() -> None:
    assert route_signal("tutoring", _result("continue")) == "tutoring"


def test_natural_end_is_the_only_skill_result_that_enters_submission_ready() -> None:
    assert route_signal("tutoring", _result("ready_for_submission")) == "ready_for_submission"


def test_runtime_failure_preserves_the_stable_tutoring_state() -> None:
    result = _result(None, SkillError(code="runtime_timeout", retryable=True))
    assert route_signal("tutoring", result) == "tutoring"


def test_signal_cannot_skip_help_input_or_advance_a_completed_session() -> None:
    with pytest.raises(StateTransitionError):
        route_signal("help_input", _result("continue"))
    with pytest.raises(StateTransitionError):
        route_signal("completed", _result("ready_for_submission"))
