from __future__ import annotations

from app.contracts import SkillIdentity, TaskInput
from app.core.records import SessionStore, create_session
from app.core.recovery import recover_session


def _skill() -> SkillIdentity:
    return SkillIdentity(id="SK-01", version="2.0", package_sha256="a" * 64)


def test_recovery_marks_pending_turn_interrupted_without_reexecuting_it(tmp_path) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    session.task_input = TaskInput(concern="哪里错了？")
    session.state = "tutoring"
    saved = store.save_session(session, 0)
    request_id = "2ab5db66-d9eb-46c1-9c05-1fa8bc903ad1"
    store.begin_turn(saved.session_id, request_id, "我再检查", saved.revision)

    view = recover_session(SessionStore(tmp_path), saved.session_id)

    assert view.state == "tutoring"
    assert view.operation_status == "interrupted"
    assert view.conversation[-1].text == "我再检查"
    assert store.get_session(saved.session_id).operations[request_id]["status"] == "interrupted"


def test_recovery_of_submitting_without_snapshot_keeps_answer_for_retry(tmp_path) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    session.state = "submitting"
    session.final_answer_draft = "学生原文"
    session.submission["id"] = "0d15b9af-c155-42bd-b0a1-4b3c1e7b17c4"
    saved = store.save_session(session, 0)

    view = recover_session(SessionStore(tmp_path), saved.session_id)

    assert view.state == "submit_failed"
    assert view.final_answer_draft == "学生原文"
    assert view.submission_id == session.submission["id"]
