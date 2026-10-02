from uuid import uuid4
from pathlib import Path
import json

import httpx
import pytest
from openai import OpenAI
from fastapi.testclient import TestClient

from app.api import create_app
from app.contracts import SkillIdentity
from app.runtime.openai_responses import OpenAIResponsesRuntime
from app.runtime.skill_loader import load_skill_package


def test_peabody_draft_uses_server_file_and_is_the_actual_skill_context(tmp_path):
    invocations = []

    class CapturingRuntime:
        def invoke(self, invocation):
            invocations.append(invocation)
            return {"student_visible_text": "Which terms did you multiply by 3?", "teaching_status": "continue", "teaching_progress": {"checkpoint": "examining"}}

    app = create_app(data_dir=tmp_path, skill=SkillIdentity(id="SK-01", version="2.0", package_sha256="a" * 64), runtime=CapturingRuntime(), model="test-model")
    with TestClient(app) as client:
        headers = {"Origin": "http://testserver"}
        view = client.post("/api/sessions", headers=headers, json={"request_id": str(uuid4())}).json()
        saved = client.put(f"/api/sessions/{view['session_id']}/draft", headers=headers, json={"request_id": str(uuid4()), "expected_revision": view["revision"], "use_learning_brief": True})
        assert saved.status_code == 200
        view = saved.json()
        assert view["task_input"]["problem"] == "Solve the equation 3(x − 2) = 12. Show your reasoning step by step."
        assert view["task_input"]["attempts"] == "3x − 2 = 12\n3x = 14 → x = 14/3"
        assert view["task_input"]["concern"] == "I’m not sure where I went wrong."
        response = client.post(f"/api/sessions/{view['session_id']}/turns", headers=headers, json={"request_id": str(uuid4()), "expected_revision": view["revision"]})
        assert response.status_code == 200
        assert invocations[0].student_input == ""
        assert invocations[0].task_input.model_dump() == view["task_input"]
        assert response.json()["conversation"][0]["role"] == "assistant"


@pytest.mark.parametrize("status, code, retryable", [(401, "provider_authentication", True), (402, "provider_balance", True), (400, "provider_configuration", True), (429, "provider_rate_limit", True)])
def test_provider_failure_reports_actionable_cause_without_exposing_secrets(tmp_path, status, code, retryable):
    package = load_skill_package(Path(__file__).resolve().parents[1] / "config/skill_registry.json")
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json={"error": {"message": "private secret must not be exposed", "type": "api_error"}}))
    with OpenAI(api_key="test-only-key", base_url="https://provider.test", max_retries=0, http_client=httpx.Client(transport=transport)) as provider:
        runtime = OpenAIResponsesRuntime(client=provider, model="test-model", package=package)
        app = create_app(data_dir=tmp_path, skill=SkillIdentity(id=package.id, version=package.version, package_sha256=package.package_sha256), runtime=runtime, model="test-model")
        with TestClient(app) as client:
            headers = {"Origin": "http://testserver"}
            view = client.post("/api/sessions", headers=headers, json={"request_id": str(uuid4())}).json()
            view = client.put(f"/api/sessions/{view['session_id']}/draft", headers=headers, json={"request_id": str(uuid4()), "expected_revision": view["revision"], "use_learning_brief": True}).json()
            response = client.post(f"/api/sessions/{view['session_id']}/turns", headers=headers, json={"request_id": str(uuid4()), "expected_revision": view["revision"]})
            assert response.json()["error"]["code"] == code
            assert response.json()["error"]["retryable"] is retryable
            assert "private secret" not in response.text
            assert response.json()["conversation"] == []
