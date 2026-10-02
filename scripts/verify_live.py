from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.contracts import (
    AuthorizedMaterial, AuthorizedMaterials, AuthorizedMaterialSource,
    ConversationEvent, Invocation, SkillIdentity, TaskInput,
)
from app.core.skill_bridge import invoke_skill
from app.main import create_client, load_settings, resolve_api_key
from app.runtime.openai_responses import OpenAIResponsesRuntime
from app.runtime.skill_loader import load_skill_package


class ConfigurationError(ValueError):
    """A required local live-verification setting is absent."""


CASE_INPUTS = {
    "C01": TaskInput(
        concern="我不知道哪里算错了",
        problem="解方程 3(x - 2) = 12",
        original_answer="x = 14/3",
        attempts="3x - 2 = 12，所以 3x = 14，x = 14/3",
    ),
    "C02": TaskInput(concern="这道方程我不会"),
    "C03": TaskInput(
        concern="我不知道哪里算错了",
        problem="解方程 3(x - 2) = 12",
        original_answer="x = 14/3",
        attempts="3x - 2 = 12，所以 3x = 14，x = 14/3",
    ),
    "C04-conflict": TaskInput(problem="3(x - 2) = 12"),
}


def validate_runtime_environment(environment: dict[str, str]) -> str:
    try:
        resolve_api_key(environment)
    except ValueError as error:
        raise ConfigurationError("ALS_API_KEY 未配置；不会降级为受控测试替身。") from error
    model = environment.get("ALS_MODEL", "").strip()
    if not model:
        raise ConfigurationError("ALS_MODEL 未配置；不会猜测或自动替换模型。")
    return model


def _repository_root() -> Path:
    return REPOSITORY_ROOT


def _write_evidence(destination: Path, record: dict[str, object]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "live-call-metadata.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def run_interactive(case_id: str, evidence_dir: Path, environment: dict[str, str]) -> int:
    model = validate_runtime_environment(environment)
    settings = load_settings(_repository_root(), environment)
    api_key = resolve_api_key(environment)
    package = load_skill_package(_repository_root() / "config" / "skill_registry.json")
    runtime = OpenAIResponsesRuntime(
        client=create_client(api_key, settings.api_base_url, use_system_proxy=None),
        model=model,
        package=package,
    )
    task_input = CASE_INPUTS[case_id]
    authorized_materials = (
        AuthorizedMaterials(reference_answer=AuthorizedMaterial(
            value="x = 5",
            source=AuthorizedMaterialSource(
                kind="authorized_system", source_id="synthetic-conflict-fixture",
                teacher_confirmation="not_applicable",
            ),
        ))
        if case_id == "C04-conflict" else AuthorizedMaterials()
    )
    session_id = str(uuid4())
    conversation: list[ConversationEvent] = []
    progress: dict[str, str] | None = None
    turns: list[dict[str, object]] = []
    print("逐轮输入合成学生原文；直接回车结束。脚本不会生成学生回答。")

    while True:
        student_text = input("学生输入：")
        if not student_text:
            break
        invocation = Invocation(
            session_id=session_id,
            request_id=str(uuid4()),
            skill=SkillIdentity(
                id="SK-01",
                version="2.0",
                package_sha256=package.package_sha256,
            ),
            task_input=task_input,
            conversation=conversation,
            student_input=student_text,
            teaching_progress=progress,
            authorized_materials=authorized_materials,
        )
        result = invoke_skill(invocation, runtime=runtime)
        take_metadata = getattr(runtime, "take_metadata", None)
        metadata = (take_metadata(invocation.request_id) if callable(take_metadata) else None) or {}
        conversation.append(ConversationEvent(event_id=str(uuid4()), role="student", text=student_text))
        if result.student_visible_text is not None:
            conversation.append(
                ConversationEvent(event_id=str(uuid4()), role="assistant", text=result.student_visible_text)
            )
        progress = result.teaching_progress
        turns.append(
            {
                "student_input": student_text,
                "student_visible_text": result.student_visible_text,
                "signal": result.signal,
                "error": result.error.model_dump() if result.error else None,
                "response_id": metadata.get("response_id"),
                "returned_model": metadata.get("returned_model"),
                "sdk_version": metadata.get("sdk_version"),
            }
        )
        if result.student_visible_text:
            print(f"Skill：{result.student_visible_text}")
        if result.error:
            print(f"本轮未完成：{result.error.code}")
            if result.error.code != "boundary_stop":
                break
            if case_id == "C04-conflict":
                break
        if result.signal == "ready_for_submission":
            print("Skill 已报告自然结束；本次交互验证停止。")
            break

    if not turns:
        raise ConfigurationError("至少一次真实教学调用才构成验证记录。")
    if case_id == "C01":
        passed = turns[-1]["signal"] == "ready_for_submission"
    elif case_id == "C04-conflict":
        passed = turns[-1]["error"] == {"code": "boundary_stop", "retryable": False}
    else:
        passed = all(turn["error"] is None for turn in turns)

    _write_evidence(
        evidence_dir,
        {
            "case_id": case_id,
            "executed_at": datetime.now(UTC).isoformat(),
            "session_id": session_id,
            "skill": {"id": package.id, "version": package.version, "package_sha256": package.package_sha256},
            "runtime": {
                "provider": "openai_responses",
                "requested_model": model,
                "api_base_url": settings.api_base_url,
            },
            "inputs": {"task_input": task_input.model_dump(), "authorized_materials": authorized_materials.model_dump()},
            "status": "passed" if passed else "failed",
            "turns": turns,
        },
    )
    return 0 if passed else 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="交互式真实 SK-01 验证")
    parser.add_argument("--case", required=True, choices=sorted(CASE_INPUTS))
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.interactive:
        parser.error("真实验证必须显式提供 --interactive，脚本不会自动生成学生回应。")
    try:
        return run_interactive(args.case, args.evidence_dir, dict(os.environ))
    except ConfigurationError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
