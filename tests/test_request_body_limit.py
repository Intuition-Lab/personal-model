"""Pure-ASGI body limits cover fixed and chunked HTTP requests."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from persome.api import build_api_app
from persome.config import Config
from persome.security.body_limit import (
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    HEALTH_IMPORT_MAX_REQUEST_BODY_BYTES,
    RequestBodyLimitMiddleware,
    RequestConcurrencyLimitMiddleware,
)


def _app(max_bytes: int = 8) -> TestClient:
    async def echo(request: Request) -> JSONResponse:
        body = await request.body()
        return JSONResponse({"size": len(body)})

    app = RequestBodyLimitMiddleware(
        Starlette(routes=[Route("/echo", echo, methods=["POST"])]),
        max_bytes=max_bytes,
    )
    return TestClient(app)


def test_small_body_is_replayed_to_the_application() -> None:
    response = _app().post("/echo", content=b"12345678")
    assert response.status_code == 200
    assert response.json() == {"size": 8}


def test_declared_oversized_body_is_rejected() -> None:
    response = _app().post(
        "/echo",
        content=b"small",
        headers={"content-length": "9"},
    )
    assert response.status_code == 413
    assert response.headers["connection"] == "close"
    assert response.headers["cache-control"] == "no-store"


def test_declared_oversized_get_body_is_rejected_and_closed() -> None:
    client = TestClient(
        RequestBodyLimitMiddleware(
            Starlette(routes=[Route("/health", lambda _request: JSONResponse({"ok": True}))]),
            max_bytes=8,
        )
    )

    response = client.get("/health", headers={"content-length": "9"})

    assert response.status_code == 413
    assert response.headers["connection"] == "close"


def test_streamed_body_is_bounded_without_content_length() -> None:
    def chunks():
        yield b"12345"
        yield b"67890"

    response = _app().post("/echo", content=chunks())
    assert response.status_code == 413


def test_invalid_content_length_is_rejected() -> None:
    response = _app().post(
        "/echo",
        content=b"small",
        headers={"content-length": "not-an-integer"},
    )
    assert response.status_code == 400


def test_duplicate_content_length_is_rejected() -> None:
    response = _app().post(
        "/echo",
        content=b"small",
        headers=[("content-length", "5"), ("content-length", "5")],
    )
    assert response.status_code == 400


def test_concurrency_limit_fails_fast_and_releases_slots() -> None:
    async def ok(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    middleware = RequestConcurrencyLimitMiddleware(
        Starlette(routes=[Route("/", ok)]),
        max_concurrent=1,
    )
    client = TestClient(middleware)
    middleware._active = 1

    rejected = client.get("/")
    assert rejected.status_code == 503
    assert rejected.headers["retry-after"] == "1"
    assert rejected.headers["connection"] == "close"

    middleware._active = 0
    assert client.get("/").status_code == 200
    assert middleware._active == 0


def test_api_app_enforces_body_limit_before_json_parsing(ac_root) -> None:
    client = TestClient(build_api_app(Config(), auth_enabled=False))

    response = client.post(
        "/captures/ingest",
        content=b"{}",
        headers={"content-length": str(DEFAULT_MAX_REQUEST_BODY_BYTES + 1)},
    )

    assert response.status_code == 413


def test_api_app_enforces_stricter_health_import_body_limit(ac_root) -> None:
    client = TestClient(build_api_app(Config(), auth_enabled=False))

    response = client.post(
        "/health-events/import",
        content=b"{}",
        headers={"content-length": str(HEALTH_IMPORT_MAX_REQUEST_BODY_BYTES + 1)},
    )

    assert response.status_code == 413
