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


def _prebuilt_capture(*, timestamp: str, text: str) -> dict:
    return {
        "timestamp": timestamp,
        "schema_version": 2,
        "trigger": {"event_type": "AXFocusedWindowChanged"},
        "window_meta": {
            "app_name": "Synthetic Editor",
            "title": "Stable window",
            "bundle_id": "com.example.editor",
        },
        "focused_element": {
            "role": "AXTextArea",
            "value": text,
            "is_editable": True,
        },
        "visible_text": text,
        "url": "",
    }


def _ingest(client: TestClient, payload: dict, *, key: str | None = None):
    return client.post(
        "/mobile/events/ingest",
        json=payload,
        headers={"Idempotency-Key": key or str(payload.get("event_id") or "")},
    )


def test_mobile_event_is_searchable_capture_with_provenance(ac_root) -> None:
    response = _ingest(_client(), _event())

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
    mobile = capture["mobile_event"]
    assert mobile["event_id"] == "share-01JZTEST"
    assert mobile["device"] == {
        "id": "iphone-cecilia",
        "platform": "ios",
        "name": "iPhone",
    }
    assert mobile["provenance"] == "owner_reported"
    assert mobile["transport"] == "paired_companion_bridge"
    assert mobile["captured_at"] == "2026-07-15T03:30:00+08:00"
    assert mobile["received_at"]
    assert len(mobile["payload_sha256"]) == 64


def test_mobile_event_requires_meaningful_content(ac_root) -> None:
    payload = _event()
    for key in ("title", "text", "url", "note"):
        payload.pop(key)

    response = _ingest(_client(), payload)

    assert response.status_code == 422


def test_mobile_event_rejects_unknown_kind(ac_root) -> None:
    payload = _event()
    payload["kind"] = "screen_spy"

    response = _ingest(_client(), payload)

    assert response.status_code == 422


def test_owner_initiated_mobile_event_is_accepted_while_mac_is_locked(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persome.capture import scheduler

    monkeypatch.setattr(scheduler.screen_state, "is_screen_locked", lambda: True)

    response = _ingest(_client(pause_on_lock=True), _event())

    assert response.status_code == 200
    assert response.json()["data"]["skipped"] is False


def test_owner_pause_still_blocks_mobile_event(ac_root) -> None:
    paths.paused_flag().touch()

    response = _ingest(_client(), _event())

    assert response.status_code == 200
    assert response.json()["data"]["skipped"] is True


def test_mobile_event_retry_is_runtime_idempotent(ac_root) -> None:
    client = _client()
    first = _ingest(client, _event())
    second = _ingest(client, _event())

    assert first.status_code == second.status_code == 200
    assert first.json()["data"]["id"] == second.json()["data"]["id"]
    assert first.json()["data"]["deduped"] is False
    assert second.json()["data"]["deduped"] is True
    assert len(list(paths.capture_buffer_dir().glob("*.json"))) == 1


def test_mobile_fallback_invalidates_stale_head_before_restart(ac_root) -> None:
    from persome.capture import scheduler

    cfg = load_config()
    cfg.capture.pause_on_lock = False
    stale = _prebuilt_capture(
        timestamp="2026-08-11T00:00:00+00:00",
        text="receipted state A",
    )
    first = scheduler._CaptureRunner(cfg.capture, provider=None)
    assert first.commit_prebuilt(stale)

    result = scheduler.ingest_mobile_event(cfg, _event())

    assert result["id"]
    with scheduler.fts_store.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM capture_content_receipts").fetchone()[0] == 0
    restarted = scheduler._CaptureRunner(cfg.capture, provider=None)
    assert restarted.commit_prebuilt({**stale, "timestamp": "2026-08-11T00:00:01+00:00"})


def test_mobile_event_retry_recovers_crash_after_capture_write(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persome.capture import scheduler

    original = scheduler._write_capture

    def write_then_crash(out, *, capture_id=None):
        original(out, capture_id=capture_id)
        raise RuntimeError("simulated process loss after durable capture")

    monkeypatch.setattr(scheduler, "_write_capture", write_then_crash)
    with pytest.raises(RuntimeError, match="simulated process loss"):
        _ingest(_client(), _event())

    monkeypatch.setattr(scheduler, "_write_capture", original)
    recovered = _ingest(_client(), _event())

    assert recovered.status_code == 200
    assert recovered.json()["data"]["deduped"] is True
    assert len(list(paths.capture_buffer_dir().glob("*.json"))) == 1


def test_mobile_event_identity_reuse_with_changed_payload_conflicts(ac_root) -> None:
    client = _client()
    assert _ingest(client, _event()).status_code == 200
    changed = _event()
    changed["text"] = "Different immutable payload"

    response = _ingest(client, changed)

    assert response.status_code == 409
    assert len(list(paths.capture_buffer_dir().glob("*.json"))) == 1


def test_distinct_mobile_ids_with_same_content_and_timestamp_are_not_deduped(ac_root) -> None:
    client = _client()
    first = _event()
    second = {**first, "event_id": "share-01JZOTHER"}

    assert _ingest(client, first).status_code == 200
    assert _ingest(client, second).status_code == 200
    assert len(list(paths.capture_buffer_dir().glob("*.json"))) == 2


def test_mobile_event_requires_matching_idempotency_header(ac_root) -> None:
    response = _ingest(_client(), _event(), key="different")

    assert response.status_code == 400


@pytest.mark.parametrize(
    "captured_at",
    ["2026-07-15T03:30:00", "2999-01-01T00:00:00+00:00"],
)
def test_mobile_event_rejects_weak_capture_timestamps(ac_root, captured_at: str) -> None:
    payload = _event()
    payload["captured_at"] = captured_at

    assert _ingest(_client(), payload).status_code == 422
