"""Startup is locked to local configuration and a single data writer."""

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.contracts import SkillIdentity
from app.main import create_client, load_settings


def test_settings_require_real_model_and_loopback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALS_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.example.toml").write_text('host = "127.0.0.1"\n[runtime]\nmodel = ""\n', encoding="utf-8")
    with pytest.raises(ValueError, match="MODEL"):
        load_settings(tmp_path)

    (tmp_path / "config" / "settings.local.toml").write_text('host = "0.0.0.0"\n[runtime]\nmodel = "model-a"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="loopback"):
        load_settings(tmp_path)


def test_settings_expose_a_custom_responses_base_url(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.example.toml").write_text(
        'host = "127.0.0.1"\n[runtime]\nmodel = "model-a"\napi_base_url = "https://api.deepseek.com"\n',
        encoding="utf-8",
    )

    settings = load_settings(tmp_path, {})

    assert settings.api_base_url == "https://api.deepseek.com"


def test_client_sends_requests_to_the_configured_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class CapturingClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("app.main.OpenAI", CapturingClient)

    create_client("test-key", "https://api.deepseek.com", use_system_proxy=False)

    assert captured["api_key"] == "test-key"
    assert captured["base_url"] == "https://api.deepseek.com"
    assert captured["max_retries"] == 0


def test_deepseek_client_does_not_inherit_a_broken_system_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class CapturingClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("app.main.OpenAI", CapturingClient)

    create_client("test-key", "https://api.deepseek.com", use_system_proxy=False)

    http_client = captured.get("http_client")
    assert isinstance(http_client, httpx.Client)
    assert http_client._trust_env is False
    http_client.close()


def test_startup_rejects_second_writer(tmp_path: Path) -> None:
    skill = SkillIdentity(id="SK-01", version="2.0", package_sha256="a" * 64)
    first = create_app(data_dir=tmp_path, skill=skill, runtime=object(), model="model-a")
    second = create_app(data_dir=tmp_path, skill=skill, runtime=object(), model="model-a")
    with TestClient(first):
        with pytest.raises(RuntimeError, match="already in use"):
            with TestClient(second):
                pass
