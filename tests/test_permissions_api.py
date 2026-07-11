"""Tests for the GET /permissions endpoint and macOS TCC probes.

The daemon is the process that reads the AX tree (via mac-ax-helper /
mac-ax-watcher and captures focused-window pixels, so /permissions reports the
daemon's own Accessibility and Screen Recording trust. This is the authoritative
signal for onboarding instead of probing from a second GUI TCC principal.
"""

from __future__ import annotations

import platform

from fastapi.testclient import TestClient

from persome.api import build_api_app
from persome.capture import ax_capture, screen_recording


def _make_client() -> TestClient:
    return TestClient(build_api_app(auth_enabled=False))


def test_permissions_reports_granted(monkeypatch) -> None:
    monkeypatch.setattr(ax_capture, "ax_trusted", lambda: True)
    monkeypatch.setattr(screen_recording, "has_screen_recording", lambda: True)
    resp = _make_client().get("/permissions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["accessibility"] == "granted"
    assert body["data"]["screen_recording"] == "granted"


def test_permissions_reports_denied(monkeypatch) -> None:
    monkeypatch.setattr(ax_capture, "ax_trusted", lambda: False)
    monkeypatch.setattr(screen_recording, "has_screen_recording", lambda: False)
    resp = _make_client().get("/permissions")
    assert resp.status_code == 200
    assert resp.json()["data"]["accessibility"] == "denied"
    assert resp.json()["data"]["screen_recording"] == "denied"


def test_ax_trusted_false_off_darwin() -> None:
    """On non-macOS hosts (the Linux CI gate) the probe is a safe False — no
    framework load, no crash."""
    if platform.system() == "Darwin":
        # On a real mac it returns a real bool; just assert the type/no-throw.
        assert isinstance(ax_capture.ax_trusted(), bool)
    else:
        assert ax_capture.ax_trusted() is False
