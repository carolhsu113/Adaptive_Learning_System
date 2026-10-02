from __future__ import annotations

import pytest

from app.contracts import AuthorizedMaterial, AuthorizedMaterialSource, AuthorizedMaterials, SkillIdentity, TaskInput
from app.core.records import SessionStore, create_session


def _skill() -> SkillIdentity:
    return SkillIdentity(id="SK-01", version="2.0", package_sha256="a" * 64)


def test_authorized_material_is_not_reported_missing_when_its_source_is_saved(tmp_path) -> None:
    store = SessionStore(tmp_path)
    session = create_session("9c51a94d-c759-41f5-8f0a-4bd0f196559d", _skill())
    session.task_input = TaskInput(concern="哪里错了")
    session.authorized_materials = AuthorizedMaterials(
        learning_objective=AuthorizedMaterial(
            value="分配律",
            source=AuthorizedMaterialSource(kind="teacher", source_id="teacher-01", teacher_confirmation="confirmed"),
        ),
    )

    saved = store.save_session(session, 0)

    assert {item["field"] for item in saved.missing_fields} == {
        "task_input.problem", "task_input.original_answer", "task_input.attempts",
        "task_input.other_known_information", "authorized_materials.reference_answer",
    }
    assert store.get_session(saved.session_id).authorized_materials.learning_objective.value == "分配律"


def test_llm_material_source_needs_traceable_model_version_and_time() -> None:
    with pytest.raises(ValueError):
        AuthorizedMaterialSource(kind="llm", source_id="generated-01", teacher_confirmation="unconfirmed")


def test_create_request_identifier_must_be_canonical_uuid4() -> None:
    with pytest.raises(ValueError):
        create_session("not-a-uuid", _skill())
