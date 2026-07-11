"""The polled REST status route must not probe paid providers on every GET."""

from __future__ import annotations

from fastapi.testclient import TestClient

from persome.api import build_api_app, routes
from persome.config import Config
from persome.writer import llm as llm_mod


def _ok_ping(cfg, stage):  # type: ignore[no-untyped-def]
    return llm_mod.PingResult(
        stage=stage,
        model=cfg.model_for(stage).model,
        ok=True,
        latency_ms=1,
        error=None,
    )


def test_status_provider_ping_is_reused_within_ttl(monkeypatch) -> None:
    calls: list[str] = []

    def counted(cfg, stage):  # type: ignore[no-untyped-def]
        calls.append(stage)
        return _ok_ping(cfg, stage)

    routes._model_ping_cache.clear()
    monkeypatch.setattr(llm_mod, "ping_stage", counted)

    profile_a, result_a = routes._status_model_pings(Config())
    profile_b, result_b = routes._status_model_pings(Config())

    # The four default stages share one provider/model, so the first call is
    # deduplicated to one probe and the second call is served entirely from cache.
    assert calls == ["timeline"]
    assert profile_a == profile_b
    assert result_a == result_b
    assert set(result_a) == {"timeline", "reducer", "classifier", "compact"}


def test_status_provider_ping_refreshes_after_ttl(monkeypatch) -> None:
    calls: list[str] = []
    now = iter((100.0, 100.0 + routes._MODEL_PING_CACHE_TTL_SECONDS + 1.0))

    def counted(cfg, stage):  # type: ignore[no-untyped-def]
        calls.append(stage)
        return _ok_ping(cfg, stage)

    routes._model_ping_cache.clear()
    monkeypatch.setattr(llm_mod, "ping_stage", counted)
    monkeypatch.setattr(routes.time, "monotonic", lambda: next(now))

    routes._status_model_pings(Config())
    routes._status_model_pings(Config())

    assert calls == ["timeline", "timeline"]


def test_status_cache_key_never_contains_provider_secret(monkeypatch) -> None:
    routes._model_ping_cache.clear()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-value-must-not-be-retained")
    monkeypatch.setattr(llm_mod, "ping_stage", _ok_ping)

    routes._status_model_pings(Config())

    assert "secret-value-must-not-be-retained" not in repr(routes._model_ping_cache)


def test_polled_status_does_not_ping_without_explicit_check(ac_root, monkeypatch) -> None:
    calls: list[str] = []

    def counted(cfg, stage):  # type: ignore[no-untyped-def]
        calls.append(stage)
        return _ok_ping(cfg, stage)

    cfg = Config()
    routes._model_ping_cache.clear()
    routes.set_config(cfg)
    monkeypatch.setattr(llm_mod, "ping_stage", counted)
    client = TestClient(build_api_app(cfg, auth_enabled=False))
    try:
        ordinary = client.get("/status")
        calls_after_ordinary = list(calls)
        explicit = client.get("/status?check_models=true")
    finally:
        routes.set_config(None)

    assert ordinary.status_code == 200
    assert ordinary.json()["data"]["models_checked"] is False
    assert ordinary.json()["data"]["models"] == {}
    assert ordinary.json()["data"]["ocr"]["state"] == "disabled"
    assert calls_after_ordinary == []
    assert calls == ["timeline"]
    assert explicit.status_code == 200
    assert explicit.json()["data"]["models_checked"] is True
