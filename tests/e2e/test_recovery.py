"""Recovery evidence using two distinct HTTP service processes."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx


def _port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _start(root: Path, data_dir: Path, port: int) -> subprocess.Popen:
    environment = dict(os.environ)
    environment["ALS_TEST_DATA_DIR"] = str(data_dir)
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "tests.fixtures.service_app:build_test_app", "--factory", "--host", "127.0.0.1", "--port", str(port), "--log-level", "error"],
        cwd=root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    origin = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"test service exited: {process.stdout.read()}")
        try:
            if httpx.get(origin + "/", timeout=.5).status_code == 200:
                return process
        except httpx.TransportError:
            pass
        time.sleep(.05)
    process.terminate()
    process.wait(timeout=5)
    raise AssertionError("test service did not start")


def _stop(process: subprocess.Popen) -> None:
    process.terminate()
    process.wait(timeout=5)
    if process.stdout:
        process.stdout.close()


def test_real_service_process_restart_recovers_saved_student_projection(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    port = _port()
    origin = f"http://127.0.0.1:{port}"
    headers = {"Origin": origin}
    first = _start(root, tmp_path, port)
    try:
        with httpx.Client(base_url=origin, timeout=3) as client:
            created = client.post("/api/sessions", json={"request_id": "9c51a94d-c759-41f5-8f0a-4bd0f196559d"}, headers=headers).json()
            session_id = created["session_id"]
            saved = client.put(f"/api/sessions/{session_id}/draft", json={
                "request_id": "2ab5db66-d9eb-46c1-9c05-1fa8bc903ad1", "expected_revision": created["revision"],
                "task_input": {"concern": "这一步为什么错？"},
            }, headers=headers).json()
            assert saved["task_input"]["concern"] == "这一步为什么错？"
    finally:
        _stop(first)

    second = _start(root, tmp_path, port)
    try:
        with httpx.Client(base_url=origin, timeout=3) as client:
            recovered = client.get(f"/api/sessions/{session_id}")
            assert recovered.status_code == 200
            assert recovered.json()["session_id"] == session_id
            assert recovered.json()["task_input"]["concern"] == "这一步为什么错？"
            assert recovered.json()["revision"] == saved["revision"]
    finally:
        _stop(second)
