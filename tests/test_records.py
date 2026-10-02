from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from app.contracts import SkillError, SkillIdentity, SkillResult, TaskInput
from app.core.records import RevisionConflictError, SessionStore, SubmissionError, create_session
from app.core.file_store import AtomicJsonFileStore, DataDirLease


def _skill() -> SkillIdentity:
    return SkillIdentity(id="SK-01", version="2.0", package_sha256="a" * 64)


def test_save_session_writes_a_readable_revisioned_record(tmp_path) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    session.task_input = TaskInput(concern="我不知道哪里算错了")

    saved = store.save_session(session, expected_revision=0)

    assert saved.revision == 1
    recovered = store.get_session(saved.session_id)
    assert recovered.task_input.concern == "我不知道哪里算错了"
    assert recovered.missing_fields[0]["field"] == "task_input.problem"


def test_save_session_rejects_an_outdated_revision_without_overwriting_data(tmp_path) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    saved = store.save_session(session, expected_revision=0)
    saved.task_input = TaskInput(concern="已保存文本")
    store.save_session(saved, expected_revision=1)

    saved.task_input = TaskInput(concern="过期文本")
    try:
        store.save_session(saved, expected_revision=1)
    except RevisionConflictError:
        pass
    else:
        raise AssertionError("outdated save must be rejected")

    assert store.get_session(saved.session_id).task_input.concern == "已保存文本"


def test_submit_creates_exactly_one_snapshot_and_replays_the_same_receipt(tmp_path) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    session.state = "final_answer"
    saved = store.save_session(session, expected_revision=0)

    receipt = store.submit_session(saved.session_id, "0d15b9af-c155-42bd-b0a1-4b3c1e7b17c4", "学生独立作答", saved.revision)
    replay = store.submit_session(saved.session_id, receipt.submission_id, "学生独立作答", receipt.revision)

    assert replay.record_id == receipt.record_id
    assert len(list((tmp_path / "snapshots").glob("*.json"))) == 1
    assert store.get_session(saved.session_id).state == "completed"


def test_submission_snapshot_is_reconciled_after_completed_session_write_fails(tmp_path, monkeypatch) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    session.state = "final_answer"
    submission_id = "0d15b9af-c155-42bd-b0a1-4b3c1e7b17c4"
    session.submission["id"] = submission_id
    saved = store.save_session(session, expected_revision=0)
    original_write = AtomicJsonFileStore.write

    def interrupt_completed_write(path, payload):
        if path == tmp_path / "sessions" / f"{saved.session_id}.json" and payload["state"] == "completed":
            raise OSError("injected completed-session write failure")
        return original_write(path, payload)

    monkeypatch.setattr(AtomicJsonFileStore, "write", interrupt_completed_write)
    with pytest.raises(OSError, match="injected"):
        store.submit_session(saved.session_id, submission_id, "学生独立作答", saved.revision)

    monkeypatch.setattr(AtomicJsonFileStore, "write", original_write)
    receipt = store.submit_session(saved.session_id, submission_id, "学生独立作答", saved.revision)

    assert receipt.submission_id == submission_id
    assert store.get_session(saved.session_id).state == "completed"
    assert len(list((tmp_path / "snapshots").glob("*.json"))) == 1


def test_completed_submission_replay_rejects_a_different_answer(tmp_path) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    session.state = "final_answer"
    saved = store.save_session(session, expected_revision=0)
    submission_id = "0d15b9af-c155-42bd-b0a1-4b3c1e7b17c4"
    store.submit_session(saved.session_id, submission_id, "原始作答", saved.revision)

    with pytest.raises(ValueError):
        store.submit_session(saved.session_id, submission_id, "篡改后的作答", saved.revision)


def test_snapshot_contains_exactly_one_ordered_success_event(tmp_path) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    session.state = "final_answer"
    session.task_input = TaskInput(original_answer="x = 14/3")
    saved = store.save_session(session, expected_revision=0)
    submission_id = "0d15b9af-c155-42bd-b0a1-4b3c1e7b17c4"
    store.submit_session(saved.session_id, submission_id, "x = 6", saved.revision)
    snapshot_path = next((tmp_path / "snapshots").glob("*.json"))
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))

    assert snapshot_path.name == f"{saved.session_id}--{submission_id}.json"
    assert snapshot["task_input"]["original_answer"] == "x = 14/3"
    assert snapshot["final_answer"] == "x = 6"
    assert [event["type"] for event in snapshot["events"]].count("submission_succeeded") == 1
    assert snapshot["events"][-1]["seq"] == len(snapshot["events"])


def test_store_rejects_non_uuid_session_paths(tmp_path) -> None:
    store = SessionStore(tmp_path)
    with pytest.raises(ValueError):
        store.get_session("../outside")


def test_turn_request_is_saved_once_and_conflicting_reuse_is_rejected(tmp_path) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    session.state = "tutoring"
    saved = store.save_session(session, 0)
    request_id = "2ab5db66-d9eb-46c1-9c05-1fa8bc903ad1"

    first = store.begin_turn(saved.session_id, request_id, "我再检查 -2", saved.revision)
    replay = store.begin_turn(saved.session_id, request_id, "我再检查 -2", saved.revision)

    assert replay.revision == first.revision
    assert [event["payload"]["text"] for event in replay.events if event["type"] == "student_input"] == ["我再检查 -2"]
    assert replay.operations[request_id]["status"] == "pending"
    with pytest.raises(SubmissionError):
        store.begin_turn(saved.session_id, request_id, "我改成其他回答", first.revision)


def test_skill_result_commits_one_visible_reply_and_skill_owned_progress(tmp_path) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    session.state = "tutoring"
    saved = store.save_session(session, 0)
    request_id = "2ab5db66-d9eb-46c1-9c05-1fa8bc903ad1"
    pending = store.begin_turn(saved.session_id, request_id, "我检查 -2", saved.revision)
    result = SkillResult(signal="ready_for_submission", student_visible_text="", teaching_progress={"checkpoint": "finished"}, error=None)

    completed = store.complete_turn(saved.session_id, request_id, result, pending.revision)

    assert completed.state == "ready_for_submission"
    assert completed.teaching_progress == {"checkpoint": "finished"}
    assert completed.operations[request_id]["status"] == "succeeded"
    assert [event["type"] for event in completed.events].count("student_input") == 1
    assert [event["type"] for event in completed.events].count("student_visible_output") == 0
    assert [event["seq"] for event in completed.events] == list(range(1, len(completed.events) + 1))


def test_failed_turn_keeps_progress_and_can_retry_without_duplicate_student_event(tmp_path) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    session.state = "tutoring"
    session.teaching_progress = {"checkpoint": "examining"}
    saved = store.save_session(session, 0)
    request_id = "2ab5db66-d9eb-46c1-9c05-1fa8bc903ad1"
    pending = store.begin_turn(saved.session_id, request_id, "我检查 -2", saved.revision)
    error = SkillResult(signal=None, student_visible_text=None, teaching_progress=None, error=SkillError(code="runtime_timeout", retryable=True))
    failed = store.complete_turn(saved.session_id, request_id, error, pending.revision)
    retry = store.begin_turn(saved.session_id, request_id, "我检查 -2", failed.revision)

    assert retry.state == "tutoring"
    assert retry.teaching_progress == {"checkpoint": "examining"}
    assert retry.operations[request_id]["status"] == "pending"
    assert [event["type"] for event in retry.events].count("student_input") == 1


def test_record_write_failure_does_not_advance_stored_revision(tmp_path, monkeypatch) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    saved = store.save_session(session, 0)
    original = AtomicJsonFileStore.write

    def fail(path, payload):
        raise OSError("injected write failure")

    monkeypatch.setattr(AtomicJsonFileStore, "write", fail)
    with pytest.raises(OSError, match="injected"):
        store.save_session(saved, saved.revision)
    monkeypatch.setattr(AtomicJsonFileStore, "write", original)

    assert saved.revision == 1
    assert store.get_session(saved.session_id).revision == 1


def test_invalid_state_is_rejected_before_a_session_file_is_written(tmp_path) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    session.state = "finished_by_keyword"

    with pytest.raises(ValueError):
        store.save_session(session, 0)
    assert not list((tmp_path / "sessions").glob("*.json"))


def test_unknown_session_schema_version_is_not_silently_loaded(tmp_path) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    saved = store.save_session(session, 0)
    path = tmp_path / "sessions" / f"{saved.session_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = "2.0"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        store.get_session(saved.session_id)


def test_failed_snapshot_write_preserves_answer_and_marks_submit_failed(tmp_path, monkeypatch) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    session.state = "final_answer"
    saved = store.save_session(session, 0)
    original = AtomicJsonFileStore.write_new

    def fail_snapshot(path, payload):
        if path.parent.name == "snapshots":
            raise OSError("injected snapshot write failure")
        return original(path, payload)

    monkeypatch.setattr(AtomicJsonFileStore, "write_new", fail_snapshot)
    with pytest.raises(OSError, match="injected"):
        store.submit_session(saved.session_id, "0d15b9af-c155-42bd-b0a1-4b3c1e7b17c4", "独立作答", saved.revision)

    recovered = store.get_session(saved.session_id)
    assert recovered.state == "submit_failed"
    assert recovered.final_answer_draft == "独立作答"
    assert not list((tmp_path / "snapshots").glob("*.json"))


def test_out_of_order_event_is_rejected_without_overwriting_the_session(tmp_path) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    saved = store.save_session(session, 0)
    saved.events.append({
        "event_id": "58f4ffbb-d685-4623-856e-049b872d57ce",
        "seq": 2,
        "at": "2026-09-18T00:00:00.000Z",
        "type": "student_input",
        "request_id": "2ab5db66-d9eb-46c1-9c05-1fa8bc903ad1",
        "payload": {"text": "hello"},
    })

    with pytest.raises(ValueError):
        store.save_session(saved, saved.revision)
    assert store.get_session(saved.session_id).events == []


def test_snapshot_carries_locked_runtime_metadata(tmp_path) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    session.state = "final_answer"
    session.runtime = {"provider": "openai_responses", "requested_model": "configured-model", "returned_model": "reported-model", "sdk_version": "3.11.0"}
    saved = store.save_session(session, 0)
    store.submit_session(saved.session_id, "0d15b9af-c155-42bd-b0a1-4b3c1e7b17c4", "x = 6", saved.revision)
    snapshot = json.loads(next((tmp_path / "snapshots").glob("*.json")).read_text(encoding="utf-8"))

    assert snapshot["runtime"] == session.runtime


def test_failed_answer_can_be_edited_and_retried_under_the_same_submission_id(tmp_path, monkeypatch) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    session.state = "final_answer"
    saved = store.save_session(session, 0)
    submission_id = "0d15b9af-c155-42bd-b0a1-4b3c1e7b17c4"
    original = AtomicJsonFileStore.write_new

    def fail_snapshot(path, payload):
        if path.parent.name == "snapshots":
            raise OSError("temporary failure")
        return original(path, payload)

    monkeypatch.setattr(AtomicJsonFileStore, "write_new", fail_snapshot)
    with pytest.raises(OSError):
        store.submit_session(saved.session_id, submission_id, "第一次答案", saved.revision)
    failed = store.get_session(saved.session_id)
    monkeypatch.setattr(AtomicJsonFileStore, "write_new", original)

    store.submit_session(saved.session_id, submission_id, "修改后的答案", failed.revision)
    completed = store.get_session(saved.session_id)
    snapshot = json.loads(next((tmp_path / "snapshots").glob("*.json")).read_text(encoding="utf-8"))

    assert [attempt["result"] for attempt in completed.submission["attempts"]] == ["failed", "saved"]
    assert completed.submission["attempts"][0]["answer_hash"] != completed.submission["attempts"][1]["answer_hash"]
    assert snapshot["final_answer"] == "修改后的答案"


def test_completed_session_cannot_be_rewritten_even_with_current_revision(tmp_path) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    session.state = "final_answer"
    saved = store.save_session(session, 0)
    store.submit_session(saved.session_id, "0d15b9af-c155-42bd-b0a1-4b3c1e7b17c4", "已提交作答", saved.revision)
    completed = store.get_session(saved.session_id)
    completed.final_answer_draft = "事后篡改"

    with pytest.raises(SubmissionError):
        store.save_session(completed, completed.revision)
    assert store.get_session(saved.session_id).final_answer_draft == "已提交作答"


def test_saved_record_identity_cannot_be_changed(tmp_path) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    saved = store.save_session(session, 0)
    saved.record_id = "58f4ffbb-d685-4623-856e-049b872d57ce"

    with pytest.raises(SubmissionError):
        store.save_session(saved, saved.revision)


def test_start_request_and_literal_start_text_are_not_idempotent_equivalents(tmp_path) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    session.task_input = TaskInput(concern="哪里错了？")
    saved = store.save_session(session, 0)
    request_id = "2ab5db66-d9eb-46c1-9c05-1fa8bc903ad1"
    pending = store.begin_turn(saved.session_id, request_id, None, saved.revision)

    with pytest.raises(SubmissionError):
        store.begin_turn(saved.session_id, request_id, "<start>", pending.revision)


def test_two_store_instances_cannot_both_commit_the_same_revision(tmp_path, monkeypatch) -> None:
    first_store = SessionStore(tmp_path)
    second_store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    saved = first_store.save_session(session, 0)
    first = first_store.get_session(saved.session_id)
    second = second_store.get_session(saved.session_id)
    first.task_input = TaskInput(concern="first")
    second.task_input = TaskInput(concern="second")
    first_write_started = Event()
    release_first = Event()
    original = AtomicJsonFileStore.write

    def slow_first_write(path, payload):
        if payload.get("revision") == 2 and payload.get("task_input", {}).get("concern") == "first":
            first_write_started.set()
            assert release_first.wait(2)
        return original(path, payload)

    monkeypatch.setattr(AtomicJsonFileStore, "write", slow_first_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(first_store.save_session, first, saved.revision)
        assert first_write_started.wait(2)
        second_future = pool.submit(second_store.save_session, second, saved.revision)
        time.sleep(0.05)
        release_first.set()
        outcomes = []
        for future in (first_future, second_future):
            try:
                future.result(timeout=2)
                outcomes.append("saved")
            except RevisionConflictError:
                outcomes.append("conflict")

    assert outcomes == ["saved", "conflict"]
    assert first_store.get_session(saved.session_id).task_input.concern == "first"


def test_atomic_new_file_cannot_replace_an_existing_success_file(tmp_path) -> None:
    path = tmp_path / "immutable" / "receipt.json"
    AtomicJsonFileStore.write_new(path, {"result": "saved", "final_answer": "original"})

    with pytest.raises(FileExistsError):
        AtomicJsonFileStore.write_new(path, {"result": "saved", "final_answer": "changed"})
    assert json.loads(path.read_text(encoding="utf-8"))["final_answer"] == "original"


def test_data_directory_allows_only_one_active_writer_lease(tmp_path) -> None:
    with DataDirLease(tmp_path):
        with pytest.raises(RuntimeError, match="already in use"):
            with DataDirLease(tmp_path):
                pass
    with DataDirLease(tmp_path):
        assert (tmp_path / ".writer.lock").exists()


def test_text_event_without_text_is_not_saved(tmp_path) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    session.events.append({
        "event_id": "58f4ffbb-d685-4623-856e-049b872d57ce",
        "seq": 1,
        "at": "2026-09-18T00:00:00.000Z",
        "type": "student_input",
        "request_id": "2ab5db66-d9eb-46c1-9c05-1fa8bc903ad1",
        "payload": {},
    })

    with pytest.raises(ValueError):
        store.save_session(session, 0)


def test_success_event_must_match_snapshot_submission_identity(tmp_path) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    session.state = "final_answer"
    saved = store.save_session(session, 0)
    submission_id = "0d15b9af-c155-42bd-b0a1-4b3c1e7b17c4"
    receipt = store.submit_session(saved.session_id, submission_id, "独立作答", saved.revision)
    path = tmp_path / "snapshots" / f"{saved.session_id}--{submission_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    success = next(event for event in payload["events"] if event["type"] == "submission_succeeded")
    success["payload"]["submission_id"] = "58f4ffbb-d685-4623-856e-049b872d57ce"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError):
        store.submit_session(saved.session_id, submission_id, "独立作答", receipt.revision)


def test_snapshot_validation_failure_does_not_leave_submitting_state(tmp_path, monkeypatch) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    session.state = "final_answer"
    saved = store.save_session(session, 0)

    def invalid_snapshot(path, payload):
        raise ValueError("injected snapshot validation failure")

    monkeypatch.setattr(AtomicJsonFileStore, "write_new", invalid_snapshot)
    with pytest.raises(ValueError, match="injected"):
        store.submit_session(saved.session_id, "0d15b9af-c155-42bd-b0a1-4b3c1e7b17c4", "独立作答", saved.revision)

    assert store.get_session(saved.session_id).state == "submit_failed"
