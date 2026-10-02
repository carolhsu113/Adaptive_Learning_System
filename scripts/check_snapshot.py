"""Validate immutable submission snapshots against their source sessions."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.file_store import AtomicJsonFileStore
from app.core.records import SessionStore, _missing_fields
from app.runtime.skill_loader import load_skill_package


def check_snapshots(data_dir: Path, expected_skill_hash: str | None = None) -> dict[str, object]:
    data_dir = Path(data_dir)
    paths = sorted((data_dir / "snapshots").glob("*.json"))
    if not paths:
        raise ValueError("snapshot directory has no saved submissions")
    store = SessionStore(data_dir)
    session_ids: list[str] = []
    seen_session_ids: set[str] = set()
    for path in paths:
        snapshot = AtomicJsonFileStore.read(path)
        if snapshot["session_id"] in seen_session_ids:
            raise ValueError("duplicate snapshots for one session")
        seen_session_ids.add(str(snapshot["session_id"]))
        session = store.get_session(str(snapshot["session_id"]))
        if session.record_id != snapshot["record_id"] or session.skill.model_dump() != snapshot["skill"]:
            raise ValueError("snapshot identity differs from source session")
        if expected_skill_hash is not None and snapshot["skill"]["package_sha256"] != expected_skill_hash:
            raise ValueError("snapshot Skill hash differs from locked package")
        if session.task_input.model_dump() != snapshot["task_input"]:
            raise ValueError("snapshot task input or original answer differs from source session")
        if session.final_answer_draft != snapshot["final_answer"]:
            raise ValueError("snapshot final answer differs from source session")
        if session.events != snapshot["events"]:
            raise ValueError("snapshot events differ from source session")
        if session.runtime != snapshot["runtime"]:
            raise ValueError("snapshot runtime differs from source session")
        if session.authorized_materials.model_dump() != snapshot["authorized_materials"]:
            raise ValueError("snapshot authorized materials differ from source session")
        expected_missing = _missing_fields(session.task_input, session.authorized_materials)
        if snapshot["missing_fields"] != expected_missing:
            raise ValueError("snapshot missing-field reasons are inconsistent")
        if session.state != "completed" or session.submission.get("id") != snapshot["submission"]["id"]:
            raise ValueError("snapshot does not match a completed session")
        if session.submission.get("submitted_at") != snapshot["submission"]["submitted_at"]:
            raise ValueError("snapshot submitted time differs from source session")
        answer_hash = hashlib.sha256(str(snapshot["final_answer"]).encode("utf-8")).hexdigest()
        if not any(
            attempt["result"] == "saved" and attempt["answer_hash"] == answer_hash
            for attempt in session.submission.get("attempts", [])
        ):
            raise ValueError("snapshot final answer hash does not match saved attempt")
        session_ids.append(session.session_id)
    return {"checked": len(paths), "session_ids": session_ids}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验已保存的学生提交快照")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "var" / "data")
    args = parser.parse_args(argv)
    try:
        package = load_skill_package(ROOT / "config" / "skill_registry.json")
        result = check_snapshots(args.data_dir, package.package_sha256)
    except (ValueError, OSError) as error:
        print(f"快照校验失败：{error}", file=sys.stderr)
        return 2
    print(f"已校验 {result['checked']} 份快照。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
