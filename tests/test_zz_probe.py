"""Security probes for the /model/edit browser-cookie route."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from persome.api import build_api_app
from persome.env_file import LOCAL_API_TOKEN_ENV
from persome.security.auth import (
    BROWSER_BOOTSTRAP_PATH,
    _reset_browser_auth_state_for_tests,
    auth_headers,
)

_TOKEN = "test-local-api-token-with-at-least-32-bytes"


@pytest.fixture(autouse=True)
def _reset() -> None:
    _reset_browser_auth_state_for_tests()
    yield
    _reset_browser_auth_state_for_tests()


def _viewer(monkeypatch):
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, _TOKEN)
    client = TestClient(build_api_app(), headers={"host": "127.0.0.1:8742"})
    issued = client.post(BROWSER_BOOTSTRAP_PATH, headers=auth_headers())
    url = issued.json()["data"]["bootstrap_url"]
    consumed = client.get(url, follow_redirects=False)
    viewer_url = consumed.headers["location"]
    return client, viewer_url


def test_probe_cookie_can_post_edit(ac_root, monkeypatch) -> None:
    client, viewer_url = _viewer(monkeypatch)
    r = client.post(
        viewer_url + "edit",
        json={"schema_version": 1, "kind": "face", "id": "nope", "op": "retire"},
    )
    print("COOKIE POST edit ->", r.status_code, r.text[:200])

    # trailing slash variant
    r2 = client.post(
        viewer_url + "edit/",
        json={"schema_version": 1, "kind": "face", "id": "nope", "op": "retire"},
        follow_redirects=False,
    )
    print("COOKIE POST edit/ ->", r2.status_code, dict(r2.headers).get("location"))


def test_probe_body_limit_on_rewritten_path(ac_root, monkeypatch) -> None:
    client, viewer_url = _viewer(monkeypatch)
    big = "a" * (200 * 1024)
    r = client.post(
        viewer_url + "edit",
        json={"schema_version": 1, "kind": "face", "id": "nope", "op": "rewrite",
              "replacement": big},
    )
    print("BIG body via viewer path ->", r.status_code, r.text[:200])

    r2 = client.post(
        "/model/edit",
        headers=auth_headers(),
        json={"schema_version": 1, "kind": "face", "id": "nope", "op": "rewrite",
              "replacement": big},
    )
    print("BIG body via bearer path ->", r2.status_code, r2.text[:200])


def test_probe_origin_and_method(ac_root, monkeypatch) -> None:
    client, viewer_url = _viewer(monkeypatch)
    for origin in ["http://evil.com", "null", "http://localhost:3000", "http://127.0.0.1:3000"]:
        r = client.post(
            viewer_url + "edit",
            headers={"origin": origin},
            json={"schema_version": 1, "kind": "face", "id": "nope", "op": "retire"},
        )
        print(f"ORIGIN {origin} ->", r.status_code, r.text[:120])
    for hdr in ["X-HTTP-Method-Override", "X-Method-Override", "_method"]:
        r = client.request(
            "PUT",
            viewer_url + "edit",
            headers={hdr: "POST"},
            json={"schema_version": 1, "kind": "face", "id": "nope", "op": "retire"},
        )
        print(f"OVERRIDE {hdr} PUT ->", r.status_code)


def test_probe_id_bounds_and_logging(ac_root, monkeypatch, caplog) -> None:
    import logging

    caplog.set_level(logging.INFO)
    client, viewer_url = _viewer(monkeypatch)
    r = client.post(
        viewer_url + "edit",
        json={
            "schema_version": 1,
            "kind": "face",
            "id": "x" * 512 + "\nFORGED LOG LINE injected",
            "op": "retire",
        },
    )
    print("LONG ID ->", r.status_code, r.text[:200])
    r2 = client.post(
        viewer_url + "edit",
        json={
            "schema_version": 1,
            "kind": "face",
            "id": "abc\nINFO forged: owner edit applied",
            "op": "retire",
        },
    )
    print("NEWLINE ID ->", r2.status_code, r2.text[:200])
    print("LOG RECORDS:", [rec.getMessage() for rec in caplog.records][-6:])


def test_probe_content_type_and_extra(ac_root, monkeypatch) -> None:
    client, viewer_url = _viewer(monkeypatch)
    body = b'{"schema_version":1,"kind":"face","id":"nope","op":"retire"}'
    r = client.post(
        viewer_url + "edit",
        content=body,
        headers={"content-type": "text/plain;charset=UTF-8"},
    )
    print("TEXT/PLAIN CT ->", r.status_code, r.text[:200])
    r2 = client.post(
        viewer_url + "edit",
        content=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    print("FORM CT ->", r2.status_code, r2.text[:200])
