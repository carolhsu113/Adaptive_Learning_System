from __future__ import annotations


def structured_result(text: str, status: str, checkpoint: str) -> dict[str, object]:
    return {
        "student_visible_text": text,
        "teaching_status": status,
        "teaching_progress": {"checkpoint": checkpoint},
    }
