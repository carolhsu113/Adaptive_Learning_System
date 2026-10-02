from __future__ import annotations

import subprocess
import sys
import json
from pathlib import Path

import pytest

from scripts import verify_live
from scripts.verify_live import ConfigurationError, validate_runtime_environment


def test_live_verification_requires_key_and_model_without_echoing_them() -> None:
    with pytest.raises(ConfigurationError, match="ALS_API_KEY"):
        validate_runtime_environment({"ALS_MODEL": "configured-model"})

    with pytest.raises(ConfigurationError, match="ALS_MODEL"):
        validate_runtime_environment({"OPENAI_API_KEY": "secret"})


def test_live_verification_accepts_a_configured_model() -> None:
    assert validate_runtime_environment({"ALS_API_KEY": "secret", "ALS_MODEL": "configured-model"}) == "configured-model"


def test_live_script_help_runs_by_the_documented_file_command() -> None:
    root = Path(__file__).resolve().parent.parent
    completed = subprocess.run(
        [sys.executable, "scripts/verify_live.py", "--help"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "交互式真实 SK-01 验证" in completed.stdout


def test_live_script_does_not_claim_success_without_a_real_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    monkeypatch.setattr(verify_live, "create_client", lambda *args, **kwargs: object())

    with pytest.raises(ConfigurationError, match="至少一次"):
        verify_live.run_interactive("C01", tmp_path / "evidence", {"OPENAI_API_KEY": "secret", "ALS_MODEL": "model-a"})
    assert not (tmp_path / "evidence" / "live-call-metadata.json").exists()


def test_live_script_returns_failure_when_runtime_call_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter(["合成学生输入", ""])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(verify_live, "create_client", lambda *args, **kwargs: object())

    class FailingRuntime:
        def __init__(self, **kwargs):
            pass

        def invoke(self, invocation):
            raise RuntimeError("network failed")

    monkeypatch.setattr(verify_live, "OpenAIResponsesRuntime", FailingRuntime)
    status = verify_live.run_interactive("C01", tmp_path / "evidence", {"OPENAI_API_KEY": "secret", "ALS_MODEL": "model-a"})

    assert status != 0


def test_c04_conflict_injects_traceable_authorized_reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt: "请帮我核对冲突材料")
    monkeypatch.setattr(verify_live, "create_client", lambda *args, **kwargs: object())
    captured = []

    class CapturingRuntime:
        def __init__(self, **kwargs):
            pass

        def invoke(self, invocation):
            captured.append(invocation)
            return {"student_visible_text": "材料互相矛盾，请先澄清。", "teaching_status": "boundary_stop", "teaching_progress": {"checkpoint": "boundary"}}

        def take_metadata(self, request_id):
            return {"response_id": "resp_test_1", "returned_model": "model-a-version", "sdk_version": "3.11.0"}

    monkeypatch.setattr(verify_live, "OpenAIResponsesRuntime", CapturingRuntime)
    status = verify_live.run_interactive("C04-conflict", tmp_path / "evidence", {"OPENAI_API_KEY": "secret", "ALS_MODEL": "model-a"})

    assert status == 0
    assert captured[0].authorized_materials.reference_answer.value == "x = 5"
    assert captured[0].authorized_materials.reference_answer.source.kind == "authorized_system"
    evidence = json.loads((tmp_path / "evidence" / "live-call-metadata.json").read_text(encoding="utf-8"))
    assert evidence["turns"][0]["response_id"] == "resp_test_1"
    assert evidence["turns"][0]["returned_model"] == "model-a-version"
