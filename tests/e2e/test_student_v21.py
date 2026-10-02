"""The 2.1 entry screen is static until it hands off to a real SK-01 session."""

from __future__ import annotations

import json

import pytest

from tests.e2e.test_student_flow import student_page  # noqa: F401
from tests.fixtures.runtime_results import structured_result


class FailFirstOpening:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, invocation):
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("simulated provider timeout")
        return structured_result("Hello, John. What did the 3 multiply in your first step?", "continue", "examining")


class DelayedOpening:
    def __init__(self):
        from threading import Event
        self.release = Event()

    def invoke(self, invocation):
        if not invocation.student_input:
            self.release.wait(5)
            return structured_result("Hello, John. Mrs. Peabody shared your task and attempt. Which terms did the 3 multiply?", "continue", "examining")
        return structured_result("How would you check both terms?", "continue", "examining")


@pytest.mark.parametrize("student_page", [DelayedOpening()], indirect=True)
def test_enter_queues_reply_while_opening_is_pending_then_sends_once(student_page, request):
    from playwright.sync_api import expect
    page, origin, data_dir = student_page
    runtime = request.node.callspec.params["student_page"]
    try:
        page.goto(origin)
        page.get_by_role("button", name="Ask a teacher").click()
        page.get_by_text("Connecting to your teacher…", exact=True).wait_for()
        reply = page.get_by_label("Your reply")
        reply.fill("I multiplied x by 3.")
        expect(page.get_by_role("button", name="Send", exact=True)).to_be_enabled(timeout=1000)
        reply.press("Enter")
        expect(page.locator(".tutor-message.student.queued")).to_have_count(1)
        runtime.release.set()
        expect(page.locator(".tutor-message.assistant")).to_have_count(2)
        expect(page.locator(".tutor-message.student:not(.queued)")).to_have_count(1)
    finally:
        runtime.release.set()


def test_hung_opening_request_stops_connecting_and_preserves_draft(student_page):
    page, origin, _ = student_page
    page.goto(origin)
    page.evaluate("""() => {
      const originalFetch = window.fetch;
      const originalTimeout = AbortSignal.timeout.bind(AbortSignal);
      AbortSignal.timeout = () => originalTimeout(200);
      window.fetch = (url, options) => url.endsWith('/turns')
        ? new Promise((resolve, reject) => options.signal?.addEventListener('abort', () => reject(new DOMException('Timed out', 'AbortError'))))
        : originalFetch(url, options);
    }""")
    page.get_by_role("button", name="Ask a teacher").click()
    page.get_by_label("Your reply").fill("Keep my thought.")
    page.get_by_role("button", name="Retry", exact=True).wait_for(timeout=2000)
    assert page.get_by_label("Your reply").input_value() == "Keep my thought."
    assert page.get_by_role("button", name="Send", exact=True).is_enabled()


def test_homepage_hands_fixed_problem_to_real_tutoring_without_submission(student_page) -> None:
    page, origin, data_dir = student_page
    calls = []
    page.on("request", lambda request: calls.append((request.method, request.url)))

    page.goto(origin)
    assert page.get_by_role("heading", name="Draft work").is_visible()
    assert page.get_by_label("My answer").is_disabled()
    assert page.get_by_role("button", name="Submit assignment").is_disabled()
    assert page.get_by_label("Message to Mrs. Peabody").is_disabled()
    assert page.get_by_role("button", name="Record voice").is_disabled()
    assert page.get_by_role("button", name="Add attachment").is_disabled()
    assert page.get_by_role("button", name="Ask a teacher").is_enabled()
    assert not any("/api/" in url for _, url in calls)

    page.get_by_role("button", name="Ask a teacher").click()
    page.get_by_role("heading", name="Socratic").wait_for()
    page.get_by_text("请检查 <script>alert(1)</script> 这一行。").wait_for()
    assert page.get_by_label("Learning brief from Mrs. Peabody").is_visible()
    assert page.get_by_label("Shared whiteboard").is_visible()
    assert page.locator("#brief-task").inner_text() == "Solve the equation 3(x − 2) = 12. Show your reasoning step by step."
    assert page.locator("#brief-attempt").inner_text().count("x = 14/3") == 1
    assert page.locator("#brief-concern").inner_text() == "I’m not sure where I went wrong."
    assert page.get_by_label("Socratic tutoring conversation").get_by_text("请检查 <script>alert(1)</script> 这一行。").count() == 1
    assert page.get_by_role("button", name="Ready to practice independently").is_disabled()
    assert page.locator("#tutor").evaluate("el => getComputedStyle(el).gridTemplateColumns.split(' ').length") == 3
    assert page.get_by_text("When you wrote your first step, which terms inside the brackets", exact=False).count() == 0
    assert page.locator("script").count() == 1
    assert any(method == "POST" and url.endswith("/api/sessions") for method, url in calls)
    assert any(method == "PUT" and url.endswith("/draft") for method, url in calls)
    assert any(method == "POST" and url.endswith("/turns") for method, url in calls)
    assert not any(url.endswith("/submit") or url.endswith("/final-answer") for _, url in calls)
    sessions = list((data_dir / "sessions").glob("*.json"))
    assert len(sessions) == 1
    stored = json.loads(sessions[0].read_text(encoding="utf-8"))
    assert stored["task_input"]["problem"] == "Solve the equation 3(x − 2) = 12. Show your reasoning step by step."
    assert stored["task_input"]["original_answer"] == "x = 14/3"
    assert stored["task_input"]["attempts"] == "3x − 2 = 12\n3x = 14 → x = 14/3"
    assert stored["task_input"]["other_known_information"] is None
    assert len([event for event in stored["events"] if event["type"] == "student_visible_output"]) == 1

    page.reload()
    assert page.get_by_role("heading", name="Draft work").is_visible()
    page.get_by_role("button", name="Ask a teacher").click()
    page.get_by_text("请检查 <script>alert(1)</script> 这一行。").wait_for()
    page.get_by_label("Your reply").fill("I need to distribute the 3 first.")
    page.get_by_role("button", name="Send").click()
    page.get_by_text("I need to distribute the 3 first.").wait_for()
    from playwright.sync_api import expect
    expect(page.get_by_role("button", name="Ready to practice independently")).to_be_enabled()
    page.get_by_role("button", name="Ready to practice independently").click()
    page.get_by_text("Independent practice will open in a later version.", exact=False).wait_for()
    assert not any(url.endswith("/submit") or url.endswith("/final-answer") for _, url in calls)


def test_homepage_assets_load_and_does_not_offer_legacy_start(student_page) -> None:
    page, origin, _ = student_page
    missing = []
    page.on("response", lambda response: missing.append(response.url) if response.status >= 400 else None)
    page.goto(origin)
    assert page.get_by_role("img", name="Mrs. Peabody").is_visible()
    assert page.get_by_role("heading", name="Task checklist").is_visible()
    assert page.get_by_role("heading", name="Task review score").is_visible()
    assert page.get_by_role("button", name="开始对话").count() == 0
    assert not missing


def test_send_tracks_typed_text_and_rejects_blank_reply(student_page) -> None:
    page, origin, _ = student_page
    page.goto(origin)
    page.get_by_role("button", name="Ask a teacher").click()
    page.get_by_text("请检查 <script>alert(1)</script> 这一行。").wait_for()
    send = page.get_by_role("button", name="Send", exact=True)
    reply = page.get_by_label("Your reply")
    assert send.is_disabled()
    reply.fill("I multiplied x by 3.")
    assert send.is_enabled()
    reply.fill("   ")
    assert send.is_disabled()


def test_returning_to_root_shows_home_before_resuming_saved_session(student_page) -> None:
    page, origin, data_dir = student_page
    page.goto(origin)
    page.get_by_role("button", name="Ask a teacher").click()
    page.get_by_text("请检查 <script>alert(1)</script> 这一行。").wait_for()
    session_id = page.evaluate("localStorage.getItem('als.active_session_id')")

    page.goto(origin)
    assert page.get_by_role("heading", name="Draft work").is_visible()
    assert page.get_by_role("heading", name="Socratic").count() == 0
    assert page.evaluate("localStorage.getItem('als.active_session_id')") == session_id

    page.get_by_role("button", name="Ask a teacher").click()
    page.get_by_text("请检查 <script>alert(1)</script> 这一行。").wait_for()
    assert len(list((data_dir / "sessions").glob("*.json"))) == 1


def test_failed_draft_can_retry_without_creating_a_second_session(student_page) -> None:
    page, origin, data_dir = student_page
    failed_once = {"value": False}

    def fail_first_draft(route):
        if not failed_once["value"]:
            failed_once["value"] = True
            route.abort()
        else:
            route.continue_()

    page.route("**/api/sessions/*/draft", fail_first_draft)
    page.goto(origin)
    page.evaluate("() => { document.getElementById('ask-teacher').click(); document.getElementById('ask-teacher').click(); }")
    page.get_by_text("Could not prepare the question. Please retry.").wait_for()
    page.get_by_role("button", name="Retry").click()
    page.get_by_text("请检查 <script>alert(1)</script> 这一行。").wait_for()
    assert len(list((data_dir / "sessions").glob("*.json"))) == 1


@pytest.mark.parametrize("student_page", [FailFirstOpening()], indirect=True)
def test_failed_opening_can_retry_after_reload_and_unlock_chat(student_page) -> None:
    page, origin, data_dir = student_page
    page.goto(origin)
    page.get_by_role("button", name="Ask a teacher").click()
    page.get_by_role("button", name="Retry").wait_for()
    assert page.get_by_label("Your reply").is_enabled()
    assert page.get_by_role("button", name="Send").is_disabled()
    page.evaluate("sessionStorage.clear()")

    page.reload()
    page.get_by_role("button", name="Ask a teacher").click()
    page.get_by_text("Hello, John. What did the 3 multiply in your first step?").wait_for(timeout=3000)
    assert page.get_by_label("Your reply").is_enabled()
    assert len(list((data_dir / "sessions").glob("*.json"))) == 1


@pytest.mark.parametrize("student_page", [FailFirstOpening()], indirect=True)
def test_student_can_draft_during_opening_failure_without_sending_early(student_page) -> None:
    page, origin, _ = student_page
    page.goto(origin)
    page.get_by_role("button", name="Ask a teacher").click()
    page.get_by_role("button", name="Retry").wait_for()

    reply = page.get_by_label("Your reply")
    assert reply.is_enabled()
    assert page.get_by_role("button", name="Send").is_disabled()
    reply.fill("I think the 3 multiplied x.")
    assert page.get_by_role("button", name="Send", exact=True).is_enabled()
    page.get_by_role("button", name="Retry", exact=True).click()
    page.get_by_text("Hello, John. What did the 3 multiply in your first step?").wait_for()
    assert reply.input_value() == "I think the 3 multiplied x."
    assert page.get_by_role("button", name="Send").is_enabled()


@pytest.mark.parametrize("student_page", [FailFirstOpening()], indirect=True)
def test_send_recovers_opening_before_student_reply_and_does_not_duplicate(student_page):
    page, origin, data_dir = student_page
    page.goto(origin)
    page.get_by_role("button", name="Ask a teacher").click()
    page.get_by_role("button", name="Retry").wait_for()
    page.get_by_label("Your reply").fill("I multiplied x by 3.")
    page.evaluate("() => { document.getElementById('tutor-send').click(); document.getElementById('tutor-send').click(); }")
    page.get_by_text("I multiplied x by 3.", exact=True).wait_for()
    from playwright.sync_api import expect
    expect(page.locator(".tutor-message.assistant")).to_have_count(2)
    stored = json.loads(next((data_dir / "sessions").glob("*.json")).read_text(encoding="utf-8"))
    assert sum(e["type"] == "student_input" for e in stored["events"]) == 1
    assert [e["type"] for e in stored["events"] if e["type"] in {"student_visible_output", "student_input"}] == ["student_visible_output", "student_input", "student_visible_output"]


def test_enter_sends_once_shift_enter_keeps_a_newline(student_page):
    page, origin, data_dir = student_page
    page.goto(origin)
    page.get_by_role("button", name="Ask a teacher").click()
    page.get_by_text("请检查 <script>alert(1)</script> 这一行。").wait_for()
    reply = page.get_by_label("Your reply")
    reply.fill("I checked")
    reply.press("Shift+Enter")
    assert reply.input_value() == "I checked\n"
    assert page.locator(".tutor-message.student").count() == 0
    reply.type("both terms")
    reply.press("Enter")
    page.get_by_text("I checked\nboth terms", exact=True).wait_for()
    page.locator(".tutor-message.student:not(.queued)").wait_for()
    assert page.locator(".tutor-message.student").count() == 1
    stored = json.loads(next((data_dir / "sessions").glob("*.json")).read_text(encoding="utf-8"))
    assert sum(e["type"] == "student_input" for e in stored["events"]) == 1


def test_failed_send_displays_unsent_reply_and_system_notice_by_john(student_page):
    page, origin, data_dir = student_page
    page.goto(origin)
    page.get_by_role("button", name="Ask a teacher").click()
    page.get_by_text("请检查 <script>alert(1)</script> 这一行。").wait_for()
    page.route("**/api/sessions/*/turns", lambda route: route.abort())
    page.get_by_label("Your reply").fill("My thought is about both terms.")
    page.get_by_role("button", name="Send", exact=True).click()
    page.get_by_text("My thought is about both terms.", exact=True).wait_for()
    page.get_by_text("Connection lost. Your message is still here; please retry.").wait_for()
    assert page.locator(".tutor-message.student.queued").count() == 1
    assert page.get_by_text("Delivery unconfirmed", exact=True).is_visible()
    assert page.locator("header > .student + #system-notice").count() == 1
    assert page.locator("#system-notice #retry-tutor").count() == 1
    assert page.get_by_role("button", name="Retry").evaluate("el => getComputedStyle(el).backgroundColor") != page.get_by_role("button", name="Send", exact=True).evaluate("el => getComputedStyle(el).backgroundColor")
    page.screenshot(path=str(data_dir / "system-notice.png"), full_page=True)
