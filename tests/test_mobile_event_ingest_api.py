"""Mobile companion observations converge on the canonical capture pipeline."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from persome import paths
from persome.api import build_api_app
from persome.api import routes as routes_mod
from persome.config import load as load_config


def _client(*, pause_on_lock: bool = False) -> TestClient:
    cfg = load_config()
    cfg.capture.pause_on_lock = pause_on_lock
    routes_mod.set_config(cfg)
    return TestClient(build_api_app(cfg, auth_enabled=False))


def _event() -> dict:
    return {
        "schema_version": 1,
        "event_id": "share-01JZTEST",
        "captured_at": "2026-07-15T03:30:00+08:00",
        "device": {"id": "iphone-cecilia", "platform": "ios", "name": "iPhone"},
        "kind": "share",
        "source_app": "Safari",
        "title": "Personal models on mobile",
        "text": "A useful article about local-first personal context.",
        "url": "https://example.test/personal-models",
        "note": "Connect this to tonight's Persome work.",
        "sensitivity": "private",
    }


def test_mobile_event_is_searchable_capture_with_provenance(ac_root) -> None:
    response = _client().post("/mobile/events/ingest", json=_event())

    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["source"] == "mobile"
    assert result["id"]

    captures = list(paths.capture_buffer_dir().glob("*.json"))
    assert len(captures) == 1
    capture = json.loads(captures[0].read_text())
    assert capture["capture_source"] == "mobile"
    assert capture["url"] == "https://example.test/personal-models"
    assert "local-first personal context" in capture["visible_text"]
    assert "Connect this to tonight's Persome work" in capture["visible_text"]
    assert capture["mobile_event"] == {
        "schema_version": 1,
        "event_id": "share-01JZTEST",
        "kind": "share",
        "device": {"id": "iphone-cecilia", "platform": "ios", "name": "iPhone"},
        "source_app": "Safari",
        "sensitivity": "private",
        "owner_initiated": True,
    }


def test_mobile_event_requires_meaningful_content(ac_root) -> None:
    payload = _event()
    for key in ("title", "text", "url", "note"):
        payload.pop(key)

    response = _client().post("/mobile/events/ingest", json=payload)

    assert response.status_code == 422


def test_mobile_event_rejects_unknown_kind(ac_root) -> None:
    payload = _event()
    payload["kind"] = "screen_spy"

    response = _client().post("/mobile/events/ingest", json=payload)

    assert response.status_code == 422


def test_owner_initiated_mobile_event_is_accepted_while_mac_is_locked(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persome.capture import scheduler

    monkeypatch.setattr(scheduler.screen_state, "is_screen_locked", lambda: True)

    response = _client(pause_on_lock=True).post("/mobile/events/ingest", json=_event())

    assert response.status_code == 200
    assert response.json()["data"]["skipped"] is False


def test_owner_pause_still_blocks_mobile_event(ac_root) -> None:
    paths.paused_flag().touch()

    response = _client().post("/mobile/events/ingest", json=_event())

    assert response.status_code == 200
    assert response.json()["data"]["skipped"] is True
