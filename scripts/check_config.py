"""Check local startup prerequisites without disclosing the API key."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import load_settings, resolve_api_key
from app.runtime.skill_loader import load_skill_package
from app.agent.learning_brief import LearningBrief


def check_configuration(root: Path = ROOT, environment: dict[str, str] | None = None) -> dict[str, object]:
    environment = dict(os.environ) if environment is None else environment
    settings = load_settings(root, environment)
    resolve_api_key(environment)
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=settings.data_dir):
            pass
    except OSError as error:
        raise ValueError("data directory is not writable") from error
    package = load_skill_package(settings.skill_registry)
    LearningBrief.model_validate_json((root / "config" / "learning_brief.json").read_text(encoding="utf-8"))
    return {
        "host": settings.host,
        "port": settings.port,
        "data_dir": str(settings.data_dir),
        "model": settings.model,
        "api_base_url": settings.api_base_url,
        "skill_id": package.id,
        "skill_version": package.version,
        "package_sha256": package.package_sha256,
    }


def main() -> int:
    try:
        checked = check_configuration()
    except (ValueError, OSError) as error:
        print(f"配置校验失败：{error}", file=sys.stderr)
        return 2
    print(f"配置就绪：{checked['skill_id']} v{checked['skill_version']}，模型已指定，密钥已提供。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
