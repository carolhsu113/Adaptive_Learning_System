"""Choose an actually reachable DeepSeek network route without sending a key."""
from __future__ import annotations

import httpx

from app.main import create_client


def test_unreachable_routes_keep_local_app_available_without_claiming_connection(monkeypatch, tmp_path, caplog):
    from fastapi.testclient import TestClient
    from app.main import build_app
    def unavailable(self, url, **kwargs):
        raise httpx.ConnectTimeout("unreachable")
    monkeypatch.setattr(httpx.Client, "post", unavailable)
    monkeypatch.setenv("ALS_API_KEY", "test-key")
    monkeypatch.setenv("ALS_MODEL", "deepseek-flash")
    monkeypatch.setenv("ALS_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("ALS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ALS_USE_SYSTEM_PROXY", raising=False)
    with TestClient(build_app()) as app:
        assert app.get("/").status_code == 200
    assert "network check passed" not in caplog.text
    assert "unavailable" in caplog.text


def test_auto_route_falls_back_to_system_network_after_direct_tls_timeout(monkeypatch):
    routes = []
    captured = {}

    def probe(self, url, **kwargs):
        routes.append(self._trust_env)
        assert url == "https://api.deepseek.com/responses"
        assert kwargs["json"] == {}
        if not self._trust_env:
            raise httpx.ConnectTimeout("TLS handshake timed out")
        return httpx.Response(401, request=httpx.Request("GET", url))

    class CapturingClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(httpx.Client, "post", probe)
    monkeypatch.setattr("app.main.OpenAI", CapturingClient)
    create_client("test-key", "https://api.deepseek.com", use_system_proxy=None)

    assert routes == [False, True]
    assert captured["http_client"]._trust_env is True
    captured["http_client"].close()


def test_auto_route_keeps_direct_when_it_is_reachable(monkeypatch):
    routes = []
    captured = {}

    def probe(self, url, **kwargs):
        routes.append(self._trust_env)
        return httpx.Response(401, request=httpx.Request("GET", url))

    class CapturingClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(httpx.Client, "post", probe)
    monkeypatch.setattr("app.main.OpenAI", CapturingClient)
    create_client("test-key", "https://api.deepseek.com", use_system_proxy=None)

    assert routes == [False]
    assert captured["http_client"]._trust_env is False
    captured["http_client"].close()
