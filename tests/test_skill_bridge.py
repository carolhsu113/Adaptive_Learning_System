from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.runtime.skill_loader import SkillLoadError, load_skill_package
from app.contracts import SkillError, SkillResult, TaskInput
from app.core.skill_bridge import invoke_skill
from app.runtime.openai_responses import OpenAIResponsesRuntime
from tests.fixtures.runtime_results import structured_result


def _write_package(root: Path) -> Path:
    (root / "references").mkdir(parents=True)
    (root / "SKILL.md").write_bytes(b"# Socratic Inquiry Skill 2.0\n")
    (root / "references" / "teaching-flow.md").write_bytes(b"# Flow\n")
    (root / "references" / "visibility.md").write_bytes(b"# Visibility\n")
    return root


def _package_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for relative_path in ("SKILL.md", "references/teaching-flow.md", "references/visibility.md"):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update((root / relative_path).read_bytes())
    return digest.hexdigest()


def _write_registry(tmp_path: Path, root: Path, package_hash: str) -> Path:
    registry_path = tmp_path / "skill_registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "skills": [
                    {
                        "skill_id": "SK-01",
                        "version": "2.0",
                        "entry_path": str(root / "SKILL.md"),
                        "reference_paths": [
                            str(root / "references" / "teaching-flow.md"),
                            str(root / "references" / "visibility.md"),
                        ],
                        "package_sha256": package_hash,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return registry_path


def test_loader_hashes_entry_and_required_references(tmp_path: Path) -> None:
    root = _write_package(tmp_path / "socratic-inquiry")
    registry_path = _write_registry(tmp_path, root, _package_hash(root))

    package = load_skill_package(registry_path)

    assert package.id == "SK-01"
    assert package.version == "2.0"
    assert package.package_sha256 == _package_hash(root)
    assert package.files["SKILL.md"] == "# Socratic Inquiry Skill 2.0\n"


def test_loader_rejects_a_registry_hash_that_differs_from_source(tmp_path: Path) -> None:
    root = _write_package(tmp_path / "socratic-inquiry")
    registry_path = _write_registry(tmp_path, root, "0" * 64)

    with pytest.raises(SkillLoadError, match="package_sha256"):
        load_skill_package(registry_path)


def test_loader_resolves_registered_paths_from_the_application_root(tmp_path: Path) -> None:
    app_root = tmp_path / "ALS_MVP"
    root = _write_package(tmp_path / "skills" / "socratic-inquiry")
    registry_path = app_root / "config" / "skill_registry.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        json.dumps(
            {
                "skills": [
                    {
                        "skill_id": "SK-01",
                        "version": "2.0",
                        "entry_path": "../skills/socratic-inquiry/SKILL.md",
                        "reference_paths": [
                            "../skills/socratic-inquiry/references/teaching-flow.md",
                            "../skills/socratic-inquiry/references/visibility.md",
                        ],
                        "package_sha256": _package_hash(root),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    package = load_skill_package(registry_path)

    assert package.root_path == str(root.resolve())


def test_contracts_preserve_raw_task_text_and_reject_unknown_signals() -> None:
    task_input = TaskInput(concern="  原文不应被裁切  ", problem=None)
    assert task_input.concern == "  原文不应被裁切  "
    assert task_input.problem is None

    with pytest.raises(ValueError):
        SkillResult(
            signal="finished_by_keyword",
            student_visible_text="继续检查。",
            teaching_progress=None,
            error=SkillError(code="invalid", retryable=False),
        )


class FakeRuntime:
    def __init__(self, result: object) -> None:
        self.result = result

    def invoke(self, invocation: object) -> object:
        return self.result


def _invocation():
    from app.contracts import Invocation, SkillIdentity

    return Invocation(
        session_id="46ba1452-b520-4d1e-9b90-55c4a67a1000",
        request_id="a5206221-e296-4ba1-b5dc-44fa8fc37a70",
        skill=SkillIdentity(
            id="SK-01",
            version="2.0",
            package_sha256="a" * 64,
        ),
        task_input=TaskInput(concern="我不知道哪里算错了"),
        conversation=[],
        student_input="我把括号外的 3 乘了 x",
    )


def test_bridge_preserves_the_model_question_without_rewriting() -> None:
    result = invoke_skill(
        _invocation(),
        runtime=FakeRuntime(structured_result("请检查你写的等式左边。\n", "continue", "examining")),
    )

    assert result.signal == "continue"
    assert result.student_visible_text == "请检查你写的等式左边。\n"
    assert result.error is None


def test_bridge_maps_natural_end_without_visible_text_to_submission_signal() -> None:
    result = invoke_skill(
        _invocation(),
        runtime=FakeRuntime(structured_result("", "natural_end", "finished")),
    )

    assert result.signal == "ready_for_submission"
    assert result.student_visible_text == ""


def test_boundary_stop_never_becomes_a_completion_signal() -> None:
    result = invoke_skill(
        _invocation(),
        runtime=FakeRuntime(structured_result("材料需要澄清。", "boundary_stop", "boundary")),
    )

    assert result.signal is None
    assert result.error == SkillError(code="boundary_stop", retryable=False)


@pytest.mark.parametrize(
    "raw_result, expected_code",
    [
        ({"student_visible_text": "", "teaching_status": "continue", "teaching_progress": {"checkpoint": "examining"}}, "empty_student_visible_text"),
        (structured_result("继续检查。", "unknown", "examining"), "invalid_teaching_status"),
        ({"teaching_status": "continue"}, "invalid_runtime_output"),
    ],
)
def test_invalid_runtime_output_does_not_advance_tutoring(raw_result: object, expected_code: str) -> None:
    result = invoke_skill(_invocation(), runtime=FakeRuntime(raw_result))

    assert result.signal is None
    assert result.error == SkillError(code=expected_code, retryable=False)


class FakeResponses:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] | None = None

    def create(self, **kwargs: object) -> object:
        self.kwargs = kwargs
        return type("Response", (), {"id": "resp_test_1", "model": "test-model-version", "status": "completed", "output_text": structured_result("检查这个步骤。", "continue", "examining"), "output": []})()


class FakeClient:
    def __init__(self) -> None:
        self.responses = FakeResponses()


def test_provider_diagnostic_classifies_read_timeout_without_exposing_secrets():
    import httpx
    from openai import APITimeoutError
    client = FakeClient()
    def timeout(**kwargs):
        assert kwargs["reasoning"] == {"effort": "none"}
        assert "John" not in kwargs["input"]
        raise APITimeoutError(request=httpx.Request("POST", "https://api.deepseek.com/responses")) from httpx.ReadTimeout("private-secret")
    client.responses.create = timeout
    result = OpenAIResponsesRuntime(client=client, model="deepseek-flash").diagnose_connection("responses")
    assert result["state"] == "timeout"
    assert result["transport"] == "ReadTimeout"
    assert "private-secret" not in str(result)


def test_provider_diagnostic_reports_rejection_reason_and_redacts_key():
    import httpx
    from openai import BadRequestError
    client = FakeClient()
    client.api_key = "private-secret"
    def rejected(**kwargs):
        raise BadRequestError("rejected", response=httpx.Response(400, request=httpx.Request("POST", "https://api.deepseek.com/responses")), body={"error": {"code": "invalid_model", "message": "Model rejected; private-secret"}})
    client.responses.create = rejected
    report = OpenAIResponsesRuntime(client=client, model="deepseek-flash").diagnose_connection("responses")
    assert report["provider_code"] == "invalid_model"
    assert report["provider_message"] == "Model rejected; [redacted]"
    assert "private-secret" not in str(report)


def test_provider_diagnostic_preserves_non_json_gateway_error_without_key():
    import httpx
    from openai import BadRequestError
    client = FakeClient()
    client.api_key = "private-secret"
    def rejected(**kwargs):
        response = httpx.Response(400, request=httpx.Request("POST", "https://api.deepseek.com/responses"), text="Gateway refused private-secret", headers={"Content-Type": "text/plain"})
        raise BadRequestError("bad gateway request", response=response, body="Gateway refused private-secret")
    client.responses.create = rejected
    report = OpenAIResponsesRuntime(client=client, model="deepseek-flash").diagnose_connection("responses")
    assert report["provider_message"] == "Gateway refused [redacted]"
    assert report["content_type"] == "text/plain"
    assert "private-secret" not in str(report)


def test_provider_diagnostic_endpoint_does_not_create_a_learning_session(tmp_path):
    from fastapi.testclient import TestClient
    from app.api import create_app
    from app.contracts import SkillIdentity
    runtime = OpenAIResponsesRuntime(client=FakeClient(), model="test-model")
    with TestClient(create_app(data_dir=tmp_path, skill=SkillIdentity(id="SK-01", version="2.0", package_sha256="a"*64), runtime=runtime, model="test-model")) as client:
        assert client.post("/api/runtime/diagnostics", json={"protocol": "responses"}).status_code == 403
        response = client.post("/api/runtime/diagnostics", json={"protocol": "responses"}, headers={"Origin": "http://testserver"})
        assert response.status_code == 200
        assert response.json()["state"] == "completed"
        assert not list((tmp_path / "sessions").glob("*.json"))


@pytest.mark.parametrize("base_url, expected", [
    ("https://api.deepseek.com/", {"effort": "low"}),
    ("https://api.openai.com/v1/", None),
])
def test_runtime_uses_bounded_thinking_effort_for_deepseek_only(tmp_path: Path, base_url, expected):
    root = _write_package(tmp_path / "socratic-inquiry")
    package = load_skill_package(_write_registry(tmp_path, root, _package_hash(root)))
    client = FakeClient()
    client.base_url = base_url
    OpenAIResponsesRuntime(client=client, model="test-model", package=package).invoke(_invocation())
    assert client.responses.kwargs.get("reasoning") == expected
    assert "# Socratic Inquiry Skill 2.0" in client.responses.kwargs["input"]


@pytest.mark.parametrize("status", ["failed", "incomplete", "in_progress"])
def test_runtime_rejects_noncompleted_response_even_if_text_looks_valid(tmp_path: Path, status: str) -> None:
    root = _write_package(tmp_path / "socratic-inquiry")
    package = load_skill_package(_write_registry(tmp_path, root, _package_hash(root)))
    client = FakeClient()
    client.responses.create = lambda **kwargs: type("Response", (), {
        "status": status,
        "output_text": structured_result("不应显示。", "natural_end", "finished"),
        "output": [],
    })()

    with pytest.raises(RuntimeError, match="not completed"):
        OpenAIResponsesRuntime(client=client, model="test-model", package=package).invoke(_invocation())


def test_runtime_rejects_explicit_refusal_even_when_output_text_is_present(tmp_path: Path) -> None:
    root = _write_package(tmp_path / "socratic-inquiry")
    package = load_skill_package(_write_registry(tmp_path, root, _package_hash(root)))
    client = FakeClient()
    refusal = type("Refusal", (), {"type": "refusal", "refusal": "cannot comply"})()
    message = type("Message", (), {"content": [refusal]})()
    client.responses.create = lambda **kwargs: type("Response", (), {
        "status": "completed", "output_text": structured_result("不应显示。", "natural_end", "finished"), "output": [message],
    })()

    with pytest.raises(RuntimeError, match="refusal"):
        OpenAIResponsesRuntime(client=client, model="test-model", package=package).invoke(_invocation())


def test_runtime_sends_locked_context_and_disables_provider_storage(tmp_path: Path) -> None:
    root = _write_package(tmp_path / "socratic-inquiry")
    package = load_skill_package(_write_registry(tmp_path, root, _package_hash(root)))
    client = FakeClient()

    runtime = OpenAIResponsesRuntime(client=client, model="test-model")
    raw_result = runtime.invoke(_invocation(), package)

    assert raw_result == structured_result("检查这个步骤。", "continue", "examining")
    assert client.responses.kwargs is not None
    assert client.responses.kwargs["model"] == "test-model"
    assert client.responses.kwargs["store"] is False
    assert "# Socratic Inquiry Skill 2.0" in str(client.responses.kwargs["input"])
    assert "我把括号外的 3 乘了 x" in str(client.responses.kwargs["input"])
    metadata = runtime.take_metadata(_invocation().request_id)
    assert metadata["response_id"] == "resp_test_1"
    assert metadata["returned_model"] == "test-model-version"
    assert metadata["sdk_version"]


def test_first_model_turn_requests_brief_acknowledgement_and_one_question(tmp_path: Path) -> None:
    root = _write_package(tmp_path / "socratic-inquiry")
    package = load_skill_package(_write_registry(tmp_path, root, _package_hash(root)))
    client = FakeClient()
    invocation = _invocation().model_copy(update={
        "student_input": "",
        "task_input": TaskInput(
            problem="Solve 3(x - 2) = 12.",
            attempts="3x - 2 = 12",
            concern="I am not sure where I went wrong.",
        ),
    })

    OpenAIResponsesRuntime(client=client, model="test-model", package=package).invoke(invocation)

    prompt = str(client.responses.kwargs["input"])
    assert "brief greeting" in prompt
    assert "Learning Brief" in prompt
    assert "exactly one main question" in prompt
    assert "Do not guess the error cause" in prompt
    assert "Solve 3(x - 2) = 12." in prompt


def test_later_model_turn_does_not_repeat_opening_instruction(tmp_path: Path) -> None:
    root = _write_package(tmp_path / "socratic-inquiry")
    package = load_skill_package(_write_registry(tmp_path, root, _package_hash(root)))
    client = FakeClient()

    OpenAIResponsesRuntime(client=client, model="test-model", package=package).invoke(_invocation())

    assert "brief greeting" not in str(client.responses.kwargs["input"])


def test_bridge_can_use_a_runtime_bound_to_the_locked_skill_package(tmp_path: Path) -> None:
    root = _write_package(tmp_path / "socratic-inquiry")
    package = load_skill_package(_write_registry(tmp_path, root, _package_hash(root)))
    runtime = OpenAIResponsesRuntime(client=FakeClient(), model="test-model", package=package)

    result = invoke_skill(_invocation(), runtime=runtime)

    assert result.signal == "continue"
    assert result.student_visible_text == "检查这个步骤。"
