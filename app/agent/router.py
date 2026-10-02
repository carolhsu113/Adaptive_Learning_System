from __future__ import annotations

from app.contracts import SkillResult


class StateTransitionError(ValueError):
    """A caller tried to apply a valid Skill signal in an invalid state."""


def route_signal(state: str, result: SkillResult) -> str:
    """Route only an already-validated SK-01 completion signal."""
    if result.signal is None:
        return state
    if state != "tutoring":
        raise StateTransitionError(f"Skill signal is not valid in state {state}")
    if result.signal == "continue":
        return "tutoring"
    if result.signal == "ready_for_submission":
        return "ready_for_submission"
    raise StateTransitionError("unknown Skill signal")
