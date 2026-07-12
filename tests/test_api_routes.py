"""End-to-end tests for the REST API layer."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from persome import __version__
from persome.api import build_api_app, routes


def test_health_returns_ok(monkeypatch) -> None:
    """GET /health must return the documented envelope immediately."""
    monkeypatch.setattr(
        routes.ocr_health,
        "inspect",
        lambda capture: SimpleNamespace(enabled=True, ready=True, state="ready", tier="tiny"),
    )
    monkeypatch.setattr(routes.ocr_health, "worker_state", lambda: "ready")
    client = TestClient(build_api_app(auth_enabled=False))
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "success": True,
        "data": {
            "status": "ok",
            "ocr": "ready",
            "ocr_worker": "ready",
            "ocr_enabled": True,
            "ocr_tier": "tiny",
        },
    }


def test_health_reports_enabled_ocr_degradation(monkeypatch) -> None:
    monkeypatch.setattr(
        routes.ocr_health,
        "inspect",
        lambda capture: SimpleNamespace(
            enabled=True,
            ready=False,
            state="permission_required",
            tier="tiny",
        ),
    )
    monkeypatch.setattr(routes.ocr_health, "worker_state", lambda: "not_started")
    client = TestClient(build_api_app(auth_enabled=False))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "status": "degraded",
        "ocr": "permission_required",
        "ocr_worker": "not_started",
        "ocr_enabled": True,
        "ocr_tier": "tiny",
    }


def test_health_reports_failed_daemon_ocr_worker(monkeypatch) -> None:
    monkeypatch.setattr(
        routes.ocr_health,
        "inspect",
        lambda capture: SimpleNamespace(enabled=True, ready=True, state="ready", tier="tiny"),
    )
    monkeypatch.setattr(routes.ocr_health, "worker_state", lambda: "failed")
    client = TestClient(build_api_app(auth_enabled=False))

    assert client.get("/health").json()["data"] == {
        "status": "degraded",
        "ocr": "ready",
        "ocr_worker": "failed",
        "ocr_enabled": True,
        "ocr_tier": "tiny",
    }


def test_health_reports_warming_daemon_ocr_worker_as_degraded(monkeypatch) -> None:
    monkeypatch.setattr(
        routes.ocr_health,
        "inspect",
        lambda capture: SimpleNamespace(enabled=True, ready=True, state="ready", tier="tiny"),
    )
    monkeypatch.setattr(routes.ocr_health, "worker_state", lambda: "warming")
    client = TestClient(build_api_app(auth_enabled=False))

    assert client.get("/health").json()["data"] == {
        "status": "degraded",
        "ocr": "ready",
        "ocr_worker": "warming",
        "ocr_enabled": True,
        "ocr_tier": "tiny",
    }


def test_onboarding_capture_returns_exact_daemon_receipt(tmp_path, monkeypatch) -> None:
    capture = tmp_path / "fresh.json"
    monkeypatch.setattr(routes.scheduler, "active_runner_state", lambda cfg: "ready")
    monkeypatch.setattr(routes.scheduler, "capture_now", lambda: capture)
    client = TestClient(build_api_app(auth_enabled=False))

    response = client.post("/_onboarding/capture")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "id": "fresh",
        "mode": "daemon",
        "receipt": "fresh-capture",
    }


def test_onboarding_capture_reports_runner_not_ready(monkeypatch) -> None:
    monkeypatch.setattr(routes.scheduler, "active_runner_state", lambda cfg: "not-ready")
    monkeypatch.setattr(routes.scheduler, "capture_now", lambda: None)
    client = TestClient(build_api_app(auth_enabled=False))

    response = client.post("/_onboarding/capture")

    assert response.status_code == 503


def test_onboarding_capture_reports_ingest_readiness_without_fake_record(monkeypatch) -> None:
    monkeypatch.setattr(routes.scheduler, "active_runner_state", lambda cfg: "ingest-ready")
    monkeypatch.setattr(
        routes.scheduler,
        "capture_now",
        lambda: pytest.fail("ingest readiness must not synthesize a capture"),
    )
    client = TestClient(build_api_app(auth_enabled=False))

    response = client.post("/_onboarding/capture")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "id": None,
        "mode": "ingest",
        "receipt": "ingest-ready",
    }


@pytest.mark.parametrize(("state", "status"), [("paused", 409), ("locked", 423)])
def test_onboarding_capture_preserves_privacy_gate(state, status, monkeypatch) -> None:
    monkeypatch.setattr(routes.scheduler, "active_runner_state", lambda cfg: state)
    monkeypatch.setattr(
        routes.scheduler,
        "capture_now",
        lambda: pytest.fail("privacy-gated onboarding must not capture"),
    )
    client = TestClient(build_api_app(auth_enabled=False))

    assert client.post("/_onboarding/capture").status_code == status


def test_openapi_reports_runtime_version() -> None:
    client = TestClient(build_api_app(auth_enabled=False))
    assert client.get("/openapi.json").json()["info"]["version"] == __version__


def test_model_routes_are_public_and_local(ac_root) -> None:
    client = TestClient(build_api_app(auth_enabled=False))
    page = client.get("/model")
    graph = client.get("/model/graph")
    asset = client.get("/model/assets/three.module.js")

    assert page.status_code == 200
    assert graph.status_code == 200
    assert graph.json()["model"]["schema_version"] == 1
    assert asset.status_code == 200
    assert len(asset.content) > 1_000_000


def test_removed_product_and_admin_routes_are_absent(ac_root) -> None:
    client = TestClient(build_api_app(auth_enabled=False))
    for path in (
        "/memories",
        "/search?query=test",
        "/activity",
        "/captures/current",
        "/timeline",
        "/attention/trajectory",
        "/rewind/day?date=2026-07-10",
        "/config/raw",
        "/daemon/pause",
        "/indices/rebuild",
        "/events/stream",
        "/consolidate",
    ):
        assert client.get(path).status_code == 404, path
