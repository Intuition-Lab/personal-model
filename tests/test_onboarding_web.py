from __future__ import annotations

import stat
from pathlib import Path

import pytest
from fastapi import HTTPException

from persome import source_import
from persome.api import routes
from persome.api.onboarding_view import render_onboarding_view


@pytest.fixture(autouse=True)
def _reset_task() -> None:
    with routes._onboarding_task_lock:
        routes._onboarding_task.update(
            stage="idle",
            message="",
            imported=0,
            unchanged=0,
            skipped=0,
            error="",
        )
        routes._onboarding_selected_paths.clear()


def test_shell_keeps_every_step_in_one_local_page() -> None:
    body = render_onboarding_view("/model/" + "x" * 43 + "/")
    assert "Set up Persome" in body
    assert 'id="steps"' in body
    assert 'id="screen"' in body
    assert 'href="assets/onboarding.css"' in body
    assert 'src="assets/onboarding.js"' in body
    assert "http://" not in body and "https://" not in body


def test_state_only_shows_detected_product_sources(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "Personal"
    vault.mkdir()
    monkeypatch.setattr(source_import, "discover_obsidian_vaults", lambda: [vault])
    monkeypatch.setattr(source_import, "count_documents", lambda root: 12)
    monkeypatch.setattr(source_import, "notion_is_installed", lambda: False)
    monkeypatch.setattr(
        routes,
        "_onboarding_permissions",
        lambda: {"accessibility": "granted", "screen_recording": "granted"},
    )

    data = routes.onboarding_state().data

    assert [item["type"] for item in data["sources"]] == ["obsidian", "folder"]
    assert data["sources"][0]["label"] == "Obsidian — Personal"
    assert "12 notes" in data["sources"][0]["detail"]


def test_browser_cannot_inject_an_unselected_local_path(tmp_path: Path) -> None:
    with pytest.raises(HTTPException, match="choose this folder") as caught:
        routes.onboarding_import({"sources": [{"type": "folder", "path": str(tmp_path)}]})
    assert caught.value.status_code == 409


def test_active_task_receipt_recovers_as_resumable_after_restart(ac_root: Path) -> None:
    routes._set_onboarding_task(stage="building", message="Building…")
    receipt = ac_root / ".onboarding-state.json"
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600

    with routes._onboarding_task_lock:
        routes._onboarding_task.update(stage="idle", message="", error="")
    restored = routes._load_onboarding_task()

    assert restored["stage"] == "failed"
    assert "resume" in restored["error"]


def test_onboarding_assets_are_bundled() -> None:
    script = routes.model_asset("onboarding.js")
    css = routes.model_asset("onboarding.css")
    assert b"Bring your history" in script.body
    assert b"Your original files will not be changed" in script.body
    assert b"onboarding/state" not in script.body  # relative capability-scoped URL
    assert b".screen" in css.body
    assert script.media_type == "text/javascript"
    assert css.media_type == "text/css"
