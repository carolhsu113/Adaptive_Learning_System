"""Real browser -> Core -> Agent -> locked Skill -> SDK, with provider transport controlled."""
import json
from pathlib import Path

import httpx
import pytest
from openai import OpenAI
from playwright.sync_api import expect

from app.runtime.openai_responses import OpenAIResponsesRuntime
from app.runtime.skill_loader import load_skill_package
from tests.e2e.test_student_flow import student_page  # noqa: F401


OPENING = "Hello, John. Mrs. Peabody shared your task: solve 3(x − 2) = 12 and show your reasoning. You wrote 3x − 2 = 12, then 3x = 14 and x = 14/3, and you're not sure where you went wrong. Which terms did the 3 multiply in your first step?"
FOLLOW_UP = "What happens to the term −2 when you multiply the expression inside the brackets by 3?"


class ProviderBoundary:
    def __init__(self):
        self.calls = []
        package = load_skill_package(Path(__file__).resolve().parents[2] / "config/skill_registry.json")
        self.client = OpenAI(api_key="test-only-key", base_url="https://provider.test", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(self.respond)))
        self.runtime = OpenAIResponsesRuntime(client=self.client, model="test-model", package=package)

    def respond(self, request):
        self.calls.append(json.loads(request.content))
        text = OPENING if len(self.calls) == 1 else FOLLOW_UP
        return httpx.Response(200, json={
            "id": f"resp_contract_{len(self.calls)}", "model": "test-model", "status": "completed",
            "output": [{"type": "message", "id": "msg_contract", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "annotations": [], "text": json.dumps({"student_visible_text": text, "teaching_status": "continue", "teaching_progress": {"checkpoint": "examining"}})}]}]
        })

    def invoke(self, invocation):
        return self.runtime.invoke(invocation)


@pytest.mark.parametrize("student_page", [ProviderBoundary()], indirect=True)
def test_page_round_trip_passes_saved_brief_and_student_reply_through_sdk(student_page, request):
    page, origin, data_dir = student_page
    boundary = request.node.callspec.params["student_page"]
    page.goto(origin)
    page.get_by_role("button", name="Ask a teacher").click()
    page.get_by_text(OPENING, exact=True).wait_for()
    assert page.get_by_role("button", name="Send", exact=True).is_disabled()
    first = boundary.calls[0]
    context = json.loads(first["input"].split("Invocation context:\n", 1)[1])
    assert context["learning_brief"]["student_name"] == "John"
    assert context["learning_brief"]["teacher_label"] == "Mrs. Peabody"
    assert context["task_input"]["concern"] == "I’m not sure where I went wrong."
    assert context["conversation"] == []
    assert context["student_input"] == ""
    assert "# Socratic Inquiry Skill 2.0" in first["input"]
    assert first["text"]["format"]["type"] == "json_schema"
    assert first["store"] is False

    reply = "In my first step, I multiplied x by 3, but I left −2 as it was."
    page.get_by_label("Your reply").fill(reply)
    expect(page.get_by_role("button", name="Send", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Send", exact=True).click()
    page.get_by_text(FOLLOW_UP, exact=True).wait_for()
    second = json.loads(boundary.calls[1]["input"].split("Invocation context:\n", 1)[1])
    assert second["student_input"] == reply
    assert second["conversation"][0]["text"] == OPENING
    assert "brief greeting" not in boundary.calls[1]["input"]
    expect(page.locator(".tutor-message.assistant")).to_have_count(2)
    expect(page.locator(".tutor-message.student")).to_have_count(1)
    expect(page.get_by_label("Your reply")).to_have_value("")
    assert page.get_by_role("button", name="Ready to practice independently").is_disabled()
    stored = json.loads(next((data_dir / "sessions").glob("*.json")).read_text(encoding="utf-8"))
    assert stored["task_input"] == context["task_input"]
    boundary.client.close()


@pytest.mark.parametrize("student_page", [ProviderBoundary()], indirect=True)
def test_acceptance_runner_exercises_full_page_flow_against_controlled_provider(student_page, tmp_path):
    import subprocess
    import sys
    _, origin, _ = student_page
    evidence = tmp_path / "controlled-acceptance"
    result = subprocess.run([sys.executable, "scripts/verify_student_chat.py", "--origin", origin, "--evidence-dir", str(evidence)], cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, encoding="utf-8", timeout=45, env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads((evidence / "student-chat.json").read_text(encoding="utf-8"))
    assert record["status"] == "passed"
    assert record["opening"] == OPENING
    assert record["follow_up"] == FOLLOW_UP
