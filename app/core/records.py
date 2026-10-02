from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock, RLock
from uuid import uuid4

from app.agent.router import route_signal
from app.contracts import AuthorizedMaterials, SkillIdentity, SkillResult, TaskInput
from app.core.file_store import AtomicJsonFileStore
from app.core.record_schema import canonical_uuid4


class RevisionConflictError(ValueError):
    """The supplied write version is no longer current."""


class SubmissionError(ValueError):
    """The session is not eligible for a new snapshot."""


VALID_STATES = frozenset({
    "help_input", "tutoring", "ready_for_submission", "final_answer",
    "submitting", "submit_failed", "completed",
})
ALLOWED_TRANSITIONS = {
    "help_input": {"help_input", "tutoring"},
    "tutoring": {"tutoring", "ready_for_submission"},
    "ready_for_submission": {"ready_for_submission", "final_answer"},
    "final_answer": {"final_answer", "submitting"},
    "submitting": {"completed", "submit_failed"},
    "submit_failed": {"submit_failed", "submitting"},
    "completed": set(),
}
_SESSION_LOCK_GUARD = Lock()
_SESSION_LOCKS: dict[tuple[Path, str], RLock] = {}
_DIRECTORY_LOCK_GUARD = Lock()
_DIRECTORY_LOCKS: dict[Path, RLock] = {}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _uuid4(value: str) -> str:
    return canonical_uuid4(value)


def _event(events: list[dict[str, object]], kind: str, request_id: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "event_id": request_id if kind == "student_input" else str(uuid4()),
        "seq": len(events) + 1,
        "at": _now(),
        "type": kind,
        "request_id": request_id,
        "payload": payload,
    }


def _missing_fields(task_input: TaskInput, materials: AuthorizedMaterials) -> list[dict[str, str]]:
    missing = [
        {"field": f"task_input.{field}", "reason": "not_provided_by_student"}
        for field in ("concern", "problem", "original_answer", "attempts", "other_known_information")
        if getattr(task_input, field) is None
    ]
    return missing + [
        {"field": f"authorized_materials.{field}", "reason": "not_provided_by_authorized_source"}
        for field in ("learning_objective", "reference_answer")
        if getattr(materials, field).value is None or getattr(materials, field).source is None
    ]


@dataclass
class Session:
    record_id: str
    session_id: str
    create_request_id: str
    skill: SkillIdentity
    created_at: str
    updated_at: str
    revision: int = 0
    state: str = "help_input"
    runtime: dict[str, str | None] = field(default_factory=lambda: {
        "provider": "openai_responses", "requested_model": None,
        "returned_model": None, "sdk_version": None,
    })
    task_input: TaskInput = field(default_factory=TaskInput)
    events: list[dict[str, object]] = field(default_factory=list)
    teaching_progress: dict[str, str] | None = None
    final_answer_draft: str = ""
    authorized_materials: AuthorizedMaterials = field(default_factory=AuthorizedMaterials)
    missing_fields: list[dict[str, str]] = field(default_factory=list)
    operations: dict[str, dict[str, object]] = field(default_factory=dict)
    submission: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "record_id": self.record_id,
            "session_id": self.session_id,
            "create_request_id": self.create_request_id,
            "skill": self.skill.model_dump(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "revision": self.revision,
            "state": self.state,
            "runtime": self.runtime,
            "task_input": self.task_input.model_dump(),
            "events": self.events,
            "teaching_progress": self.teaching_progress,
            "final_answer_draft": self.final_answer_draft,
            "authorized_materials": self.authorized_materials.model_dump(),
            "missing_fields": self.missing_fields,
            "operations": self.operations,
            "submission": self.submission,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> Session:
        if payload.get("schema_version") != "1.0":
            raise ValueError("unsupported session schema_version")
        if payload.get("state") not in VALID_STATES:
            raise ValueError("invalid session state")
        return cls(
            record_id=str(payload["record_id"]),
            session_id=str(payload["session_id"]),
            create_request_id=str(payload["create_request_id"]),
            skill=SkillIdentity.model_validate(payload["skill"]),
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
            revision=int(payload["revision"]),
            state=str(payload["state"]),
            runtime=dict(payload.get("runtime", {
                "provider": "openai_responses", "requested_model": None,
                "returned_model": None, "sdk_version": None,
            })),
            task_input=TaskInput.model_validate(payload["task_input"]),
            events=list(payload.get("events", [])),
            teaching_progress=payload.get("teaching_progress"),
            final_answer_draft=str(payload.get("final_answer_draft", "")),
            authorized_materials=AuthorizedMaterials.model_validate(payload.get("authorized_materials", {})),
            missing_fields=list(payload.get("missing_fields", [])),
            operations=dict(payload.get("operations", {})),
            submission=dict(payload.get("submission", {})),
        )


@dataclass(frozen=True)
class Receipt:
    record_id: str
    submission_id: str
    saved_at: str
    revision: int


def create_session(create_request_id: str, skill: SkillIdentity) -> Session:
    _uuid4(create_request_id)
    now = _now()
    task_input = TaskInput()
    materials = AuthorizedMaterials()
    return Session(
        record_id=str(uuid4()),
        session_id=str(uuid4()),
        create_request_id=create_request_id,
        skill=skill,
        created_at=now,
        updated_at=now,
        task_input=task_input,
        authorized_materials=materials,
        missing_fields=_missing_fields(task_input, materials),
        submission={"id": None, "attempts": [], "result": None, "submitted_at": None, "snapshot_filename": None},
    )


class SessionStore:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = Path(data_dir)
        self._sessions_dir = self._data_dir / "sessions"
        self._snapshots_dir = self._data_dir / "snapshots"

    def _lock_for(self, session_id: str) -> RLock:
        _uuid4(session_id)
        key = (self._data_dir.resolve(), session_id)
        with _SESSION_LOCK_GUARD:
            return _SESSION_LOCKS.setdefault(key, RLock())

    def _directory_lock(self) -> RLock:
        key = self._data_dir.resolve()
        with _DIRECTORY_LOCK_GUARD:
            return _DIRECTORY_LOCKS.setdefault(key, RLock())

    def create_once(self, create_request_id: str, skill: SkillIdentity, model: str) -> Session:
        _uuid4(create_request_id)
        with self._directory_lock():
            for path in self._sessions_dir.glob("*.json"):
                existing = Session.from_dict(AtomicJsonFileStore.read(path))
                if existing.create_request_id == create_request_id:
                    return existing
            session = create_session(create_request_id, skill)
            session.runtime["requested_model"] = model
            return self.save_session(session, 0)

    def _session_path(self, session_id: str) -> Path:
        _uuid4(session_id)
        return self._sessions_dir / f"{session_id}.json"

    def _snapshot_path(self, session_id: str, submission_id: str) -> Path:
        _uuid4(session_id)
        _uuid4(submission_id)
        return self._snapshots_dir / f"{session_id}--{submission_id}.json"

    def get_session(self, session_id: str) -> Session:
        return Session.from_dict(AtomicJsonFileStore.read(self._session_path(session_id)))

    def save_draft(
        self,
        session_id: str,
        request_id: str,
        expected_revision: int,
        *,
        task_input: TaskInput | None = None,
        final_answer_draft: str | None = None,
    ) -> Session:
        with self._lock_for(session_id):
            _uuid4(request_id)
            session = self.get_session(session_id)
            payload = {
                "kind": "draft",
                "task_input": task_input.model_dump() if task_input is not None else None,
                "final_answer_draft": final_answer_draft,
            }
            payload_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            existing = session.operations.get(request_id)
            if existing is not None:
                if existing.get("kind") == "draft" and existing.get("payload_hash") == payload_hash and existing.get("status") == "succeeded":
                    return session
                raise SubmissionError("request identifier was reused with different draft input")
            if session.revision != expected_revision:
                raise RevisionConflictError("session revision is stale")
            if session.state == "help_input" and task_input is not None and final_answer_draft is None:
                session.task_input = task_input
            elif session.state in {"final_answer", "submit_failed"} and final_answer_draft is not None and task_input is None:
                session.final_answer_draft = final_answer_draft
            else:
                raise SubmissionError("draft is not editable in this state")
            session.operations[request_id] = {
                "kind": "draft", "payload_hash": payload_hash, "status": "succeeded",
                "student_event_id": None, "assistant_event_id": None,
                "error_code": None, "retryable": False, "response_id": None,
            }
            return self.save_session(session, expected_revision)

    def enter_final_answer(self, session_id: str, request_id: str, expected_revision: int) -> Session:
        with self._lock_for(session_id):
            _uuid4(request_id)
            session = self.get_session(session_id)
            payload_hash = hashlib.sha256(b'{"kind":"final_answer"}').hexdigest()
            existing = session.operations.get(request_id)
            if existing is not None:
                if existing.get("kind") == "final_answer" and existing.get("payload_hash") == payload_hash and existing.get("status") == "succeeded":
                    return session
                raise SubmissionError("request identifier was reused for another operation")
            if session.revision != expected_revision:
                raise RevisionConflictError("session revision is stale")
            if session.state != "ready_for_submission":
                raise SubmissionError("session is not ready for final answer")
            session.state = "final_answer"
            session.submission["id"] = str(uuid4())
            session.final_answer_draft = ""
            session.events.append(_event(session.events, "state_changed", request_id, {"from": "ready_for_submission", "to": "final_answer"}))
            session.operations[request_id] = {
                "kind": "final_answer", "payload_hash": payload_hash, "status": "succeeded",
                "student_event_id": None, "assistant_event_id": None,
                "error_code": None, "retryable": False, "response_id": None,
            }
            return self.save_session(session, expected_revision)

    def save_session(self, session: Session, expected_revision: int) -> Session:
        with self._lock_for(session.session_id):
            if session.state not in VALID_STATES:
                raise ValueError("invalid session state")
            path = self._session_path(session.session_id)
            if path.exists():
                current = self.get_session(session.session_id)
                if current.revision != expected_revision:
                    raise RevisionConflictError("session revision is stale")
                if (session.record_id, session.create_request_id, session.skill) != (
                    current.record_id, current.create_request_id, current.skill
                ):
                    raise SubmissionError("saved session identity cannot change")
                if current.runtime.get("requested_model") not in (None, session.runtime.get("requested_model")):
                    raise SubmissionError("locked runtime model cannot change")
                if session.state not in ALLOWED_TRANSITIONS[current.state]:
                    raise SubmissionError("illegal or completed session transition")
            elif expected_revision != 0:
                raise RevisionConflictError("new session requires revision 0")
            updated_at = _now()
            missing_fields = _missing_fields(session.task_input, session.authorized_materials)
            payload = session.to_dict() | {
                "revision": expected_revision + 1,
                "updated_at": updated_at,
                "missing_fields": missing_fields,
            }
            AtomicJsonFileStore.write(path, payload)
            session.revision = expected_revision + 1
            session.updated_at = updated_at
            session.missing_fields = missing_fields
            return session

    def _existing_snapshot(self, session: Session, submission_id: str, final_answer: str) -> dict[str, object] | None:
        path = self._snapshot_path(session.session_id, submission_id)
        if not path.exists():
            return None
        snapshot = AtomicJsonFileStore.read(path)
        submission = snapshot.get("submission")
        events = snapshot.get("events")
        if (
            snapshot.get("schema_version") != "1.0"
            or snapshot.get("record_id") != session.record_id
            or snapshot.get("session_id") != session.session_id
            or snapshot.get("skill") != session.skill.model_dump()
            or snapshot.get("final_answer") != final_answer
            or not isinstance(submission, dict)
            or submission.get("id") != submission_id
            or submission.get("result") != "saved"
            or not isinstance(submission.get("submitted_at"), str)
            or not isinstance(events, list)
            or sum(event.get("type") == "submission_succeeded" for event in events if isinstance(event, dict)) != 1
            or [event.get("seq") for event in events if isinstance(event, dict)] != list(range(1, len(events) + 1))
        ):
            raise SubmissionError("existing snapshot is invalid or does not match this submission")
        return snapshot

    def _receipt_from_snapshot(self, session: Session, submission_id: str, snapshot: dict[str, object]) -> Receipt:
        submission = snapshot["submission"]
        assert isinstance(submission, dict)
        if session.state != "completed":
            session.state = "completed"
            session.final_answer_draft = str(snapshot["final_answer"])
            session.events = list(snapshot["events"])
            attempts = list(session.submission.get("attempts", []))
            answer_hash = hashlib.sha256(str(snapshot["final_answer"]).encode("utf-8")).hexdigest()
            if attempts and attempts[-1].get("result") == "pending":
                attempts[-1] = {"submission_id": submission_id, "answer_hash": answer_hash, "result": "saved"}
            else:
                attempts.append({"submission_id": submission_id, "answer_hash": answer_hash, "result": "saved"})
            session.submission = {
                "id": submission_id,
                "attempts": attempts,
                "result": "saved",
                "submitted_at": submission["submitted_at"],
                "snapshot_filename": self._snapshot_path(session.session_id, submission_id).name,
            }
            self.save_session(session, session.revision)
        return Receipt(session.record_id, submission_id, str(submission["submitted_at"]), session.revision)

    def begin_turn(self, session_id: str, request_id: str, text: str | None, expected_revision: int) -> Session:
        with self._lock_for(session_id):
            _uuid4(request_id)
            session = self.get_session(session_id)
            payload_bytes = json.dumps({"kind": "turn", "text": text}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            payload_hash = hashlib.sha256(payload_bytes).hexdigest()
            existing = session.operations.get(request_id)
            if existing is not None:
                if existing.get("payload_hash") != payload_hash:
                    raise SubmissionError("request identifier was reused with different input")
                if existing.get("status") in {"pending", "succeeded"}:
                    return session
                if existing.get("status") in {"failed", "interrupted"} and existing.get("retryable"):
                    if session.revision != expected_revision:
                        raise RevisionConflictError("session revision is stale")
                    existing["status"] = "pending"
                    existing["error_code"] = None
                    return self.save_session(session, expected_revision)
                raise SubmissionError("request cannot be retried")
            if session.revision != expected_revision:
                raise RevisionConflictError("session revision is stale")
            if any(operation.get("status") == "pending" for operation in session.operations.values()):
                raise SubmissionError("another teaching request is pending")
            if session.state == "help_input":
                if text is not None or not any(value and value.strip() for value in session.task_input.model_dump().values()):
                    raise SubmissionError("at least one saved task field is required to start")
                session.state = "tutoring"
                session.events.append(_event(session.events, "state_changed", request_id, {"from": "help_input", "to": "tutoring"}))
            elif session.state == "tutoring":
                if text is None or not text.strip():
                    raise SubmissionError("a tutoring response cannot be blank")
                session.events.append(_event(session.events, "student_input", request_id, {"text": text}))
            else:
                raise SubmissionError("session is not accepting a teaching turn")
            session.operations[request_id] = {
                "kind": "turn",
                "payload_hash": payload_hash,
                "status": "pending",
                "student_event_id": session.events[-1]["event_id"] if text is not None else None,
                "assistant_event_id": None,
                "error_code": None,
                "retryable": False,
                "response_id": None,
            }
            return self.save_session(session, expected_revision)

    def reconcile_submission(self, session_id: str) -> Session:
        """Resolve an interrupted submit from the immutable snapshot commit point."""
        with self._lock_for(session_id):
            session = self.get_session(session_id)
            if session.state != "submitting":
                return session
            submission_id = session.submission.get("id")
            if not isinstance(submission_id, str):
                raise SubmissionError("submitting session has no submission identifier")
            snapshot_path = self._snapshot_path(session_id, submission_id)
            if snapshot_path.exists():
                snapshot = AtomicJsonFileStore.read(snapshot_path)
                self._existing_snapshot(session, submission_id, str(snapshot["final_answer"]))
                self._receipt_from_snapshot(session, submission_id, snapshot)
                return self.get_session(session_id)
            session.state = "submit_failed"
            if session.submission["attempts"] and session.submission["attempts"][-1]["result"] == "pending":
                session.submission["attempts"][-1]["result"] = "failed"
            session.submission["result"] = "failed"
            session.events.append(_event(session.events, "operation_failed", submission_id, {"code": "submission_interrupted"}))
            return self.save_session(session, session.revision)

    def complete_turn(
        self, session_id: str, request_id: str, result: SkillResult, expected_revision: int,
        metadata: dict[str, str | None] | None = None,
    ) -> Session:
        with self._lock_for(session_id):
            _uuid4(request_id)
            session = self.get_session(session_id)
            operation = session.operations.get(request_id)
            if operation is None:
                raise SubmissionError("teaching request does not exist")
            if operation.get("status") == "succeeded":
                return session
            if operation.get("status") != "pending" or session.revision != expected_revision:
                raise RevisionConflictError("teaching request is not pending at this revision")
            if session.state != "tutoring":
                raise SubmissionError("teaching result is not allowed in this state")
            if metadata:
                operation["response_id"] = metadata.get("response_id")
                session.runtime["returned_model"] = metadata.get("returned_model")
                session.runtime["sdk_version"] = metadata.get("sdk_version")
            if result.error is not None:
                operation["status"] = "failed"
                operation["error_code"] = result.error.code
                operation["retryable"] = result.error.retryable
                session.events.append(_event(session.events, "operation_failed", request_id, {"code": result.error.code}))
            else:
                next_state = route_signal(session.state, result)
                if result.signal is None:
                    raise SubmissionError("successful teaching result must carry a route signal")
                if result.student_visible_text:
                    visible_event = _event(session.events, "student_visible_output", request_id, {"text": result.student_visible_text})
                    session.events.append(visible_event)
                    operation["assistant_event_id"] = visible_event["event_id"]
                session.teaching_progress = result.teaching_progress
                operation["status"] = "succeeded"
                if next_state != session.state:
                    session.events.append(_event(session.events, "state_changed", request_id, {"from": session.state, "to": next_state}))
                    session.state = next_state
            return self.save_session(session, expected_revision)

    def submit_session(self, session_id: str, submission_id: str, final_answer: str, expected_revision: int) -> Receipt:
        with self._lock_for(session_id):
            _uuid4(submission_id)
            session = self.get_session(session_id)
            existing_id = session.submission.get("id")
            if existing_id not in (None, submission_id):
                raise SubmissionError("submission identifier differs from the locked session identifier")
            snapshot = self._existing_snapshot(session, submission_id, final_answer)
            if snapshot is not None:
                return self._receipt_from_snapshot(session, submission_id, snapshot)
            if session.state == "completed":
                raise SubmissionError("completed session has no valid snapshot")
            if session.revision != expected_revision:
                raise RevisionConflictError("session revision is stale")
            if session.state not in {"final_answer", "submit_failed"} or not final_answer.strip():
                raise SubmissionError("session is not ready for a non-empty final answer")

            previous_state = session.state
            session.state = "submitting"
            session.final_answer_draft = final_answer
            session.submission["id"] = submission_id
            answer_hash = hashlib.sha256(final_answer.encode("utf-8")).hexdigest()
            session.submission["attempts"].append({"submission_id": submission_id, "answer_hash": answer_hash, "result": "pending"})
            session.submission["result"] = None
            session.events.append(_event(session.events, "state_changed", submission_id, {"from": previous_state, "to": "submitting"}))
            session.events.append(_event(session.events, "submission_attempt", submission_id, {"submission_id": submission_id, "answer_hash": answer_hash, "result": "pending"}))
            self.save_session(session, expected_revision)

            saved_at = _now()
            snapshot_events = list(session.events)
            snapshot_events.append(_event(snapshot_events, "submission_succeeded", submission_id, {"submission_id": submission_id, "record_id": session.record_id}))
            snapshot_events.append(_event(snapshot_events, "state_changed", submission_id, {"from": "submitting", "to": "completed"}))
            snapshot_path = self._snapshot_path(session_id, submission_id)
            snapshot = {
                "schema_version": "1.0",
                "record_id": session.record_id,
                "session_id": session_id,
                "skill": session.skill.model_dump(),
                "runtime": session.runtime,
                "task_input": session.task_input.model_dump(),
                "events": snapshot_events,
                "authorized_materials": session.authorized_materials.model_dump(),
                "missing_fields": session.missing_fields,
                "final_answer": final_answer,
                "submission": {"id": submission_id, "submitted_at": saved_at, "result": "saved"},
            }
            try:
                AtomicJsonFileStore.write_new(snapshot_path, snapshot)
            except (OSError, ValueError):
                failed = self.get_session(session_id)
                failed.state = "submit_failed"
                failed.submission["attempts"][-1]["result"] = "failed"
                failed.submission["result"] = "failed"
                failed.events.append(_event(failed.events, "operation_failed", submission_id, {"code": "snapshot_write_failed"}))
                try:
                    self.save_session(failed, failed.revision)
                except OSError:
                    pass
                raise
            return self._receipt_from_snapshot(session, submission_id, snapshot)
