from __future__ import annotations

import json
from typing import Protocol

from app.contracts import Invocation, SkillError, SkillResult, RuntimeCallError


VALID_CHECKPOINTS = {
    "clarifying",
    "examining",
    "error_cause",
    "prevention_rule",
    "transfer_checkpoint",
    "finished",
    "boundary",
}


class Runtime(Protocol):
    def invoke(self, invocation: Invocation) -> object: ...


def _failure(code: str, *, retryable: bool = False) -> SkillResult:
    return SkillResult(
        signal=None,
        student_visible_text=None,
        teaching_progress=None,
        error=SkillError(code=code, retryable=retryable),
    )


def invoke_skill(invocation: Invocation, *, runtime: Runtime) -> SkillResult:
    """Map one runtime result without changing the Skill's visible wording."""
    try:
        raw_result = runtime.invoke(invocation)
    except RuntimeCallError as error:
        return _failure(error.code, retryable=error.retryable)
    except TimeoutError:
        return _failure("runtime_timeout", retryable=True)
    except Exception:
        return _failure("runtime_failure", retryable=True)

    if isinstance(raw_result, str):
        try:
            raw_result = json.loads(raw_result)
        except json.JSONDecodeError:
            return _failure("invalid_runtime_output")
    if not isinstance(raw_result, dict):
        return _failure("invalid_runtime_output")

    text = raw_result.get("student_visible_text")
    status = raw_result.get("teaching_status")
    progress = raw_result.get("teaching_progress")
    if not isinstance(text, str) or not isinstance(progress, dict):
        return _failure("invalid_runtime_output")
    checkpoint = progress.get("checkpoint")
    if checkpoint not in VALID_CHECKPOINTS or set(progress) != {"checkpoint"}:
        return _failure("invalid_runtime_output")

    if status == "continue":
        if not text:
            return _failure("empty_student_visible_text")
        return SkillResult(
            signal="continue",
            student_visible_text=text,
            teaching_progress={"checkpoint": checkpoint},
            error=None,
        )
    if status == "natural_end":
        return SkillResult(
            signal="ready_for_submission",
            student_visible_text=text,
            teaching_progress={"checkpoint": checkpoint},
            error=None,
        )
    if status == "boundary_stop":
        return SkillResult(
            signal=None,
            student_visible_text=text or None,
            teaching_progress={"checkpoint": checkpoint},
            error=SkillError(code="boundary_stop", retryable=False),
        )
    return _failure("invalid_teaching_status")
