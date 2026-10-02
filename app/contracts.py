from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuntimeCallError(RuntimeError):
    """Safe provider failure category; never carries a key or response body."""

    def __init__(self, code: str, *, retryable: bool = True):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class TaskInput(StrictModel):
    concern: str | None = None
    problem: str | None = None
    original_answer: str | None = None
    attempts: str | None = None
    other_known_information: str | None = None


class AuthorizedMaterialSource(StrictModel):
    kind: Literal["authorized_system", "teacher", "llm"]
    source_id: str
    model: str | None = None
    model_version: str | None = None
    generated_at: str | None = None
    teacher_confirmation: Literal["confirmed", "unconfirmed", "not_applicable"]

    @model_validator(mode="after")
    def traceable_source(self) -> AuthorizedMaterialSource:
        if not self.source_id.strip():
            raise ValueError("authorized source_id cannot be blank")
        if self.kind == "llm" and not all((self.model, self.model_version, self.generated_at)):
            raise ValueError("LLM material source requires model, model_version, and generated_at")
        return self


class AuthorizedMaterial(StrictModel):
    value: str | None = None
    source: AuthorizedMaterialSource | None = None


class AuthorizedMaterials(StrictModel):
    learning_objective: AuthorizedMaterial = AuthorizedMaterial()
    reference_answer: AuthorizedMaterial = AuthorizedMaterial()


class SkillIdentity(StrictModel):
    id: Literal["SK-01"]
    version: Literal["2.0"]
    package_sha256: str
    envelope_version: str = "1.0"


class SkillPackage(StrictModel):
    id: Literal["SK-01"]
    version: Literal["2.0"]
    package_sha256: str
    root_path: str
    files: dict[str, str]


class ConversationEvent(StrictModel):
    event_id: str
    role: Literal["student", "assistant"]
    text: str


class Invocation(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    session_id: str
    request_id: str
    skill: SkillIdentity
    task_input: TaskInput
    conversation: list[ConversationEvent]
    student_input: str
    teaching_progress: dict[str, str] | None = None
    authorized_materials: AuthorizedMaterials = AuthorizedMaterials()


class SkillError(StrictModel):
    code: str
    retryable: bool


class SkillResult(StrictModel):
    signal: Literal["continue", "ready_for_submission"] | None
    student_visible_text: str | None
    teaching_progress: dict[str, str] | None
    error: SkillError | None
