from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from fastapi.testclient import TestClient

from app.contracts import AuthorizedMaterial, AuthorizedMaterialSource, AuthorizedMaterials, SkillIdentity
from app.api import create_app


ORIGIN = "http://127.0.0.1:8767"
WRITE_HEADERS = {"Origin": ORIGIN}
VIEW_KEYS = {
    "session_id", "revision", "state", "task_input", "conversation",
    "final_answer_draft", "submission_id", "operation_status", "error", "saved_at",
}


class FakeRuntime:
    def invoke(self, invocation):
        return {
            "student_visible_text": "请检查括号内的 -2。",
            "teaching_status": "continue",
            "teaching_progress": {"checkpoint": "examining"},
        }


def _client(tmp_path) -> TestClient:
    skill = SkillIdentity(id="SK-01", version="2.0", package_sha256="a" * 64)
    app = create_app(data_dir=tmp_path, skill=skill, runtime=FakeRuntime(), model="configured-model")
    return TestClient(app, base_url=ORIGIN)


def test_create_draft_and_start_return_only_student_view(tmp_path) -> None:
    client = _client(tmp_path)
    created = client.post("/api/sessions", json={"request_id": "9c51a94d-c759-41f5-8f0a-4bd0f196559d"}, headers=WRITE_HEADERS)
    assert created.status_code == 200
    view = created.json()
    assert set(view) == VIEW_KEYS
    assert view["state"] == "help_input"
    assert view["conversation"] == []

    saved = client.put(
        f"/api/sessions/{view['session_id']}/draft",
        json={
            "request_id": "2ab5db66-d9eb-46c1-9c05-1fa8bc903ad1",
            "expected_revision": view["revision"],
            "task_input": {"concern": "哪里错了？"},
        },
        headers=WRITE_HEADERS,
    )
    assert saved.status_code == 200
    assert saved.json()["task_input"]["concern"] == "哪里错了？"

    started = client.post(
        f"/api/sessions/{view['session_id']}/turns",
        json={"request_id": "b6a44ec8-e810-4ce9-bd5d-d74cfce8b4db", "expected_revision": saved.json()["revision"]},
        headers=WRITE_HEADERS,
    )
    assert started.status_code == 200
    result = started.json()
    assert set(result) == VIEW_KEYS
    assert result["state"] == "tutoring"
    assert result["conversation"] == [{"event_id": result["conversation"][0]["event_id"], "role": "assistant", "text": "请检查括号内的 -2。"}]
    assert "teaching_progress" not in started.text
    assert "checkpoint" not in started.text


def test_client_cannot_supply_state_or_hidden_materials(tmp_path) -> None:
    client = _client(tmp_path)
    response = client.post(
        "/api/sessions",
        json={"request_id": "9c51a94d-c759-41f5-8f0a-4bd0f196559d", "state": "completed"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 400
    assert "completed" not in response.text


def test_write_rejects_foreign_origin_without_creating_a_session(tmp_path) -> None:
    client = _client(tmp_path)
    response = client.post(
        "/api/sessions",
        json={"request_id": "9c51a94d-c759-41f5-8f0a-4bd0f196559d"},
        headers={"Origin": "https://foreign.example"},
    )
    assert response.status_code == 403
    assert not list((tmp_path / "sessions").glob("*.json"))


def test_repeated_create_request_returns_the_original_session(tmp_path) -> None:
    client = _client(tmp_path)
    body = {"request_id": "9c51a94d-c759-41f5-8f0a-4bd0f196559d"}
    first = client.post("/api/sessions", json=body, headers=WRITE_HEADERS)
    second = client.post("/api/sessions", json=body, headers=WRITE_HEADERS)

    assert second.status_code == 200
    assert second.json()["session_id"] == first.json()["session_id"]
    assert len(list((tmp_path / "sessions").glob("*.json"))) == 1


def test_draft_request_replay_is_stable_and_different_payload_conflicts(tmp_path) -> None:
    client = _client(tmp_path)
    created = client.post("/api/sessions", json={"request_id": "9c51a94d-c759-41f5-8f0a-4bd0f196559d"}, headers=WRITE_HEADERS).json()
    path = f"/api/sessions/{created['session_id']}/draft"
    body = {
        "request_id": "2ab5db66-d9eb-46c1-9c05-1fa8bc903ad1",
        "expected_revision": created["revision"],
        "task_input": {"concern": "原始困惑"},
    }
    first = client.put(path, json=body, headers=WRITE_HEADERS)
    replay = client.put(path, json=body, headers=WRITE_HEADERS)
    changed = client.put(path, json=body | {"task_input": {"concern": "修改内容"}}, headers=WRITE_HEADERS)

    assert first.status_code == replay.status_code == 200
    assert replay.json()["revision"] == first.json()["revision"]
    assert changed.status_code == 409


def test_final_answer_is_independent_and_submission_replays_one_receipt(tmp_path) -> None:
    class NaturalEndRuntime:
        def invoke(self, invocation):
            return {
                "student_visible_text": "",
                "teaching_status": "natural_end",
                "teaching_progress": {"checkpoint": "finished"},
            }

    skill = SkillIdentity(id="SK-01", version="2.0", package_sha256="a" * 64)
    app = create_app(data_dir=tmp_path, skill=skill, runtime=NaturalEndRuntime(), model="configured-model")
    client = TestClient(app, base_url=ORIGIN)
    created = client.post("/api/sessions", json={"request_id": "9c51a94d-c759-41f5-8f0a-4bd0f196559d"}, headers=WRITE_HEADERS).json()
    session_path = f"/api/sessions/{created['session_id']}"
    draft = client.put(
        session_path + "/draft",
        json={"request_id": "2ab5db66-d9eb-46c1-9c05-1fa8bc903ad1", "expected_revision": created["revision"], "task_input": {"concern": "检查这道题"}},
        headers=WRITE_HEADERS,
    ).json()
    ready = client.post(
        session_path + "/turns",
        json={"request_id": "b6a44ec8-e810-4ce9-bd5d-d74cfce8b4db", "expected_revision": draft["revision"]},
        headers=WRITE_HEADERS,
    ).json()
    assert ready["state"] == "ready_for_submission"

    enter_body = {"request_id": "f901fbab-c4b8-4ddd-b0d3-995be99cd084", "expected_revision": ready["revision"]}
    entered = client.post(session_path + "/final-answer", json=enter_body, headers=WRITE_HEADERS)
    replay = client.post(session_path + "/final-answer", json=enter_body, headers=WRITE_HEADERS)
    assert entered.status_code == replay.status_code == 200
    assert replay.json()["submission_id"] == entered.json()["submission_id"]
    assert replay.json()["revision"] == entered.json()["revision"]
    assert entered.json()["final_answer_draft"] == ""

    blank = client.post(
        session_path + "/submit",
        json={"submission_id": entered.json()["submission_id"], "expected_revision": entered.json()["revision"], "final_answer": "  "},
        headers=WRITE_HEADERS,
    )
    assert blank.status_code == 422

    submit_body = {"submission_id": entered.json()["submission_id"], "expected_revision": entered.json()["revision"], "final_answer": "x = 6"}
    completed = client.post(session_path + "/submit", json=submit_body, headers=WRITE_HEADERS)
    receipt_replay = client.post(session_path + "/submit", json=submit_body, headers=WRITE_HEADERS)
    assert completed.status_code == receipt_replay.status_code == 200
    assert completed.json()["state"] == "completed"
    assert completed.json()["saved_at"] == receipt_replay.json()["saved_at"]
    assert client.get(session_path).status_code == 410
    assert len(list((tmp_path / "snapshots").glob("*.json"))) == 1


def test_static_route_cannot_read_sibling_configuration_via_query_parameter(tmp_path) -> None:
    client = _client(tmp_path)
    response = client.get("/?filename=../config/settings.example.toml")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "ALS Student — 2.1" in response.text
    assert "skill_registry" not in response.text


def test_get_during_an_active_turn_does_not_mark_it_interrupted(tmp_path) -> None:
    class BlockingRuntime:
        def __init__(self):
            self.started = Event()
            self.release = Event()

        def invoke(self, invocation):
            self.started.set()
            assert self.release.wait(3)
            return {"student_visible_text": "继续检查这一项。", "teaching_status": "continue", "teaching_progress": {"checkpoint": "examining"}}

    runtime = BlockingRuntime()
    skill = SkillIdentity(id="SK-01", version="2.0", package_sha256="a" * 64)
    app = create_app(data_dir=tmp_path, skill=skill, runtime=runtime, model="configured-model")
    client = TestClient(app, base_url=ORIGIN)
    created = client.post("/api/sessions", json={"request_id": "9c51a94d-c759-41f5-8f0a-4bd0f196559d"}, headers=WRITE_HEADERS).json()
    path = f"/api/sessions/{created['session_id']}"
    saved = client.put(path + "/draft", json={"request_id": "2ab5db66-d9eb-46c1-9c05-1fa8bc903ad1", "expected_revision": created["revision"], "task_input": {"concern": "不懂"}}, headers=WRITE_HEADERS).json()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            client.post, path + "/turns",
            json={"request_id": "b6a44ec8-e810-4ce9-bd5d-d74cfce8b4db", "expected_revision": saved["revision"]},
            headers=WRITE_HEADERS,
        )
        assert runtime.started.wait(2)
        pending = client.get(path)
        runtime.release.set()
        finished = future.result(timeout=3)

    assert pending.status_code == 200
    assert pending.json()["operation_status"] == "pending"
    assert finished.status_code == 200


def test_changed_runtime_lock_refuses_existing_session_without_rewriting_it(tmp_path) -> None:
    first = _client(tmp_path)
    created = first.post("/api/sessions", json={"request_id": "9c51a94d-c759-41f5-8f0a-4bd0f196559d"}, headers=WRITE_HEADERS).json()
    skill = SkillIdentity(id="SK-01", version="2.0", package_sha256="a" * 64)
    changed = TestClient(create_app(data_dir=tmp_path, skill=skill, runtime=FakeRuntime(), model="different-model"), base_url=ORIGIN)

    response = changed.get(f"/api/sessions/{created['session_id']}")

    assert response.status_code == 503
    assert set(response.json()) == VIEW_KEYS
    assert response.json()["session_id"] == created["session_id"]
    assert response.json()["error"]["code"] == "runtime_lock_changed"
    assert "different-model" not in response.text


def test_hidden_material_never_crosses_student_view_even_on_error_and_restart(tmp_path) -> None:
    first = _client(tmp_path)
    created = first.post("/api/sessions", json={"request_id": "9c51a94d-c759-41f5-8f0a-4bd0f196559d"}, headers=WRITE_HEADERS).json()
    session_id = created["session_id"]
    session = first.app.state.store.get_session(session_id)
    sentinel = "PRIVATE-REFERENCE-ANSWER-SENTINEL"
    session.authorized_materials = AuthorizedMaterials(
        reference_answer=AuthorizedMaterial(
            value=sentinel,
            source=AuthorizedMaterialSource(kind="teacher", source_id="teacher-1", teacher_confirmation="confirmed"),
        )
    )
    first.app.state.store.save_session(session, session.revision)

    restarted = _client(tmp_path)
    recovered = restarted.get(f"/api/sessions/{session_id}")
    invalid = restarted.put(
        f"/api/sessions/{session_id}/draft",
        json={"request_id": "2ab5db66-d9eb-46c1-9c05-1fa8bc903ad1", "expected_revision": 1, "authorized_materials": sentinel},
        headers=WRITE_HEADERS,
    )

    assert recovered.status_code == 200
    assert recovered.json()["session_id"] == session_id
    assert set(recovered.json()) == VIEW_KEYS
    assert invalid.status_code == 400
    assert sentinel not in recovered.text + invalid.text


def test_student_event_id_is_stable_retry_token_after_restart(tmp_path) -> None:
    client = _client(tmp_path)
    created = client.post("/api/sessions", json={"request_id": "9c51a94d-c759-41f5-8f0a-4bd0f196559d"}, headers=WRITE_HEADERS).json()
    path = f"/api/sessions/{created['session_id']}"
    saved = client.put(path + "/draft", json={"request_id": "2ab5db66-d9eb-46c1-9c05-1fa8bc903ad1", "expected_revision": created["revision"], "task_input": {"concern": "困惑"}}, headers=WRITE_HEADERS).json()
    started = client.post(path + "/turns", json={"request_id": created["session_id"], "expected_revision": saved["revision"]}, headers=WRITE_HEADERS).json()
    turn_id = "b6a44ec8-e810-4ce9-bd5d-d74cfce8b4db"
    replied = client.post(path + "/turns", json={"request_id": turn_id, "expected_revision": started["revision"], "text": "我再检查"}, headers=WRITE_HEADERS).json()

    assert replied["conversation"][-2] == {"event_id": turn_id, "role": "student", "text": "我再检查"}


def test_runtime_response_metadata_is_saved_server_side_but_not_exposed(tmp_path) -> None:
    class TraceRuntime(FakeRuntime):
        def take_metadata(self, request_id):
            return {"response_id": "resp_test_1", "returned_model": "returned-version", "sdk_version": "3.11.0"}

    skill = SkillIdentity(id="SK-01", version="2.0", package_sha256="a" * 64)
    app = create_app(data_dir=tmp_path, skill=skill, runtime=TraceRuntime(), model="configured-model")
    client = TestClient(app, base_url=ORIGIN)
    created = client.post("/api/sessions", json={"request_id": "9c51a94d-c759-41f5-8f0a-4bd0f196559d"}, headers=WRITE_HEADERS).json()
    path = f"/api/sessions/{created['session_id']}"
    saved = client.put(path + "/draft", json={"request_id": "2ab5db66-d9eb-46c1-9c05-1fa8bc903ad1", "expected_revision": created["revision"], "task_input": {"concern": "困惑"}}, headers=WRITE_HEADERS).json()
    turn_id = created["session_id"]
    visible = client.post(path + "/turns", json={"request_id": turn_id, "expected_revision": saved["revision"]}, headers=WRITE_HEADERS)
    record = app.state.store.get_session(created["session_id"])

    assert record.operations[turn_id]["response_id"] == "resp_test_1"
    assert record.runtime["returned_model"] == "returned-version"
    assert record.runtime["sdk_version"] == "3.11.0"
    assert "resp_test_1" not in visible.text
    assert "returned-version" not in visible.text
