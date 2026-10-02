from __future__ import annotations

from importlib.metadata import version
from threading import Lock
from typing import Any
import logging
from time import monotonic

import httpx

from openai import APIConnectionError, APIStatusError, APITimeoutError

from app.contracts import Invocation, SkillPackage, RuntimeCallError
from app.agent.learning_brief import assemble_skill_prompt


RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["student_visible_text", "teaching_status", "teaching_progress"],
    "properties": {
        "student_visible_text": {"type": "string"},
        "teaching_status": {"type": "string", "enum": ["continue", "natural_end", "boundary_stop"]},
        "teaching_progress": {
            "type": "object",
            "additionalProperties": False,
            "required": ["checkpoint"],
            "properties": {
                "checkpoint": {
                    "type": "string",
                    "enum": [
                        "clarifying",
                        "examining",
                        "error_cause",
                        "prevention_rule",
                        "transfer_checkpoint",
                        "finished",
                        "boundary",
                    ],
                }
            },
        },
    },
}


class OpenAIResponsesRuntime:
    """Thin adapter that submits a locked Skill package to Responses."""

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        package: SkillPackage | None = None,
        timeout_seconds: int = 90,
    ) -> None:
        self._client = client
        self._model = model
        self._package = package
        self._timeout_seconds = timeout_seconds
        self._metadata_lock = Lock()
        self._metadata: dict[str, dict[str, str | None]] = {}

    def take_metadata(self, request_id: str) -> dict[str, str | None] | None:
        with self._metadata_lock:
            return self._metadata.pop(request_id, None)

    def diagnose_connection(self, protocol: str) -> dict[str, Any]:
        """Fixed operator probes: no student context, key, or generated text returned."""
        if protocol not in {"responses", "structured_responses", "chat_completions", "models"}:
            raise ValueError("unsupported diagnostic protocol")
        started = monotonic()
        report: dict[str, Any] = {"protocol": protocol}
        timeout = httpx.Timeout(15.0, connect=6.0)
        try:
            if protocol == "models":
                response = self._client.models.list(timeout=timeout)
                report.update(state="completed", models=[item.id for item in response.data])
            elif protocol == "chat_completions":
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=[{"role": "user", "content": 'Return only this JSON: {"ok":true}'}],
                    response_format={"type": "json_object"},
                    extra_body={"thinking": {"type": "disabled"}},
                    max_tokens=128, timeout=timeout,
                )
                report["state"] = "completed" if response.choices else "empty_output"
            else:
                options: dict[str, Any] = {}
                prompt = "Reply with the word READY."
                if protocol == "structured_responses":
                    prompt = "Diagnostic only. Return student_visible_text READY, teaching_status continue, teaching_progress checkpoint clarifying."
                    options["text"] = {"format": {"type": "json_schema", "name": "sk01_result", "strict": True, "schema": RESULT_SCHEMA}}
                response = self._client.responses.create(
                    model=self._model, input=prompt, store=False,
                    reasoning={"effort": "none"}, max_output_tokens=128,
                    timeout=timeout, **options,
                )
                report["state"] = getattr(response, "status", "unknown")
        except APITimeoutError as error:
            report.update(state="timeout", transport=type(error.__cause__).__name__)
        except APIConnectionError as error:
            report.update(state="connection_error", transport=type(error.__cause__).__name__)
        except APIStatusError as error:
            report.update(state="http_error", http_status=error.status_code)
            report["content_type"] = error.response.headers.get("content-type", "")
            body = error.body if isinstance(error.body, dict) else {}
            details = body.get("error", body)
            key = getattr(self._client, "api_key", "")
            if isinstance(details, dict):
                for source, target in (("code", "provider_code"), ("message", "provider_message")):
                    value = str(details.get(source) or "")
                    if key:
                        value = value.replace(key, "[redacted]")
                    report[target] = value[:512]
            if not report.get("provider_message"):
                value = str(error.body or error.response.text or "Empty response body")
                if key:
                    value = value.replace(key, "[redacted]")
                report["provider_message"] = value[:2048]
        except Exception as error:
            report.update(state="adapter_error", error_type=type(error).__name__)
        report["elapsed_seconds"] = round(monotonic() - started, 2)
        return report

    def invoke(self, invocation: Invocation, package: SkillPackage | None = None) -> object:
        package = package or self._package
        if package is None:
            raise RuntimeError("a locked Skill package is required")
        prompt = assemble_skill_prompt(invocation, package)
        provider_options: dict[str, Any] = {}
        if str(getattr(self._client, "base_url", "")).rstrip("/") == "https://api.deepseek.com":
            provider_options["reasoning"] = {"effort": "low"}
        try:
            response = self._client.responses.create(
                model=self._model,
                input=prompt,
                store=False,
                timeout=self._timeout_seconds,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "sk01_result",
                        "strict": True,
                        "schema": RESULT_SCHEMA,
                    }
                },
                **provider_options,
            )
        except APITimeoutError as error:
            logging.getLogger(__name__).warning("SK-01 provider timeout: transport=%s", type(error.__cause__).__name__)
            raise RuntimeCallError("runtime_timeout") from None
        except APIConnectionError as error:
            logging.getLogger(__name__).warning("SK-01 provider connection failed: transport=%s", type(error.__cause__).__name__)
            raise RuntimeCallError("provider_connection") from None
        except APIStatusError as error:
            code = {
                400: "provider_configuration", 401: "provider_authentication",
                402: "provider_balance", 403: "provider_authentication",
                404: "provider_configuration", 422: "provider_configuration",
                429: "provider_rate_limit",
            }.get(error.status_code, "runtime_failure")
            logging.getLogger(__name__).warning("SK-01 provider request failed: category=%s status=%s", code, error.status_code)
            raise RuntimeCallError(code) from None
        with self._metadata_lock:
            self._metadata[invocation.request_id] = {
                "response_id": getattr(response, "id", None),
                "returned_model": getattr(response, "model", None),
                "sdk_version": version("openai"),
            }
        if getattr(response, "status", None) != "completed":
            raise RuntimeError("Responses request was not completed")
        if getattr(response, "error", None) is not None:
            raise RuntimeError("Responses request reported an error")
        for item in getattr(response, "output", []) or []:
            for content in getattr(item, "content", []) or []:
                if getattr(content, "type", None) == "refusal":
                    raise RuntimeError("Responses request returned a refusal")
        output_text = getattr(response, "output_text", None)
        if output_text is None:
            raise RuntimeError("Responses output_text is unavailable")
        return output_text
