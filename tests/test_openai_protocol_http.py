from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from persome.config import Config, ModelConfig
from persome.llm_setup import probe_profile
from persome.providers import make_profile
from persome.writer.llm import call_llm, extract_text


class _Handler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    token_limit_error: str | None = None

    def log_message(self, format: str, *args: Any) -> None:
        return None

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        self.requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": body,
            }
        )
        if self.token_limit_error == "reject_completion" and "max_completion_tokens" in body:
            self._json_error(
                "Unrecognized request argument supplied: max_completion_tokens",
            )
            return
        if self.token_limit_error == "reject_legacy" and "max_tokens" in body:
            self._json_error(
                "Unsupported parameter: 'max_tokens' is not supported with this model.",
                param="max_tokens",
                code="unsupported_parameter",
            )
            return
        if self.token_limit_error == "unrelated":
            self._json_error(
                "The requested model was not found.", param="model", code="model_not_found"
            )
            return
        if self.token_limit_error == "conflicting_unrecognized":
            self._json_error(
                "Unrecognized request argument supplied: max_completion_tokens",
                param="model",
                code="invalid_request_error",
            )
            return
        if self.token_limit_error == "verbose_unrecognized":
            self._json_error(
                "Request failed: Unrecognized request argument supplied: max_completion_tokens",
            )
            return
        self._json_completion(body)

    def _json_error(
        self, message: str, *, param: str | None = None, code: str | None = None
    ) -> None:
        payload = {
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": param,
                "code": code,
            }
        }
        raw = json.dumps(payload).encode()
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _json_completion(self, body: dict[str, Any]) -> None:
        message: dict[str, Any] = {"role": "assistant", "content": "ok"}
        finish_reason = "stop"
        if body.get("tools"):
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_probe",
                        "type": "function",
                        "function": {"name": "persome_setup_check", "arguments": "{}"},
                    }
                ],
            }
            finish_reason = "tool_calls"
        payload = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@contextmanager
def _server(*, token_limit_error: str | None = None) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    _Handler.requests = []
    _Handler.token_limit_error = token_limit_error
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/v1", _Handler.requests
    finally:
        _Handler.token_limit_error = None
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("provider", ["custom-openai", "deepseek"])
def test_compatible_writer_keeps_legacy_token_limit_parameter(monkeypatch, provider) -> None:
    monkeypatch.delenv("PERSOME_LLM_MOCK", raising=False)
    monkeypatch.setenv("PERSOME_LLM_API_KEY", "wire-secret")
    with _server() as (base_url, requests):
        cfg = Config(
            models={
                "default": ModelConfig(
                    provider=provider,
                    protocol="openai",
                    model="test-model",
                    base_url=base_url,
                    api_key_env="PERSOME_LLM_API_KEY",
                )
            }
        )

        response = call_llm(
            cfg,
            "timeline",
            messages=[{"role": "user", "content": "hello"}],
        )

    assert extract_text(response) == "ok"
    assert requests[0]["path"] == "/v1/chat/completions"
    assert requests[0]["authorization"] == "Bearer wire-secret"
    assert requests[0]["body"]["max_tokens"] == 8192
    assert "max_completion_tokens" not in requests[0]["body"]


def test_openai_writer_uses_completion_token_limit_parameter(monkeypatch) -> None:
    monkeypatch.delenv("PERSOME_LLM_MOCK", raising=False)
    monkeypatch.setenv("PERSOME_LLM_API_KEY", "wire-secret")
    with _server() as (base_url, requests):
        cfg = Config(
            models={
                "default": ModelConfig(
                    provider="OpenAI",
                    protocol="openai",
                    model="gpt-5.4",
                    base_url=base_url,
                    api_key_env="PERSOME_LLM_API_KEY",
                )
            }
        )

        response = call_llm(
            cfg,
            "timeline",
            messages=[{"role": "user", "content": "hello"}],
        )

    assert extract_text(response) == "ok"
    assert requests[0]["body"]["max_completion_tokens"] == 8192
    assert "max_tokens" not in requests[0]["body"]


def test_onboarding_probe_uses_real_openai_sdk(monkeypatch) -> None:
    monkeypatch.setenv("PERSOME_LLM_API_KEY", "wire-secret")
    with _server() as (base_url, requests):
        profile = make_profile(
            "custom-openai",
            model="test-model",
            base_url=base_url,
            api_key_env="PERSOME_LLM_API_KEY",
            api_key="wire-secret",
            protocol="openai",
        )

        result = probe_profile(profile)

    assert result.completion_ok is True
    assert result.tool_call_ok is True
    assert len(requests) == 2
    assert requests[1]["body"]["tool_choice"]["function"]["name"] == "persome_setup_check"


def test_azure_onboarding_probe_uses_completion_token_limit_parameter(monkeypatch) -> None:
    monkeypatch.setenv("PERSOME_LLM_API_KEY", "wire-secret")
    with _server() as (base_url, requests):
        profile = make_profile(
            "azure-openai",
            model="gpt-5.4",
            base_url=base_url,
            api_key_env="PERSOME_LLM_API_KEY",
            api_key="wire-secret",
            protocol="openai",
        )

        result = probe_profile(profile)

    assert result.completion_ok is True
    assert result.tool_call_ok is True
    assert [request["body"]["max_completion_tokens"] for request in requests] == [8, 512]
    assert all("max_tokens" not in request["body"] for request in requests)


@pytest.mark.parametrize("provider", ["azure-openai", "openai"])
def test_official_profile_falls_back_for_legacy_endpoint(monkeypatch, provider) -> None:
    monkeypatch.setenv("PERSOME_LLM_API_KEY", "wire-secret")
    with _server(token_limit_error="reject_completion") as (base_url, requests):
        profile = make_profile(
            provider,
            model="arbitrary-deployment-name",
            base_url=base_url,
            api_key_env="PERSOME_LLM_API_KEY",
            api_key="wire-secret",
            protocol="openai",
        )

        result = probe_profile(profile)

    assert result.completion_ok is True
    assert result.tool_call_ok is True
    assert len(requests) == 3
    assert requests[0]["body"]["max_completion_tokens"] == 8
    assert requests[1]["body"]["max_tokens"] == 8
    assert requests[2]["body"]["max_tokens"] == 512
    assert all("max_completion_tokens" not in request["body"] for request in requests[1:])


def test_compatible_profile_falls_forward_for_current_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("PERSOME_LLM_API_KEY", "wire-secret")
    with _server(token_limit_error="reject_legacy") as (base_url, requests):
        profile = make_profile(
            "custom-openai",
            model="gpt-5.4",
            base_url=base_url,
            api_key_env="PERSOME_LLM_API_KEY",
            api_key="wire-secret",
            protocol="openai",
        )

        result = probe_profile(profile)

    assert result.completion_ok is True
    assert result.tool_call_ok is True
    assert len(requests) == 3
    assert requests[0]["body"]["max_tokens"] == 8
    assert requests[1]["body"]["max_completion_tokens"] == 8
    assert requests[2]["body"]["max_completion_tokens"] == 512
    assert all("max_tokens" not in request["body"] for request in requests[1:])


@pytest.mark.parametrize(
    "token_limit_error", ["unrelated", "conflicting_unrecognized", "verbose_unrecognized"]
)
def test_token_limit_fallback_does_not_retry_ambiguous_400(monkeypatch, token_limit_error) -> None:
    monkeypatch.setenv("PERSOME_LLM_API_KEY", "wire-secret")
    with _server(token_limit_error=token_limit_error) as (base_url, requests):
        profile = make_profile(
            "azure-openai",
            model="missing-deployment",
            base_url=base_url,
            api_key_env="PERSOME_LLM_API_KEY",
            api_key="wire-secret",
            protocol="openai",
        )

        result = probe_profile(profile)

    assert result.completion_ok is False
    assert result.tool_call_ok is False
    assert len(requests) == 1
