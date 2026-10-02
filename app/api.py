"""Same-origin student API exposing only the StudentView projection."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.contracts import ConversationEvent, Invocation, SkillIdentity, TaskInput
from app.agent.learning_brief import load_learning_brief
from app.core.record_schema import canonical_uuid4
from app.core.file_store import DataDirLease
from app.core.records import RevisionConflictError, SessionStore, SubmissionError
from app.core.recovery import StudentView, VisibleError, empty_error_view, recover_session, to_student_view
from app.core.skill_bridge import invoke_skill


class RuntimeLockError(Exception):
    def __init__(self, session):
        self.session = session


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateRequest(StrictRequest):
    request_id: str


class DraftRequest(StrictRequest):
    request_id: str
    expected_revision: int = Field(ge=1)
    task_input: TaskInput | None = None
    final_answer_draft: str | None = None
    use_learning_brief: bool = False


class TurnRequest(StrictRequest):
    request_id: str
    expected_revision: int = Field(ge=1)
    text: str | None = None


class DiagnosticRequest(StrictRequest):
    protocol: Literal["responses", "structured_responses", "chat_completions", "models"]


class FinalAnswerRequest(StrictRequest):
    request_id: str
    expected_revision: int = Field(ge=1)


class SubmitRequest(StrictRequest):
    submission_id: str
    expected_revision: int = Field(ge=1)
    final_answer: str


def _json(view: StudentView, status_code: int = 200) -> JSONResponse:
    return JSONResponse(view.model_dump(mode="json"), status_code=status_code)


def _error(code: str, message: str, status_code: int, session=None) -> JSONResponse:
    visible = VisibleError(code=code, message=message, retryable=status_code in {502, 503})
    return _json(to_student_view(session, visible) if session else empty_error_view(code, message), status_code)


def create_app(*, data_dir: Path, skill: SkillIdentity, runtime: object, model: str) -> FastAPI:
    if not model.strip():
        raise ValueError("a configured ALS_MODEL is required")
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        with DataDirLease(data_dir):
            yield

    app = FastAPI(lifespan=lifespan)
    store = SessionStore(data_dir)
    active_turns: set[tuple[str, str]] = set()
    active_lock = Lock()
    app.state.store = store
    app.state.locked_model = model

    def checked_session(session_id: str):
        session = store.get_session(session_id)
        if session.skill != skill or session.runtime["requested_model"] != model:
            raise RuntimeLockError(session)
        return session

    @app.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            expected_origin = f"{request.url.scheme}://{request.url.netloc}"
            if request.headers.get("origin") != expected_origin:
                return _error("origin_rejected", "此操作只允许从本机页面发起。", 403)
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError):
        return _error("invalid_request", "请求内容无效。", 400)

    @app.exception_handler(RevisionConflictError)
    async def stale_revision(request: Request, error: RevisionConflictError):
        return _error("revision_conflict", "页面内容已有更新，请先读取最新状态。", 409)

    @app.exception_handler(SubmissionError)
    async def illegal_operation(request: Request, error: SubmissionError):
        return _error("invalid_state", "当前状态不能执行此操作。", 409)

    @app.exception_handler(FileNotFoundError)
    async def missing_session(request: Request, error: FileNotFoundError):
        return _error("session_not_found", "无法找到原学习会话。", 404)

    @app.exception_handler(ValueError)
    async def invalid_data(request: Request, error: ValueError):
        return _error("invalid_request", "请求内容无效，或会话记录无法读取。", 400)

    @app.exception_handler(OSError)
    async def storage_unavailable(request: Request, error: OSError):
        return _error("storage_unavailable", "暂时无法保存，请保留页面中的原文并重试。", 503)

    @app.exception_handler(RuntimeLockError)
    async def runtime_lock_changed(request: Request, error: RuntimeLockError):
        return _error("runtime_lock_changed", "原会话所用的教学配置已变化，暂时无法继续。", 503, error.session)

    @app.post("/api/sessions")
    def create_student_session(body: CreateRequest):
        canonical_uuid4(body.request_id)
        session = store.create_once(body.request_id, skill, model)
        checked_session(session.session_id)
        return to_student_view(session)

    @app.post("/api/runtime/diagnostics", include_in_schema=False)
    def diagnose_provider(body: DiagnosticRequest):
        diagnostic = getattr(runtime, "diagnose_connection", None)
        if not callable(diagnostic):
            return JSONResponse({"state": "unsupported"}, status_code=501)
        return diagnostic(body.protocol)

    @app.get("/api/sessions/{session_id}")
    def get_student_session(session_id: str):
        session_id = canonical_uuid4(session_id)
        checked_session(session_id)
        with active_lock:
            active_ids = frozenset(request_id for sid, request_id in active_turns if sid == session_id)
        view = recover_session(store, session_id, active_ids)
        if view.state == "completed":
            return _error("session_completed", "本次学习记录已保存，可开始新问题。", 410, store.get_session(session_id))
        return view

    @app.put("/api/sessions/{session_id}/draft")
    def save_draft(session_id: str, body: DraftRequest):
        checked_session(canonical_uuid4(session_id))
        if body.use_learning_brief and (body.task_input is not None or body.final_answer_draft is not None):
            raise ValueError("Learning Brief cannot be mixed with client task data")
        saved = store.save_draft(
            canonical_uuid4(session_id), body.request_id, body.expected_revision,
            task_input=load_learning_brief().task_input if body.use_learning_brief else body.task_input,
            final_answer_draft=body.final_answer_draft,
        )
        return to_student_view(saved)

    @app.post("/api/sessions/{session_id}/turns")
    def submit_turn(session_id: str, body: TurnRequest):
        session_id = canonical_uuid4(session_id)
        checked_session(session_id)
        canonical_uuid4(body.request_id)
        marker = (session_id, body.request_id)
        with active_lock:
            already_active = marker in active_turns
            active_turns.add(marker)
        try:
            before = store.get_session(session_id)
            old_operation = before.operations.get(body.request_id)
            pending = store.begin_turn(session_id, body.request_id, body.text, body.expected_revision)
            if already_active or old_operation and old_operation["status"] in {"pending", "succeeded"}:
                status = old_operation["status"] if old_operation else "pending"
                return _json(to_student_view(pending), 202 if status == "pending" else 200)
            prior_events = [
                ConversationEvent(
                    event_id=str(event["event_id"]),
                    role="student" if event["type"] == "student_input" else "assistant",
                    text=str(event["payload"]["text"]),
                )
                for event in pending.events
                if event["type"] in {"student_input", "student_visible_output"}
                and not (event["type"] == "student_input" and event["request_id"] == body.request_id)
            ]
            invocation = Invocation(
                session_id=session_id,
                request_id=body.request_id,
                skill=pending.skill,
                task_input=pending.task_input,
                conversation=prior_events,
                student_input=body.text or "",
                teaching_progress=pending.teaching_progress,
                authorized_materials=pending.authorized_materials,
            )
            result = invoke_skill(invocation, runtime=runtime)
            take_metadata = getattr(runtime, "take_metadata", None)
            metadata = take_metadata(body.request_id) if callable(take_metadata) else None
            completed = store.complete_turn(session_id, body.request_id, result, pending.revision, metadata=metadata)
            if result.error is None or result.error.code == "boundary_stop":
                return to_student_view(completed)
            status_code = 503 if result.error.retryable else 502
            return _json(to_student_view(completed), status_code)
        finally:
            if not already_active:
                with active_lock:
                    active_turns.discard(marker)

    @app.post("/api/sessions/{session_id}/final-answer")
    def begin_final_answer(session_id: str, body: FinalAnswerRequest):
        checked_session(canonical_uuid4(session_id))
        return to_student_view(store.enter_final_answer(canonical_uuid4(session_id), body.request_id, body.expected_revision))

    @app.post("/api/sessions/{session_id}/submit")
    def submit_final_answer(session_id: str, body: SubmitRequest):
        session_id = canonical_uuid4(session_id)
        checked_session(session_id)
        canonical_uuid4(body.submission_id)
        if not body.final_answer.strip():
            return _error("empty_final_answer", "请先填写最终作答。", 422)
        try:
            store.submit_session(session_id, body.submission_id, body.final_answer, body.expected_revision)
        except (OSError, ValueError) as error:
            if isinstance(error, (RevisionConflictError, SubmissionError)):
                raise
            return _error("storage_unavailable", "暂时无法保存，请保留页面中的原文并重试。", 503, store.get_session(session_id))
        return to_student_view(store.get_session(session_id))

    web_dir = Path(__file__).resolve().parent.parent / "web"
    def static_endpoint(name: str):
        def serve():
            path = web_dir / name
            if not path.is_file():
                return _error("page_not_ready", "页面文件尚未安装。", 404)
            return FileResponse(path)
        return serve

    for route, filename in (
        ("/", "index.html"), ("/app.js", "app.js"), ("/styles.css", "styles.css"),
        ("/v1/", "v1/index.html"), ("/v1/app.js", "v1/app.js"),
        ("/v1/styles.css", "v1/styles.css"),
        ("/assets/peabody-avatar.png", "assets/peabody-avatar.png"),
        ("/assets/john-avatar.png", "assets/john-avatar.png"),
        ("/assets/socratic-avatar.jpg", "assets/socratic-avatar.jpg"),
    ):
        app.add_api_route(route, static_endpoint(filename), methods=["GET"], include_in_schema=False)

    return app
