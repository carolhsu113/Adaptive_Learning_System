"""Local-only production entry point: uvicorn app.main:build_app --factory."""

from __future__ import annotations

import os
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

import httpx
from openai import OpenAI

from app.api import create_app
from app.contracts import SkillIdentity
from app.runtime.openai_responses import OpenAIResponsesRuntime
from app.runtime.skill_loader import load_skill_package


ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    data_dir: Path
    skill_registry: Path
    model: str
    api_base_url: str
    timeout_seconds: int


def _within_root(root: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def resolve_api_key(environment: dict[str, str]) -> str:
    """Return the provider-neutral key, accepting the original name for migration."""
    api_key = environment.get("ALS_API_KEY") or environment.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("ALS_API_KEY is required")
    return api_key


def _working_deepseek_route(api_base_url: str) -> bool | None:
    """Return a verified route, or None when the provider is unavailable."""
    for trust_env in (False, True):
        try:
            with httpx.Client(trust_env=trust_env) as probe:
                response = probe.post(
                    f"{api_base_url.rstrip('/')}/responses",
                    json={},
                    timeout=httpx.Timeout(10.0, connect=8.0),
                )
            if response.status_code < 500:
                logging.getLogger(__name__).warning("DeepSeek Responses network check passed: route=%s status=%s (no API Key sent)", "system" if trust_env else "direct", response.status_code)
                return trust_env
        except httpx.RequestError:
            continue
    logging.getLogger(__name__).warning("DeepSeek Responses is currently unavailable; the local app will remain available. Teacher requests may fail until the network recovers.")
    return None


def create_client(api_key: str, api_base_url: str, *, use_system_proxy: bool | None = False) -> OpenAI:
    options: dict[str, object] = {"api_key": api_key, "max_retries": 0}
    if api_base_url:
        options["base_url"] = api_base_url
    if api_base_url.rstrip("/") == "https://api.deepseek.com":
        selected_route = _working_deepseek_route(api_base_url) if use_system_proxy is None else use_system_proxy
        if selected_route is None:
            selected_route = True
        options["http_client"] = httpx.Client(trust_env=selected_route)
    return OpenAI(**options)


def load_settings(root: Path = ROOT, environment: dict[str, str] | None = None) -> Settings:
    root = Path(root).resolve()
    environment = dict(os.environ) if environment is None else environment
    path = root / "config" / "settings.local.toml"
    if path.exists():
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    else:
        with (root / "config" / "settings.example.toml").open("rb") as handle:
            raw = tomllib.load(handle)
    runtime = raw.get("runtime", {})
    host = environment.get("ALS_HOST", raw.get("host", "127.0.0.1"))
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("host must be loopback only")
    model = environment.get("ALS_MODEL", runtime.get("model", ""))
    if not isinstance(model, str) or not model.strip():
        raise ValueError("a configured ALS_MODEL is required")
    if runtime.get("provider", "openai_responses") != "openai_responses":
        raise ValueError("unsupported runtime provider")
    if runtime.get("store", False) is not False or runtime.get("max_retries", 0) != 0:
        raise ValueError("runtime storage and automatic retries must be disabled")
    api_base_url = environment.get("ALS_BASE_URL", runtime.get("api_base_url", "")).strip()
    port = int(environment.get("ALS_PORT", raw.get("port", 8767)))
    if not 1 <= port <= 65535:
        raise ValueError("invalid port")
    timeout = int(environment.get("ALS_TIMEOUT_SECONDS", runtime.get("timeout_seconds", 90)))
    if timeout <= 0:
        raise ValueError("invalid runtime timeout")
    return Settings(
        host=host,
        port=port,
        data_dir=_within_root(root, environment.get("ALS_DATA_DIR", raw.get("data_dir", "var/data"))),
        skill_registry=_within_root(root, environment.get("ALS_SKILL_REGISTRY", raw.get("skill_registry", "config/skill_registry.json"))),
        model=model,
        api_base_url=api_base_url,
        timeout_seconds=timeout,
    )


def build_app():
    environment = dict(os.environ)
    settings = load_settings(environment=environment)
    api_key = resolve_api_key(environment)
    package = load_skill_package(settings.skill_registry)
    client = create_client(
        api_key, settings.api_base_url,
        use_system_proxy=(
            None if "ALS_USE_SYSTEM_PROXY" not in environment
            else environment["ALS_USE_SYSTEM_PROXY"] == "1"
        ),
    )
    runtime = OpenAIResponsesRuntime(
        client=client, model=settings.model, package=package,
        timeout_seconds=settings.timeout_seconds,
    )
    app = create_app(
        data_dir=settings.data_dir,
        skill=SkillIdentity(id=package.id, version=package.version, package_sha256=package.package_sha256),
        runtime=runtime,
        model=settings.model,
    )
    app.state.host = settings.host
    app.state.port = settings.port
    return app


def main() -> None:
    import uvicorn

    settings = load_settings()
    uvicorn.run(build_app(), host=settings.host, port=settings.port, workers=1)


if __name__ == "__main__":
    main()
