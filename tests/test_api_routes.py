"""End-to-end tests for the REST API layer."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from persome import __version__
from persome.api import build_api_app, routes


def test_health_returns_ok(monkeypatch) -> None:
    """GET /health must return the documented envelope immediately."""
    monkeypatch.setattr(
        routes.ocr_health,
        "inspect",
        lambda capture: SimpleNamespace(enabled=True, ready=True, state="ready"),
    )
    client = TestClient(build_api_app(auth_enabled=False))
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body == {"success": True, "data": {"status": "ok", "ocr": "ready"}}


def test_health_reports_enabled_ocr_degradation(monkeypatch) -> None:
    monkeypatch.setattr(
        routes.ocr_health,
        "inspect",
        lambda capture: SimpleNamespace(
            enabled=True,
            ready=False,
            state="permission_required",
        ),
    )
    client = TestClient(build_api_app(auth_enabled=False))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "status": "degraded",
        "ocr": "permission_required",
    }


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
