from __future__ import annotations

import pytest

from persome.config import ModelConfig
from persome.providers import LLM_API_KEY_ENV, PROVIDERS, detected_providers, resolve_profile


def _clear_provider_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for spec in PROVIDERS:
        monkeypatch.delenv(spec.discovery_api_key_env, raising=False)
        if spec.resolved_base_url_env:
            monkeypatch.delenv(spec.resolved_base_url_env, raising=False)
    monkeypatch.delenv(LLM_API_KEY_ENV, raising=False)


def test_explicit_deepseek_profile_uses_openai_compatibility(monkeypatch) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv(LLM_API_KEY_ENV, "synthetic")
    profile = resolve_profile(
        ModelConfig(
            provider="deepseek",
            protocol="openai",
            model="deepseek-chat",
            api_key_env=LLM_API_KEY_ENV,
        )
    )
    assert profile.protocol == "openai"
    assert profile.base_url == "https://api.deepseek.com/v1"
    assert profile.api_key == "synthetic"
    assert profile.credential_ready is True


def test_legacy_config_keeps_anthropic_wire_semantics(monkeypatch) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "legacy")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example/anthropic")
    profile = resolve_profile(ModelConfig(model="deepseek-v4-flash"))
    assert profile.legacy is True
    assert profile.provider == "deepseek"
    assert profile.protocol == "anthropic"
    assert profile.api_key_env == LLM_API_KEY_ENV
    assert profile.credential_migration_required is True
    assert profile.base_url == "https://gateway.example/anthropic"


def test_openrouter_preserves_nested_model_provider_prefix(monkeypatch) -> None:
    _clear_provider_env(monkeypatch)
    profile = resolve_profile(
        ModelConfig(
            provider="openrouter",
            protocol="openai",
            model="anthropic/claude-sonnet-4",
            api_key_env=LLM_API_KEY_ENV,
        )
    )
    assert profile.wire_model == "anthropic/claude-sonnet-4"


def test_selected_provider_routing_prefix_is_removed(monkeypatch) -> None:
    _clear_provider_env(monkeypatch)
    profile = resolve_profile(
        ModelConfig(
            provider="openai",
            protocol="openai",
            model="openai/gpt-4.1-mini",
            api_key_env=LLM_API_KEY_ENV,
        )
    )
    assert profile.wire_model == "gpt-4.1-mini"


def test_explicit_provider_id_is_case_insensitive(monkeypatch) -> None:
    _clear_provider_env(monkeypatch)
    profile = resolve_profile(
        ModelConfig(
            provider="OpenAI",
            protocol="openai",
            model="openai/gpt-4.1-mini",
            api_key_env=LLM_API_KEY_ENV,
        )
    )
    assert profile.provider == "openai"
    assert profile.wire_model == "gpt-4.1-mini"


def test_local_provider_does_not_require_a_key(monkeypatch) -> None:
    _clear_provider_env(monkeypatch)
    profile = resolve_profile(
        ModelConfig(
            provider="ollama",
            protocol="openai",
            model="qwen3:8b",
            api_key_env=LLM_API_KEY_ENV,
        )
    )
    assert profile.api_key is None
    assert profile.credential_ready is True
    assert profile.client_api_key() == "persome-local"


def test_hosted_profile_fails_before_network_when_key_is_missing(monkeypatch) -> None:
    _clear_provider_env(monkeypatch)
    profile = resolve_profile(
        ModelConfig(
            provider="openai",
            protocol="openai",
            model="gpt-4.1-mini",
            api_key_env=LLM_API_KEY_ENV,
        )
    )
    with pytest.raises(RuntimeError, match=LLM_API_KEY_ENV):
        profile.client_api_key()


def test_explicit_provider_specific_key_is_a_migration_fallback(monkeypatch) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "legacy-provider-key")

    profile = resolve_profile(
        ModelConfig(
            provider="deepseek",
            protocol="openai",
            model="deepseek-chat",
            api_key_env="DEEPSEEK_API_KEY",
        )
    )

    assert profile.api_key == "legacy-provider-key"
    assert profile.api_key_env == LLM_API_KEY_ENV
    assert profile.credential_migration_required is True


def test_canonical_profile_does_not_fall_back_to_provider_key(monkeypatch) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "provider-specific-key")

    profile = resolve_profile(
        ModelConfig(
            provider="openai",
            protocol="openai",
            model="gpt-4.1-mini",
            api_key_env=LLM_API_KEY_ENV,
        )
    )

    assert profile.api_key is None
    assert profile.credential_ready is False


def test_detected_providers_keeps_region_choices_for_shared_credential(monkeypatch) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "synthetic")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
    detected = detected_providers()
    assert [spec.id for spec in detected] == ["openai", "qwen-cn", "qwen-us"]


def test_generic_key_does_not_guess_a_provider(monkeypatch) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv(LLM_API_KEY_ENV, "synthetic")

    assert detected_providers() == []
