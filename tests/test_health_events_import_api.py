"""Normalized wearable/health event import contract."""

from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient

from persome.api import build_api_app
from persome.store import fts, health_events


def _client() -> TestClient:
    return TestClient(build_api_app(auth_enabled=False))


def _payload() -> dict:
    return {
        "schema_version": 1,
        "events": [
            {
                "event_id": "HKQuantitySample:heart-rate:abc123",
                "source": {
                    "provider": "apple_health",
                    "device": "Apple Watch",
                    "device_id": "watch-local-id",
                },
                "metric": "heart_rate",
                "value": 72,
                "unit": "bpm",
                "started_at": "2026-07-15T09:30:00+08:00",
                "ended_at": "2026-07-15T09:30:05+08:00",
                "timezone": "Asia/Shanghai",
                "metadata": {"source_revision": "watchOS"},
            }
        ],
    }


def test_import_persists_normalized_event(ac_root) -> None:
    response = _client().post("/health-events/import", json=_payload())
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "schema_version": 1,
        "received": 1,
        "inserted": 1,
        "corrected": 0,
        "duplicates": 0,
        "deleted": 0,
    }

    with fts.cursor() as conn:
        row = conn.execute("SELECT * FROM health_events").fetchone()
    assert row["provider"] == "apple_health"
    assert row["external_id"] == "HKQuantitySample:heart-rate:abc123"
    assert row["metric"] == "heart_rate"
    assert row["value_json"] == "72.0"
    assert row["started_at"] == "2026-07-15T09:30:00+08:00"


def test_import_is_idempotent(ac_root) -> None:
    client = _client()
    assert client.post("/health-events/import", json=_payload()).status_code == 200
    response = client.post("/health-events/import", json=_payload())
    assert response.json()["data"]["inserted"] == 0
    assert response.json()["data"]["corrected"] == 0
    assert response.json()["data"]["duplicates"] == 1


def test_same_id_with_changed_content_corrects_existing_event(ac_root) -> None:
    client = _client()
    assert client.post("/health-events/import", json=_payload()).status_code == 200

    corrected = _payload()
    corrected["events"][0]["value"] = 76
    corrected["events"][0]["metadata"] = {"source_revision": "watchOS", "sync": 2}
    response = client.post("/health-events/import", json=corrected)

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "schema_version": 1,
        "received": 1,
        "inserted": 0,
        "corrected": 1,
        "duplicates": 0,
        "deleted": 0,
    }
    with fts.cursor() as conn:
        rows = conn.execute("SELECT value_json, metadata_json FROM health_events").fetchall()
    assert [(row["value_json"], row["metadata_json"]) for row in rows] == [
        ("76.0", '{"source_revision":"watchOS","sync":2}')
    ]


def test_import_rejects_naive_or_reversed_timestamps(ac_root) -> None:
    payload = _payload()
    payload["events"][0]["started_at"] = "2026-07-15T09:30:00"
    assert _client().post("/health-events/import", json=payload).status_code == 422

    payload = _payload()
    payload["events"][0]["ended_at"] = "2026-07-15T09:29:00+08:00"
    assert _client().post("/health-events/import", json=payload).status_code == 422


def test_import_rejects_empty_and_oversized_batches(ac_root) -> None:
    assert (
        _client()
        .post("/health-events/import", json={"schema_version": 1, "events": []})
        .status_code
        == 422
    )

    event = _payload()["events"][0]
    response = _client().post(
        "/health-events/import",
        json={"schema_version": 1, "events": [event] * 1001},
    )
    assert response.status_code in {400, 422}


def test_deletions_apply_in_same_transaction_before_anchor_can_advance(ac_root) -> None:
    client = _client()
    assert client.post("/health-events/import", json=_payload()).status_code == 200

    response = client.post(
        "/health-events/import",
        json={
            "schema_version": 1,
            "events": [],
            "deleted_events": [
                {
                    "provider": "apple_health",
                    "event_id": "HKQuantitySample:heart-rate:abc123",
                }
            ],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "schema_version": 1,
        "received": 0,
        "inserted": 0,
        "corrected": 0,
        "duplicates": 0,
        "deleted": 1,
    }
    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM health_events").fetchone()[0] == 0


def test_deletion_rolls_back_when_later_event_fails(ac_root) -> None:
    payload = _payload()["events"][0]
    with fts.cursor() as conn:
        health_events.import_events(conn, [payload])
        with pytest.raises(KeyError):
            health_events.import_events(
                conn,
                [{"event_id": "invalid-after-delete"}],
                [{"provider": "apple_health", "event_id": payload["event_id"]}],
            )
        assert conn.execute("SELECT COUNT(*) FROM health_events").fetchone()[0] == 1


@pytest.mark.parametrize("value", ["x" * 4097, float("nan"), float("inf"), -float("inf")])
def test_import_rejects_unbounded_or_nonfinite_values(ac_root, value) -> None:
    payload = _payload()
    payload["events"][0]["value"] = value
    body = json.dumps(payload)
    response = _client().post(
        "/health-events/import",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert response.status_code in {400, 422}


def test_import_rejects_oversized_metadata_and_combined_operations(ac_root) -> None:
    payload = _payload()
    payload["events"][0]["metadata"] = {"note": "x" * (64 * 1024)}
    assert _client().post("/health-events/import", json=payload).status_code == 422

    event = _payload()["events"][0]
    deletions = [
        {"provider": "apple_health", "event_id": f"deleted-{index}"} for index in range(1_000)
    ]
    response = _client().post(
        "/health-events/import",
        json={"schema_version": 1, "events": [event], "deleted_events": deletions},
    )
    assert response.status_code == 422


def test_mixed_deletion_and_insert_is_atomic(ac_root) -> None:
    client = _client()
    assert client.post("/health-events/import", json=_payload()).status_code == 200
    replacement = copy.deepcopy(_payload()["events"][0])
    replacement["event_id"] = "replacement"

    response = client.post(
        "/health-events/import",
        json={
            "schema_version": 1,
            "events": [replacement],
            "deleted_events": [
                {
                    "provider": "apple_health",
                    "event_id": "HKQuantitySample:heart-rate:abc123",
                }
            ],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["deleted"] == 1
    assert response.json()["data"]["inserted"] == 1
    with fts.cursor() as conn:
        ids = [row[0] for row in conn.execute("SELECT external_id FROM health_events")]
    assert ids == ["replacement"]
