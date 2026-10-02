"""Strict on-disk contracts for the September student session and snapshot."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.contracts import AuthorizedMaterials, SkillIdentity, TaskInput


class DiskModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RuntimeRecord(DiskModel):
    provider: Literal["openai_responses"]
    requested_model: str | None
    returned_model: str | None
    sdk_version: str | None


State = Literal[
    "help_input", "tutoring", "ready_for_submission", "final_answer",
    "submitting", "submit_failed", "completed",
]


class BaseEvent(DiskModel):
    event_id: str
    seq: int = Field(ge=1)
    at: str
    request_id: str

    @field_validator("event_id", "request_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return canonical_uuid4(value)

    @field_validator("at")
    @classmethod
    def valid_time(cls, value: str) -> str:
        return utc_time(value)


class TextPayload(DiskModel):
    text: str


class StatePayload(DiskModel):
    from_state: State = Field(alias="from")
    to: State


class AttemptPayload(DiskModel):
    submission_id: str
    answer_hash: str
    result: Literal["pending", "failed", "saved"]

    @field_validator("submission_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return canonical_uuid4(value)

    @field_validator("answer_hash")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        return sha256_hex(value)


class SuccessPayload(DiskModel):
    submission_id: str
    record_id: str

    @field_validator("submission_id", "record_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return canonical_uuid4(value)


class FailurePayload(DiskModel):
    code: str = Field(min_length=1)


class StudentInputEvent(BaseEvent):
    type: Literal["student_input"]
    payload: TextPayload


class StudentVisibleOutputEvent(BaseEvent):
    type: Literal["student_visible_output"]
    payload: TextPayload


class StateChangedEvent(BaseEvent):
    type: Literal["state_changed"]
    payload: StatePayload


class SubmissionAttemptEvent(BaseEvent):
    type: Literal["submission_attempt"]
    payload: AttemptPayload


class SubmissionSucceededEvent(BaseEvent):
    type: Literal["submission_succeeded"]
    payload: SuccessPayload


class OperationFailedEvent(BaseEvent):
    type: Literal["operation_failed"]
    payload: FailurePayload


EventRecord = Annotated[
    StudentInputEvent | StudentVisibleOutputEvent | StateChangedEvent
    | SubmissionAttemptEvent | SubmissionSucceededEvent | OperationFailedEvent,
    Field(discriminator="type"),
]


class MissingField(DiskModel):
    field: str
    reason: Literal["not_provided_by_student", "not_provided_by_authorized_source"]


class OperationRecord(DiskModel):
    kind: Literal["draft", "turn", "final_answer"] = "turn"
    payload_hash: str
    status: Literal["pending", "succeeded", "failed", "interrupted"]
    student_event_id: str | None
    assistant_event_id: str | None
    error_code: str | None
    retryable: bool
    response_id: str | None

    @field_validator("student_event_id", "assistant_event_id")
    @classmethod
    def valid_event_id(cls, value: str | None) -> str | None:
        return canonical_uuid4(value) if value is not None else None

    @field_validator("payload_hash")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        return sha256_hex(value)


class SubmissionAttempt(DiskModel):
    submission_id: str
    answer_hash: str
    result: Literal["pending", "failed", "saved"]

    @field_validator("submission_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return canonical_uuid4(value)

    @field_validator("answer_hash")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        return sha256_hex(value)


class SessionSubmission(DiskModel):
    id: str | None
    attempts: list[SubmissionAttempt]
    result: Literal["saved", "failed"] | None
    submitted_at: str | None
    snapshot_filename: str | None

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str | None) -> str | None:
        return canonical_uuid4(value) if value is not None else None

    @field_validator("submitted_at")
    @classmethod
    def valid_time(cls, value: str | None) -> str | None:
        return utc_time(value) if value is not None else None


class SnapshotSubmission(DiskModel):
    id: str
    submitted_at: str
    result: Literal["saved"]

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return canonical_uuid4(value)

    @field_validator("submitted_at")
    @classmethod
    def valid_time(cls, value: str) -> str:
        return utc_time(value)


def canonical_uuid4(value: str) -> str:
    parsed = UUID(value)
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("identifier must be a canonical UUIDv4")
    return value


def sha256_hex(value: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("hash must be lowercase SHA-256")
    return value


def utc_time(value: str) -> str:
    if not value.endswith("Z") and not value.endswith("+00:00"):
        raise ValueError("timestamp must be UTC RFC 3339")
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def ordered_events(events: list[EventRecord]) -> None:
    if [event.seq for event in events] != list(range(1, len(events) + 1)):
        raise ValueError("event sequence must be contiguous")
    if len({event.event_id for event in events}) != len(events):
        raise ValueError("event identifiers must be unique")


class SessionRecord(DiskModel):
    schema_version: Literal["1.0"]
    record_id: str
    session_id: str
    create_request_id: str
    skill: SkillIdentity
    created_at: str
    updated_at: str
    revision: int = Field(ge=1)
    state: State
    runtime: RuntimeRecord
    task_input: TaskInput
    events: list[EventRecord]
    teaching_progress: dict[str, str] | None
    final_answer_draft: str
    authorized_materials: AuthorizedMaterials
    missing_fields: list[MissingField]
    operations: dict[str, OperationRecord]
    submission: SessionSubmission

    @field_validator("record_id", "session_id", "create_request_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return canonical_uuid4(value)

    @field_validator("created_at", "updated_at")
    @classmethod
    def valid_time(cls, value: str) -> str:
        return utc_time(value)

    @model_validator(mode="after")
    def valid_sequence(self) -> SessionRecord:
        ordered_events(self.events)
        for request_id in self.operations:
            canonical_uuid4(request_id)
        return self


class SnapshotRecord(DiskModel):
    schema_version: Literal["1.0"]
    record_id: str
    session_id: str
    skill: SkillIdentity
    runtime: RuntimeRecord
    task_input: TaskInput
    events: list[EventRecord]
    authorized_materials: AuthorizedMaterials
    missing_fields: list[MissingField]
    final_answer: str
    submission: SnapshotSubmission

    @field_validator("record_id", "session_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return canonical_uuid4(value)

    @model_validator(mode="after")
    def valid_submission(self) -> SnapshotRecord:
        ordered_events(self.events)
        if not self.final_answer.strip():
            raise ValueError("snapshot final answer cannot be blank")
        successes = [event for event in self.events if isinstance(event, SubmissionSucceededEvent)]
        if len(successes) != 1:
            raise ValueError("snapshot requires exactly one success event")
        if successes[0].payload.submission_id != self.submission.id or successes[0].payload.record_id != self.record_id:
            raise ValueError("success event does not match snapshot identity")
        return self


def validate_record(path: Path, payload: dict[str, object]) -> None:
    if path.parent.name == "sessions":
        record = SessionRecord.model_validate(payload)
        if path.name != f"{record.session_id}.json":
            raise ValueError("session filename does not match session_id")
    elif path.parent.name == "snapshots":
        snapshot = SnapshotRecord.model_validate(payload)
        if path.name != f"{snapshot.session_id}--{snapshot.submission.id}.json":
            raise ValueError("snapshot filename does not match its identifiers")
