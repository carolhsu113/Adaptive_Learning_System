"""Student-visible projection and reconciliation of saved session facts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.contracts import TaskInput
from app.core.records import Session, SessionStore


class ViewModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VisibleMessage(ViewModel):
    event_id: str
    role: Literal["student", "assistant"]
    text: str


class VisibleError(ViewModel):
    code: str
    message: str
    retryable: bool


class StudentView(ViewModel):
    session_id: str | None
    revision: int
    state: str | None
    task_input: TaskInput
    conversation: list[VisibleMessage]
    final_answer_draft: str
    submission_id: str | None
    operation_status: str
    error: VisibleError | None
    saved_at: str | None


ERROR_MESSAGES = {
    "runtime_timeout": "本轮等待超时，学生回应已保留。",
    "runtime_failure": "本轮未完成，学生回应已保留。",
    "invalid_runtime_output": "本轮未完成，请重试。",
    "boundary_stop": "本次引导不能继续，请开始新问题。",
    "request_interrupted": "本轮中断，学生回应已保留。",
}


def to_student_view(session: Session, error: VisibleError | None = None) -> StudentView:
    conversation = [
        VisibleMessage(
            event_id=str(event["event_id"]),
            role="student" if event["type"] == "student_input" else "assistant",
            text=str(event["payload"]["text"]),
        )
        for event in session.events
        if event["type"] in {"student_input", "student_visible_output"}
    ]
    latest = list(session.operations.values())[-1] if session.operations else None
    operation_status = str(latest["status"]) if latest else "idle"
    if error is None and latest and latest["status"] in {"failed", "interrupted"}:
        code = str(latest.get("error_code") or "request_interrupted")
        error = VisibleError(
            code=code,
            message=ERROR_MESSAGES.get(code, "本轮未完成，学生回应已保留。"),
            retryable=bool(latest.get("retryable")),
        )
    return StudentView(
        session_id=session.session_id,
        revision=session.revision,
        state=session.state,
        task_input=session.task_input,
        conversation=conversation,
        final_answer_draft=session.final_answer_draft,
        submission_id=session.submission.get("id"),
        operation_status=operation_status,
        error=error,
        saved_at=session.submission.get("submitted_at") if session.state == "completed" else None,
    )


def recover_session(store: SessionStore, session_id: str, active_request_ids: frozenset[str] = frozenset()) -> StudentView:
    session = store.reconcile_submission(session_id)
    changed = False
    for request_id, operation in session.operations.items():
        if operation["status"] == "pending" and request_id not in active_request_ids:
            operation["status"] = "interrupted"
            operation["error_code"] = "request_interrupted"
            operation["retryable"] = True
            changed = True
    if changed:
        session = store.save_session(session, session.revision)
    return to_student_view(session)


def empty_error_view(code: str, message: str) -> StudentView:
    return StudentView(
        session_id=None,
        revision=0,
        state=None,
        task_input=TaskInput(),
        conversation=[],
        final_answer_draft="",
        submission_id=None,
        operation_status="idle",
        error=VisibleError(code=code, message=message, retryable=False),
        saved_at=None,
    )
