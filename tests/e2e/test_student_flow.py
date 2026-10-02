"""Browser-level checks of the confirmed continuous student workspace."""

from __future__ import annotations

import socket
import time
from pathlib import Path
from threading import Thread

import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from app.api import create_app
from app.contracts import SkillIdentity
from app.core.records import SessionStore
from app.core.file_store import AtomicJsonFileStore


class Runtime:
    def invoke(self, invocation):
        if invocation.student_input:
            return {"student_visible_text": "", "teaching_status": "natural_end", "teaching_progress": {"checkpoint": "finished"}}
        return {"student_visible_text": "请检查 <script>alert(1)</script> 这一行。", "teaching_status": "continue", "teaching_progress": {"checkpoint": "examining"}}


@pytest.fixture
def student_page(tmp_path, request):
    skill = SkillIdentity(id="SK-01", version="2.0", package_sha256="a" * 64)
    app = create_app(data_dir=tmp_path, skill=skill, runtime=getattr(request, "param", None) or Runtime(), model="test-model")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        with sync_playwright() as playwright:
            chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
            browser = playwright.chromium.launch(headless=True, executable_path=str(chrome) if chrome.is_file() else None)
            page = browser.new_page()
            yield page, f"http://127.0.0.1:{port}", tmp_path
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_material_to_answer_flow_is_safe_and_recoverable(student_page) -> None:
    page, origin, data_dir = student_page
    page.goto(origin + "/v1/")
    page.get_by_label("当前困惑").fill("我不懂这一步")
    page.get_by_text("草稿已保存").wait_for()
    page.get_by_role("button", name="开始对话").click()
    page.get_by_text("请检查 <script>alert(1)</script> 这一行。").wait_for()
    assert page.locator("script").count() == 1
    assert page.evaluate("Object.keys(localStorage)") == ["als.active_session_id"]

    page.reload()
    page.get_by_text("请检查 <script>alert(1)</script> 这一行。").wait_for()
    reopened_context = page.context.browser.new_context(storage_state=page.context.storage_state())
    try:
        reopened_page = reopened_context.new_page()
        reopened_page.goto(origin + "/v1/")
        reopened_page.get_by_text("请检查 <script>alert(1)</script> 这一行。").wait_for()
        assert reopened_page.evaluate("Object.keys(localStorage)") == ["als.active_session_id"]
    finally:
        reopened_context.close()
    page.get_by_label("本轮回应").fill("我再检查")
    page.get_by_role("button", name="发送").click()
    page.get_by_role("button", name="前往最终作答").wait_for()
    page.get_by_role("button", name="前往最终作答").click()
    assert page.get_by_label("我的最终作答").input_value() == ""
    page.get_by_label("我的最终作答").fill("x = 6")
    page.get_by_role("button", name="提交作答").click()
    page.get_by_text("作答已保存").wait_for()
    assert page.evaluate("Object.keys(localStorage)") == []
    assert len(list((data_dir / "snapshots").glob("*.json"))) == 1


def test_draft_failure_preserves_text_then_retry_saves_and_blank_answer_is_rejected(student_page) -> None:
    page, origin, data_dir = student_page
    page.goto(origin + "/v1/")
    failed_once = {"value": False}

    def fail_first_draft(route):
        if not failed_once["value"]:
            failed_once["value"] = True
            route.abort()
        else:
            route.continue_()

    page.route("**/api/sessions/*/draft", fail_first_draft)
    page.get_by_label("当前困惑").fill("我的原文不能丢")
    page.get_by_text("保存失败").wait_for()
    assert page.get_by_label("当前困惑").input_value() == "我的原文不能丢"
    page.get_by_label("当前困惑").fill("我的原文不能丢，补充一步")
    page.get_by_text("草稿已保存").wait_for()
    page.get_by_role("button", name="开始对话").click()
    page.get_by_text("请检查 <script>alert(1)</script> 这一行。").wait_for()
    page.get_by_label("本轮回应").fill("我再检查")
    page.get_by_role("button", name="发送").click()
    page.get_by_role("button", name="前往最终作答").click()
    page.get_by_role("button", name="提交作答").click()
    page.get_by_text("请先填写最终作答").wait_for()
    assert not list((data_dir / "snapshots").glob("*.json"))


def test_completed_session_cannot_be_reopened_as_editable(student_page) -> None:
    page, origin, data_dir = student_page
    page.goto(origin + "/v1/")
    page.get_by_label("当前困惑").fill("求解")
    page.get_by_role("button", name="开始对话").click()
    page.get_by_text("请检查 <script>alert(1)</script> 这一行。").wait_for()
    page.get_by_label("本轮回应").fill("我再检查")
    page.get_by_role("button", name="发送").click()
    page.get_by_role("button", name="前往最终作答").click()
    page.get_by_label("我的最终作答").fill("x = 6")
    page.get_by_role("button", name="提交作答").dblclick()
    page.get_by_text("作答已保存").wait_for()
    page.reload()
    page.get_by_label("当前困惑").wait_for()
    assert page.get_by_label("我的最终作答").is_hidden()
    assert len(list((data_dir / "snapshots").glob("*.json"))) == 1


def test_interrupted_turn_is_retried_after_refresh_without_duplicate_student_message(student_page) -> None:
    page, origin, data_dir = student_page
    page.goto(origin + "/v1/")
    page.get_by_label("当前困惑").fill("求解")
    page.get_by_role("button", name="开始对话").click()
    page.get_by_text("请检查 <script>alert(1)</script> 这一行。").wait_for()
    session_id = page.evaluate("localStorage.getItem('als.active_session_id')")
    store = SessionStore(data_dir)
    turn_id = "b6a44ec8-e810-4ce9-bd5d-d74cfce8b4db"
    session = store.get_session(session_id)
    store.begin_turn(session_id, turn_id, "我再检查", session.revision)

    page.reload()
    page.get_by_role("button", name="重试本轮").click()
    page.get_by_role("button", name="前往最终作答").wait_for()
    assert page.get_by_text("我再检查").count() == 1
    assert sum(event["type"] == "student_input" for event in store.get_session(session_id).events) == 1


def test_submit_write_failure_keeps_answer_editable_and_retry_saves_once(student_page, monkeypatch) -> None:
    page, origin, data_dir = student_page
    page.goto(origin + "/v1/")
    page.get_by_label("当前困惑").fill("求解")
    page.get_by_role("button", name="开始对话").click()
    page.get_by_text("请检查 <script>alert(1)</script> 这一行。").wait_for()
    page.get_by_label("本轮回应").fill("我再检查")
    page.get_by_role("button", name="发送").click()
    page.get_by_role("button", name="前往最终作答").click()
    page.get_by_label("我的最终作答").fill("初稿")
    page.get_by_text("草稿已保存").last.wait_for()

    original = AtomicJsonFileStore.write_new
    failed_once = {"value": False}

    def fail_once(path, payload):
        if not failed_once["value"]:
            failed_once["value"] = True
            raise OSError("injected snapshot write failure")
        return original(path, payload)

    monkeypatch.setattr(AtomicJsonFileStore, "write_new", staticmethod(fail_once))
    page.get_by_role("button", name="提交作答").click()
    page.get_by_text("暂时无法保存").first.wait_for()
    assert page.get_by_label("我的最终作答").input_value() == "初稿"
    page.get_by_label("我的最终作答").fill("修改后的原文")
    page.get_by_text("草稿已保存").last.wait_for()
    page.get_by_role("button", name="重新提交").click()
    page.get_by_text("作答已保存").wait_for()
    assert len(list((data_dir / "snapshots").glob("*.json"))) == 1


def test_lost_submit_receipt_reuses_original_snapshot(student_page) -> None:
    page, origin, data_dir = student_page
    page.goto(origin + "/v1/")
    page.get_by_label("当前困惑").fill("求解")
    page.get_by_role("button", name="开始对话").click()
    page.get_by_text("请检查 <script>alert(1)</script> 这一行。").wait_for()
    page.get_by_label("本轮回应").fill("我再检查")
    page.get_by_role("button", name="发送").click()
    page.get_by_role("button", name="前往最终作答").click()
    page.get_by_label("我的最终作答").fill("x = 6")
    page.get_by_text("草稿已保存").last.wait_for()
    blocked = {"value": False}

    def lose_first_receipt(route):
        if not blocked["value"]:
            blocked["value"] = True
            response = route.fetch()
            assert response.status == 200
            route.abort()
        else:
            route.continue_()

    page.route("**/api/sessions/*/submit", lose_first_receipt)
    page.get_by_role("button", name="提交作答").click()
    page.get_by_text("未能确认保存结果").wait_for()
    assert len(list((data_dir / "snapshots").glob("*.json"))) == 1
    page.get_by_role("button", name="提交作答").click()
    page.get_by_text("作答已保存").wait_for()
    assert len(list((data_dir / "snapshots").glob("*.json"))) == 1


def test_new_problem_ignores_late_draft_ack_from_previous_session(student_page) -> None:
    page, origin, _ = student_page
    page.goto(origin + "/v1/")
    page.get_by_label("当前困惑").fill("旧会话的材料")
    page.get_by_text("草稿已保存").wait_for()
    page.reload()
    page.get_by_label("当前困惑").wait_for(state="visible")
    page.evaluate("""() => {
      const original = window.fetch;
      window.fetch = (...args) => original(...args).then(response =>
        args[1]?.method === 'PUT'
          ? new Promise(resolve => setTimeout(() => resolve(response), 800))
          : response);
    }""")
    with page.expect_response("**/api/sessions/*/draft"):
        page.get_by_label("当前困惑").fill("旧会话的修改")
    page.get_by_role("button", name="开始新问题").first.click()
    page.wait_for_timeout(1000)

    assert page.get_by_label("当前困惑").input_value() == ""
    assert page.evaluate("localStorage.getItem('als.active_session_id')") is None


def test_stale_draft_keeps_local_text_and_retries_with_latest_revision(student_page) -> None:
    page, origin, data_dir = student_page
    page.goto(origin + "/v1/")
    page.get_by_label("当前困惑").fill("原文")
    page.get_by_text("草稿已保存").wait_for()
    page.reload()
    page.get_by_label("当前困惑").wait_for(state="visible")
    session_id = page.evaluate("localStorage.getItem('als.active_session_id')")
    store = SessionStore(data_dir)
    session = store.get_session(session_id)
    store.save_draft(session_id, "a6f4ad3f-7507-4830-bd4b-43f582b2b27e", session.revision, task_input=session.task_input)

    page.get_by_label("当前困惑").fill("本页仍要保留的修改")
    page.get_by_role("button", name="重新保存").first.wait_for()
    assert page.get_by_label("当前困惑").input_value() == "本页仍要保留的修改"
    page.get_by_role("button", name="重新保存").first.click()
    page.get_by_text("草稿已保存").wait_for()
    assert store.get_session(session_id).task_input.concern == "本页仍要保留的修改"


def test_pending_turn_response_is_polled_until_visible_result(student_page) -> None:
    page, origin, _ = student_page
    page.goto(origin + "/v1/")
    delayed_once = {"value": False}

    def show_pending_before_completed(route):
        if not delayed_once["value"]:
            delayed_once["value"] = True
            response = route.fetch()
            body = response.json()
            body["operation_status"] = "pending"
            body["conversation"] = []
            route.fulfill(status=202, json=body)
        else:
            route.continue_()

    page.route("**/api/sessions/*/turns", show_pending_before_completed)
    page.get_by_label("当前困惑").fill("求解")
    page.get_by_role("button", name="开始对话").click()
    page.get_by_text("请检查 <script>alert(1)</script> 这一行。").wait_for()
