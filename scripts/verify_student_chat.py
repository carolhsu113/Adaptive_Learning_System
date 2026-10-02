"""Accept a real local student round trip; never substitute a provider or teacher message."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def verify(origin: str, evidence_dir: Path) -> int:
    record = {"executed_at": datetime.now(UTC).isoformat(), "origin": origin,
              "scenario": "synthetic John equation; actual configured provider", "status": "failed"}
    with sync_playwright() as playwright:
        chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
        browser = playwright.chromium.launch(headless=True, executable_path=str(chrome) if chrome.is_file() else None)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.set_default_timeout(120000)
        try:
            page.goto(origin)
            expect(page.get_by_role("heading", name="Draft work")).to_be_visible()
            page.get_by_role("button", name="Ask a teacher").click()
            page.wait_for_function("document.querySelector('.tutor-message.assistant') || !document.getElementById('retry-tutor').hidden")
            first_message = page.locator(".tutor-message.assistant .tutor-bubble p").first
            if first_message.count() == 0:
                raise AssertionError(page.locator("#tutor-status").inner_text())
            first = first_message.inner_text()
            record["opening"] = first
            normalized = first.lower().replace("−", "-").replace("’", "'")
            compact = re.sub(r"\s+", "", normalized)
            assert re.search(r"\b(hello|hi|welcome|good morning|good afternoon)\b", normalized), "Opening lacks greeting"
            assert "john" in normalized and re.search(r"mrs\.?\s+peabody", normalized), "Opening lacks student or handoff identity"
            assert "3(x-2)=12" in compact, "Opening lacks task acknowledgement"
            assert "3x-2=12" in compact and "14/3" in compact, "Opening lacks original attempt"
            assert re.search(r"not sure|unsure|uncertain", normalized), "Opening lacks uncertainty acknowledgement"
            assert first.count("?") == 1, "Opening must ask one main question"
            assert not re.search(r"x\s*=\s*6\b", normalized), "Opening reveals the answer"
            reply = page.get_by_label("Your reply")
            send = page.get_by_role("button", name="Send", exact=True)
            expect(reply).to_be_enabled()
            expect(send).to_be_disabled()
            student_text = "In my first step, I multiplied x by 3, but I left −2 as it was."
            reply.fill(student_text)
            expect(send).to_be_enabled()
            send.click()
            page.wait_for_function("document.querySelectorAll('.tutor-message.assistant').length >= 2 || !document.getElementById('retry-tutor').hidden")
            expect(page.locator(".tutor-message.assistant")).to_have_count(2)
            expect(page.locator(".tutor-message.student")).to_have_count(1)
            second = page.locator(".tutor-message.assistant .tutor-bubble p").nth(1).inner_text()
            record["student_reply"] = student_text
            record["follow_up"] = second
            assert second.count("?") == 1, "Follow-up must ask one main question"
            assert not re.search(r"x\s*=\s*6\b", second.lower()), "Follow-up reveals the answer"
            expect(reply).to_have_value("")
            expect(page.get_by_role("button", name="Ready to practice independently")).to_be_disabled()
            record["session_id"] = page.evaluate("localStorage.getItem('als.active_session_id')")
            record["status"] = "passed"
            print(json.dumps(record, ensure_ascii=False, indent=2))
            return 0
        except Exception as error:
            record["error"] = str(error)
            print(json.dumps(record, ensure_ascii=False, indent=2), file=sys.stderr)
            return 1
        finally:
            evidence_dir.mkdir(parents=True, exist_ok=True)
            (evidence_dir / "student-chat.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            if page.url.startswith(origin):
                page.screenshot(path=str(evidence_dir / "student-chat.png"), full_page=True)
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="http://127.0.0.1:8767")
    parser.add_argument("--evidence-dir", type=Path, required=True)
    arguments = parser.parse_args()
    if not re.fullmatch(r"http://(127\.0\.0\.1|localhost):\d+/?", arguments.origin):
        parser.error("Use the local ALS service address")
    raise SystemExit(verify(arguments.origin.rstrip("/"), arguments.evidence_dir))
