from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.contracts import SkillPackage


REQUIRED_RELATIVE_PATHS = (
    "SKILL.md",
    "references/teaching-flow.md",
    "references/visibility.md",
)


class SkillLoadError(ValueError):
    """The registered Skill cannot be safely loaded."""


def _hash_package(root: Path) -> str:
    digest = hashlib.sha256()
    for relative_path in REQUIRED_RELATIVE_PATHS:
        file_path = root / relative_path
        if not file_path.is_file():
            raise SkillLoadError(f"required Skill file missing: {relative_path}")
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_path.read_bytes())
    return digest.hexdigest()


def load_skill_package(registry_path: Path) -> SkillPackage:
    registry_path = Path(registry_path)
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        entry = registry["skills"][0]
    except (OSError, json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
        raise SkillLoadError("invalid skill registry") from error

    application_root = registry_path.parent.parent

    def resolve_registered_path(value: object) -> Path:
        path = Path(str(value))
        return path.resolve() if path.is_absolute() else (application_root / path).resolve()

    entry_path = resolve_registered_path(entry.get("entry_path", ""))
    root = entry_path.parent
    required_references = [(root / path).resolve() for path in REQUIRED_RELATIVE_PATHS[1:]]
    if entry.get("skill_id") != "SK-01" or entry.get("version") != "2.0":
        raise SkillLoadError("unsupported Skill identity")
    registered_references = entry.get("reference_paths")
    if not isinstance(registered_references, list):
        raise SkillLoadError("required Skill paths do not match registry")
    if entry_path != (root / "SKILL.md").resolve() or [
        resolve_registered_path(path) for path in registered_references
    ] != required_references:
        raise SkillLoadError("required Skill paths do not match registry")

    actual_hash = _hash_package(root)
    if entry.get("package_sha256") != actual_hash:
        raise SkillLoadError("package_sha256 does not match Skill source")

    return SkillPackage(
        id="SK-01",
        version="2.0",
        package_sha256=actual_hash,
        root_path=str(root),
        files={path: (root / path).read_text(encoding="utf-8") for path in REQUIRED_RELATIVE_PATHS},
    )
