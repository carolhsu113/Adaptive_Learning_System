from __future__ import annotations

from pathlib import Path
import json
import os
import shutil
import subprocess
import sys

import pytest

from app.contracts import SkillIdentity, TaskInput
from app.core.records import SessionStore, create_session
from scripts.check_config import check_configuration
from scripts.check_snapshot import check_snapshots


def test_deepseek_launcher_checks_configuration_without_exposing_key() -> None:
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("PowerShell is not available")
    root = Path(__file__).resolve().parent.parent
    environment = os.environ.copy()
    environment.update({"ALS_API_KEY": "private-test-key", "ALS_PYTHON": sys.executable})
    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(root / "scripts" / "start_deepseek.ps1"), "-CheckOnly"],
        cwd=root, env=environment, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "SK-01" in result.stdout
    assert "private-test-key" not in result.stdout + result.stderr


def test_deepseek_cmd_launcher_works_from_restricted_powershell() -> None:
    shell = shutil.which("powershell")
    if not shell:
        pytest.skip("Windows PowerShell is not available")
    root = Path(__file__).resolve().parent.parent
    environment = os.environ.copy()
    environment.update({"ALS_API_KEY": "private-test-key", "ALS_PYTHON": sys.executable})
    launcher = root / "scripts" / "start_deepseek.cmd"
    result = subprocess.run(
        [shell, "-NoProfile", "-ExecutionPolicy", "Restricted", "-Command", f"& '{launcher}' -CheckOnly"],
        cwd=root, env=environment, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SK-01" in result.stdout
    assert "private-test-key" not in result.stdout + result.stderr


def test_config_check_requires_a_key_and_model_without_printing_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.example.toml").write_text('host = "127.0.0.1"\n[runtime]\nmodel = ""\n', encoding="utf-8")
    monkeypatch.delenv("ALS_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ALS_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ALS_MODEL"):
        check_configuration(tmp_path, {})
    with pytest.raises(ValueError, match="ALS_API_KEY"):
        check_configuration(tmp_path, {"ALS_MODEL": "model-a"})


def test_config_check_accepts_the_provider_neutral_key_name(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.example.toml").write_text(
        'host = "127.0.0.1"\n[runtime]\nmodel = "model-a"\n', encoding="utf-8"
    )
    skill = tmp_path / "skills" / "socratic-inquiry"
    (skill / "references").mkdir(parents=True)
    for relative in ("SKILL.md", "references/teaching-flow.md", "references/visibility.md"):
        (skill / relative).write_text("locked content", encoding="utf-8")
    (tmp_path / "config" / "skill_registry.json").write_text(
        json.dumps({"skills": [{
            "skill_id": "SK-01", "version": "2.0", "entry_path": "skills/socratic-inquiry/SKILL.md",
            "reference_paths": ["skills/socratic-inquiry/references/teaching-flow.md", "skills/socratic-inquiry/references/visibility.md"],
            "package_sha256": "0" * 64,
        }]}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="package_sha256"):
        check_configuration(tmp_path, {"ALS_API_KEY": "secret", "ALS_MODEL": "model-a"})


def test_config_check_rejects_changed_skill_hash(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.local.toml").write_text('host = "127.0.0.1"\n[runtime]\nmodel = "model-a"\n', encoding="utf-8")
    skill = tmp_path / "skills" / "socratic-inquiry"
    (skill / "references").mkdir(parents=True)
    for relative in ("SKILL.md", "references/teaching-flow.md", "references/visibility.md"):
        (skill / relative).write_text("locked content", encoding="utf-8")
    registry = {"skills": [{
        "skill_id": "SK-01", "version": "2.0", "entry_path": "skills/socratic-inquiry/SKILL.md",
        "reference_paths": ["skills/socratic-inquiry/references/teaching-flow.md", "skills/socratic-inquiry/references/visibility.md"],
        "package_sha256": "0" * 64,
    }]}
    (config / "skill_registry.json").write_text(json.dumps(registry), encoding="utf-8")
    with pytest.raises(ValueError, match="package_sha256"):
        check_configuration(tmp_path, {"OPENAI_API_KEY": "secret", "ALS_MODEL": "model-a"})


def test_config_check_rejects_unwritable_data_target(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.example.toml").write_text('host = "127.0.0.1"\n[runtime]\nmodel = "model-a"\n', encoding="utf-8")
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("occupied", encoding="utf-8")
    with pytest.raises(ValueError, match="data directory"):
        check_configuration(tmp_path, {"OPENAI_API_KEY": "secret", "ALS_DATA_DIR": str(blocked)})


def test_snapshot_check_requires_one_valid_snapshot_and_original_answer(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="snapshot"):
        check_snapshots(tmp_path)

    store = SessionStore(tmp_path)
    skill = SkillIdentity(id="SK-01", version="2.0", package_sha256="a" * 64)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", skill)
    session.task_input = TaskInput(concern="哪里错了？", original_answer="x = 14/3")
    session.state = "final_answer"
    saved = store.save_session(session, 0)
    store.submit_session(saved.session_id, "2ab5db66-d9eb-46c1-9c05-1fa8bc903ad1", "x = 6", saved.revision)
    summary = check_snapshots(tmp_path)

    assert summary["checked"] == 1
    assert summary["session_ids"] == [saved.session_id]

    path = next((tmp_path / "snapshots").glob("*.json"))
    corrupted = json.loads(path.read_text(encoding="utf-8"))
    corrupted["final_answer"] = "内容被改写"
    path.write_text(json.dumps(corrupted), encoding="utf-8")
    with pytest.raises(ValueError, match="final answer"):
        check_snapshots(tmp_path)
    corrupted["final_answer"] = "x = 6"
    corrupted["events"][-1]["seq"] = 99
    path.write_text(json.dumps(corrupted), encoding="utf-8")
    with pytest.raises(ValueError, match="contiguous"):
        check_snapshots(tmp_path)
