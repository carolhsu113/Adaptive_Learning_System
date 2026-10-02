"""Fixed, offline runtime used only by the process-restart integration test."""

from __future__ import annotations

import os
from pathlib import Path

from app.api import create_app
from app.contracts import SkillIdentity


class TestRuntime:
    def invoke(self, invocation):
        return {"student_visible_text": "请检查括号。", "teaching_status": "continue", "teaching_progress": {"checkpoint": "examining"}}


def build_test_app():
    return create_app(
        data_dir=Path(os.environ["ALS_TEST_DATA_DIR"]),
        skill=SkillIdentity(id="SK-01", version="2.0", package_sha256="a" * 64),
        runtime=TestRuntime(), model="test-model",
    )
