"""The write boundary around `POST /model/edit`.

`/model/edit` is the first state-changing route a browser cookie can reach, so
each property that makes granting it defensible is pinned here rather than left
to hold by accident.
"""

from __future__ import annotations

import logging

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
_RETIRE = {"schema_version": 1, "kind": "face", "id": "nope", "op": "retire"}


@pytest.fixture(autouse=True)
def _reset():
    _reset_browser_auth_state_for_tests()
    yield
    _reset_browser_auth_state_for_tests()


def _viewer_client(monkeypatch) -> tuple[TestClient, str]:
    """Return a client holding a live viewer capability, and its base path."""
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, _TOKEN)
    client = TestClient(build_api_app(), headers={"host": "127.0.0.1:8742"})
    issued = client.post(BROWSER_BOOTSTRAP_PATH, headers=auth_headers())
    consumed = client.get(issued.json()["data"]["bootstrap_url"], follow_redirects=False)
    return client, consumed.headers["location"]


def test_the_viewer_capability_may_write(ac_root, monkeypatch) -> None:
    """The viewer is the owner's correction surface, so its cookie carries POST.

    Reaching the handler (400 for a missing object) rather than the auth layer
    (401) is the assertion.
    """
    client, viewer = _viewer_client(monkeypatch)
    response = client.post(viewer + "edit", json=_RETIRE)
    assert response.status_code == 400
    assert response.json()["detail"] == "unknown_object"


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_the_viewer_capability_carries_only_allowlisted_methods(
    ac_root, monkeypatch, method
) -> None:
    """The method set is a decision, not an omission."""
    client, viewer = _viewer_client(monkeypatch)
    response = client.request(method, viewer + "edit", json=_RETIRE)
    assert response.status_code == 401


@pytest.mark.parametrize("header", ["X-HTTP-Method-Override", "X-Method-Override", "_method"])
def test_method_override_headers_cannot_smuggle_a_write(ac_root, monkeypatch, header) -> None:
    client, viewer = _viewer_client(monkeypatch)
    response = client.request("PUT", viewer + "edit", headers={header: "POST"}, json=_RETIRE)
    assert response.status_code == 401


@pytest.mark.parametrize("origin", ["http://evil.com", "null"])
def test_a_foreign_origin_cannot_reach_the_write_route(ac_root, monkeypatch, origin) -> None:
    client, viewer = _viewer_client(monkeypatch)
    response = client.post(viewer + "edit", headers={"origin": origin}, json=_RETIRE)
    assert response.status_code == 403


def test_the_body_limit_applies_on_the_rewritten_viewer_path(ac_root, monkeypatch) -> None:
    """The limit is registered against `/model/edit`, but the viewer posts to
    `/model/<token>/edit`. This pins that the rewrite happens first."""
    client, viewer = _viewer_client(monkeypatch)
    oversized = {**_RETIRE, "op": "rewrite", "replacement": "a" * (200 * 1024)}
    assert client.post(viewer + "edit", json=oversized).status_code == 413
    assert client.post("/model/edit", headers=auth_headers(), json=oversized).status_code == 413


@pytest.mark.parametrize(
    "content_type",
    ["text/plain;charset=UTF-8", "application/x-www-form-urlencoded"],
)
def test_only_json_bodies_are_accepted(ac_root, monkeypatch, content_type) -> None:
    client, viewer = _viewer_client(monkeypatch)
    response = client.post(
        viewer + "edit",
        content=b'{"schema_version":1,"kind":"face","id":"nope","op":"retire"}',
        headers={"content-type": content_type},
    )
    assert response.status_code == 422


def test_an_id_cannot_forge_a_line_in_the_audit_log(ac_root, monkeypatch, caplog) -> None:
    """The route logs the target id. A newline in it would let a caller write
    arbitrary lines into a log this product treats as provenance."""
    caplog.set_level(logging.INFO)
    client, viewer = _viewer_client(monkeypatch)
    response = client.post(
        viewer + "edit",
        json={**_RETIRE, "id": "abc\nINFO forged: owner edit applied"},
    )
    assert response.status_code == 422
    assert not any("forged" in record.getMessage() for record in caplog.records)


def test_the_writer_flattens_control_characters_before_logging(ac_root, caplog) -> None:
    """Defence in depth: the CLI reaches the writer without passing the route."""
    from persome.model.edit import apply_model_edit
    from persome.store import fts

    caplog.set_level(logging.INFO)
    with fts.cursor() as conn:
        apply_model_edit(conn, kind="face", target_id="abc\nINFO forged line", op="retire")
    assert not any(
        "\n" in record.getMessage() and "forged" in record.getMessage() for record in caplog.records
    )
