"""POST /captures/ingest — Swift-side capture push into the daemon pipeline.

The Swift "Persome" process now owns OS capture (AX tree + screenshot); the daemon
becomes a zero-permission compute backend that receives pre-captured payloads via
this endpoint and runs the SAME enrich → persist → hook tail as the in-daemon
capture loop. These tests pin the contract:

  * the posted AX tree is re-enriched BYTE-COMPATIBLY with the daemon's own
    capture path (focused_element / visible_text / url),
  * the capture lands in the buffer and is readable through the production
    capture reader used by MCP,
  * a live capture runner updates session activity and content-dedups identical
    consecutive pushes.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from persome import paths
from persome.api import build_api_app
from persome.config import load as load_config


def _client(cfg=None):
    if cfg is None:
        cfg = load_config()
    # Ingest contract tests must not depend on the workstation/CI runner's real
    # screen state. Lock fail-closed behavior is covered separately.
    cfg.capture.pause_on_lock = False
    from persome.api import routes as routes_mod

    routes_mod.set_config(cfg)
    return TestClient(build_api_app(cfg, auth_enabled=False))


def _payload(*, with_screenshot: bool = False) -> tuple[dict, dict]:
    cap = {
        "timestamp": "2026-07-10T09:00:00+08:00",
        "trigger": {"event_type": "AXFocusedWindowChanged"},
        "window_meta": {
            "app_name": "Synthetic Browser",
            "title": "Runtime status",
            "bundle_id": "com.google.Chrome",
        },
        "ax_tree": {
            "apps": [
                {
                    "name": "Synthetic Browser",
                    "bundle_id": "com.google.Chrome",
                    "is_frontmost": True,
                    "windows": [
                        {
                            "title": "Runtime status",
                            "focused": True,
                            "elements": [
                                {
                                    "role": "AXTextField",
                                    "value": "https://example.com/runtime",
                                },
                                {
                                    "role": "AXWebArea",
                                    "title": "Runtime status",
                                    "children": [
                                        {
                                            "role": "AXStaticText",
                                            "value": "The local model is ready.",
                                        }
                                    ],
                                },
                            ],
                        }
                    ],
                }
            ]
        },
        "ax_metadata": {"synthetic": True},
        "screenshot": {
            "image_base64": "aGVsbG8=",
            "mime_type": "image/jpeg",
            "width": 1,
            "height": 1,
        },
    }
    payload = {
        "timestamp": cap["timestamp"],
        "trigger": cap.get("trigger") or {"event_type": "AXFocusedWindowChanged"},
        "window_meta": cap["window_meta"],
        "ax_tree": cap["ax_tree"],
        "ax_metadata": cap.get("ax_metadata", {}),
    }
    if with_screenshot:
        payload["screenshot"] = cap.get("screenshot")
    return cap, payload


def _prebuilt_capture(*, timestamp: str, text: str, title: str = "Dynamic title") -> dict:
    return {
        "timestamp": timestamp,
        "schema_version": 2,
        "trigger": {"event_type": "AXFocusedWindowChanged"},
        "window_meta": {
            "app_name": "Synthetic Editor",
            "title": title,
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


def test_ingest_writes_and_enriches_byte_compatibly(ac_root) -> None:
    from persome.capture import s1_parser

    cap, payload = _payload()
    resp = _client().post("/captures/ingest", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data.get("id")

    files = list(paths.capture_buffer_dir().glob("*.json"))
    assert len(files) == 1
    out = json.loads(files[0].read_text())

    # The ingest path must reproduce the DAEMON's own enrichment for identical
    # inputs (both paths share `_finalize_capture` → `s1_parser.enrich`). Compute
    # the reference from the same AX tree rather than the stored fixture, which is
    # a snapshot that can drift as the renderer evolves.
    ref = {"ax_tree": cap["ax_tree"], "window_meta": cap["window_meta"]}
    s1_parser.enrich(ref)
    assert out["window_meta"] == cap["window_meta"]
    assert out["visible_text"] == ref["visible_text"]
    assert out["focused_element"] == ref["focused_element"]
    assert out["url"] == ref["url"]
    assert out["capture_source"] == "ingest"


def test_ingest_readable_via_recent(ac_root) -> None:
    from persome.mcp.captures import read_recent_capture

    _, payload = _payload()
    client = _client()
    assert client.post("/captures/ingest", json=payload).status_code == 200
    rec = read_recent_capture()
    assert rec is not None
    assert rec["app_name"] == payload["window_meta"]["app_name"]


def test_ingest_filters_empty_composer_placeholder(ac_root) -> None:
    phrase = "Ask for follow-up changes"
    _, payload = _payload()
    payload["trigger"] = {
        "event_type": "UserMouseClick",
        "details": {"element": {"role": "AXStaticText", "value": phrase}},
    }
    app = payload["ax_tree"]["apps"][0]
    app["focused_element"] = {
        "role": "AXTextArea",
        "value": phrase,
        "is_editable": True,
    }
    app["windows"][0]["elements"] = [
        {
            "role": "AXTextArea",
            "value": phrase,
            "children": [
                {
                    "role": "AXGroup",
                    "domClassList": ["placeholder"],
                    "children": [{"role": "AXStaticText", "value": phrase}],
                }
            ],
        }
    ]

    response = _client().post("/captures/ingest", json=payload)

    assert response.status_code == 200, response.text
    out = json.loads(next(paths.capture_buffer_dir().glob("*.json")).read_text())
    assert out["focused_element"]["value"] == ""
    assert phrase not in out["visible_text"]
    assert out["trigger"]["details"]["element"].get("value", "") == ""


def test_ingest_through_runner_fires_session_hook_and_dedups(ac_root) -> None:
    from persome.capture import scheduler

    seen: list[dict] = []
    runner = scheduler._CaptureRunner(
        load_config().capture,
        provider=None,  # ingest never builds via the provider
        pre_capture_hook=lambda trigger: seen.append(trigger),
    )
    scheduler._set_active_runner(runner)
    try:
        _, payload = _payload()
        client = _client()
        first = client.post("/captures/ingest", json=payload).json()["data"]
        second = client.post("/captures/ingest", json=payload).json()["data"]
        # Identical content → second push is a no-op (content fingerprint dedup).
        assert first.get("id")
        assert second.get("deduped") is True
        assert len(list(paths.capture_buffer_dir().glob("*.json"))) == 1
        # The session hook fires once; a duplicate does not refresh activity.
        assert len(seen) == 1
        assert seen[0]["event_type"] == "AXFocusedWindowChanged"
    finally:
        scheduler._set_active_runner(None)


# ── Hardening (codex adversarial review findings) ────────────────────────────


def test_ingest_rejects_path_traversal_timestamp(ac_root) -> None:
    """An untrusted timestamp must not escape the capture buffer or clobber files.

    The payload timestamp flows into the capture filename; a non-ISO8601 value (here a
    path-traversal string) is replaced with a safe server timestamp, so the file lands
    INSIDE the buffer with a separator-free stem.
    """
    _, payload = _payload()
    payload["timestamp"] = "../../../etc/evil"
    resp = _client().post("/captures/ingest", json=payload)
    assert resp.status_code == 200, resp.text

    files = list(paths.capture_buffer_dir().glob("*.json"))
    assert len(files) == 1  # nothing escaped the buffer
    assert files[0].parent == paths.capture_buffer_dir()
    assert "/" not in files[0].stem
    out = json.loads(files[0].read_text())
    datetime.fromisoformat(out["timestamp"])  # replaced with a valid ISO8601 stamp


def test_ingest_honors_screenshot_optout(ac_root) -> None:
    """With [capture].include_screenshot=false the daemon must not persist a pushed image."""
    from persome.api import routes as routes_mod

    routes_mod.set_config(None)  # force _get_cfg() → load_config() (reads our config.toml)
    (ac_root / "config.toml").write_text("[capture]\ninclude_screenshot = false\n")
    try:
        _, payload = _payload(with_screenshot=True)
        assert payload.get("screenshot")  # the fixture really carries a screenshot
        resp = _client().post("/captures/ingest", json=payload)
        assert resp.status_code == 200, resp.text
        out = json.loads(next(paths.capture_buffer_dir().glob("*.json")).read_text())
        assert "screenshot" not in out
    finally:
        routes_mod.set_config(None)


def test_commit_prebuilt_propagates_write_error_not_dedup(ac_root, monkeypatch) -> None:
    """A real write failure must PROPAGATE (→ HTTP 500), never be reported as a dedup."""
    from persome.capture import scheduler

    capture_cfg = load_config().capture
    capture_cfg.pause_on_lock = False
    runner = scheduler._CaptureRunner(capture_cfg, provider=None)

    real_write = scheduler._write_capture

    def boom(_out):
        raise OSError("disk full")

    monkeypatch.setattr(scheduler, "_write_capture", boom)
    out = scheduler.build_ingest_capture(capture_cfg, _payload()[1])
    assert out is not None
    with pytest.raises(OSError):
        runner.commit_prebuilt(out)

    # A failed write must not move the in-memory head or mint a durable receipt.
    monkeypatch.setattr(scheduler, "_write_capture", real_write)
    assert runner.commit_prebuilt(out)
    with scheduler.fts_store.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM capture_content_receipts").fetchone()[0] == 1


def test_v2_fingerprint_keeps_distinct_title_context_with_semantic_content() -> None:
    from persome.capture import scheduler

    first = _prebuilt_capture(
        timestamp="2026-08-11T00:00:00+00:00",
        text="same semantic content",
        title="Build 1%",
    )
    second = _prebuilt_capture(
        timestamp="2026-08-11T00:00:01+00:00",
        text="same semantic content",
        title="Build 99%",
    )

    assert scheduler._content_fingerprint(first).startswith("v2:")
    assert scheduler._content_fingerprint(first) != scheduler._content_fingerprint(second)


def test_v2_fingerprint_keeps_distinct_title_context_for_opaque_capture() -> None:
    from persome.capture import scheduler

    first = _prebuilt_capture(
        timestamp="2026-08-11T00:00:00+00:00",
        text="",
        title="Opaque window A",
    )
    second = _prebuilt_capture(
        timestamp="2026-08-11T00:00:01+00:00",
        text="",
        title="Opaque window B",
    )

    assert scheduler._content_fingerprint(first) != scheduler._content_fingerprint(second)


def test_runner_dedups_recent_a_b_a_without_refreshing_skip(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persome.capture import scheduler

    now = [100.0]
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: now[0])
    cfg = load_config().capture
    cfg.same_window_dedup_seconds = 5.0
    runner = scheduler._CaptureRunner(cfg, provider=None)

    a1 = _prebuilt_capture(timestamp="2026-08-11T00:00:00+00:00", text="state A")
    b = _prebuilt_capture(timestamp="2026-08-11T00:00:01+00:00", text="state B")
    a2 = _prebuilt_capture(timestamp="2026-08-11T00:00:02+00:00", text="state A")
    a3 = _prebuilt_capture(timestamp="2026-08-11T00:00:03+00:00", text="state A")

    assert runner.commit_prebuilt(a1)
    now[0] = 101.0
    assert runner.commit_prebuilt(b)
    now[0] = 104.9
    assert runner.commit_prebuilt(a2) is None
    # The skipped A did not refresh its receipt; five seconds is measured from
    # the successful A at t=100, so it is admitted immediately after t=105.
    now[0] = 105.1
    assert runner.commit_prebuilt(a3)
    assert len(list(paths.capture_buffer_dir().glob("*.json"))) == 3


def test_runner_uses_configured_recent_content_horizon(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persome.capture import scheduler

    now = [100.0]
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: now[0])
    cfg = load_config().capture
    cfg.same_window_dedup_seconds = 1.5
    runner = scheduler._CaptureRunner(cfg, provider=None)

    assert runner.commit_prebuilt(
        _prebuilt_capture(timestamp="2026-08-11T00:00:00+00:00", text="state A")
    )
    now[0] = 100.5
    assert runner.commit_prebuilt(
        _prebuilt_capture(timestamp="2026-08-11T00:00:01+00:00", text="state B")
    )
    now[0] = 101.6
    assert runner.commit_prebuilt(
        _prebuilt_capture(timestamp="2026-08-11T00:00:02+00:00", text="state A")
    )


def test_runner_restores_retained_head_across_restart(ac_root) -> None:
    from persome.capture import scheduler

    cfg = load_config().capture
    first = scheduler._CaptureRunner(cfg, provider=None)
    assert first.commit_prebuilt(
        _prebuilt_capture(timestamp="2026-08-11T00:00:00+00:00", text="static state")
    )

    restarted = scheduler._CaptureRunner(cfg, provider=None)
    assert (
        restarted.commit_prebuilt(
            _prebuilt_capture(timestamp="2026-08-12T00:00:00+00:00", text="static state")
        )
        is None
    )
    assert len(list(paths.capture_buffer_dir().glob("*.json"))) == 1


def test_direct_ingest_invalidates_stale_head_before_restart(ac_root) -> None:
    from persome.capture import scheduler

    cfg = load_config()
    cfg.capture.pause_on_lock = False
    stale = _prebuilt_capture(
        timestamp="2026-08-11T00:00:00+00:00",
        text="receipted state A",
    )
    first = scheduler._CaptureRunner(cfg.capture, provider=None)
    assert first.commit_prebuilt(stale)

    _, direct_payload = _payload()
    direct = scheduler.ingest_capture(cfg, direct_payload)

    assert direct["id"]
    with scheduler.fts_store.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM capture_content_receipts").fetchone()[0] == 0
    restarted = scheduler._CaptureRunner(cfg.capture, provider=None)
    assert restarted.commit_prebuilt({**stale, "timestamp": "2026-08-11T00:00:01+00:00"})


def test_direct_ingest_write_failure_leaves_fail_open_marker(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persome.capture import scheduler

    cfg = load_config()
    cfg.capture.pause_on_lock = False
    first = scheduler._CaptureRunner(cfg.capture, provider=None)
    assert first.commit_prebuilt(
        _prebuilt_capture(
            timestamp="2026-08-11T00:00:00+00:00",
            text="receipted state A",
        )
    )

    def fail_write(_out, *, capture_id=None):
        del capture_id
        raise OSError("disk full")

    monkeypatch.setattr(scheduler, "_write_capture", fail_write)
    _, direct_payload = _payload()

    with pytest.raises(OSError, match="disk full"):
        scheduler.ingest_capture(cfg, direct_payload)
    with scheduler.fts_store.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM capture_content_receipts").fetchone()[0] == 1
    assert paths.capture_content_receipt_invalidation_marker().is_file()

    restarted = scheduler._CaptureRunner(cfg.capture, provider=None)
    assert restarted._last_fingerprint is None
    assert not paths.capture_content_receipt_invalidation_marker().exists()


def test_direct_ingest_clear_failure_retains_durable_invalidation_marker(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persome.capture import scheduler

    cfg = load_config()
    cfg.capture.pause_on_lock = False
    stale = _prebuilt_capture(
        timestamp="2026-08-11T00:00:00+00:00",
        text="receipted state A",
    )
    first = scheduler._CaptureRunner(cfg.capture, provider=None)
    assert first.commit_prebuilt(stale)

    def fail_clear(_conn) -> int:
        raise OSError("receipt database unavailable")

    real_clear = scheduler.content_receipt_store.clear
    monkeypatch.setattr(scheduler.content_receipt_store, "clear", fail_clear)
    _, direct_payload = _payload()
    direct = scheduler.ingest_capture(cfg, direct_payload)

    assert direct["id"]
    assert paths.capture_content_receipt_invalidation_marker().is_file()
    with scheduler.fts_store.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM capture_content_receipts").fetchone()[0] == 1
    monkeypatch.setattr(scheduler.content_receipt_store, "clear", real_clear)
    restarted = scheduler._CaptureRunner(cfg.capture, provider=None)
    assert not paths.capture_content_receipt_invalidation_marker().exists()
    assert restarted.commit_prebuilt({**stale, "timestamp": "2026-08-11T00:00:01+00:00"})


def test_runner_load_clear_failure_keeps_marker_and_fails_open(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persome.capture import scheduler

    cfg = load_config().capture
    seeded = scheduler._CaptureRunner(cfg, provider=None)
    assert seeded.commit_prebuilt(
        _prebuilt_capture(timestamp="2026-08-11T00:00:00+00:00", text="stale head")
    )
    marker = paths.capture_content_receipt_invalidation_marker()
    paths.atomic_write_private_text(marker, "pending\n")
    real_clear = scheduler.content_receipt_store.clear

    def fail_clear(_conn) -> int:
        raise OSError("receipt database unavailable during load")

    monkeypatch.setattr(scheduler.content_receipt_store, "clear", fail_clear)
    restarted = scheduler._CaptureRunner(cfg, provider=None)

    assert restarted._last_fingerprint is None
    assert marker.is_file()

    monkeypatch.setattr(scheduler.content_receipt_store, "clear", real_clear)
    recovered = scheduler._CaptureRunner(cfg, provider=None)
    assert recovered._last_fingerprint is None
    assert not marker.exists()


def test_runner_load_marker_cleanup_failure_retries_safe_clear(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persome.capture import scheduler

    cfg = load_config().capture
    seeded = scheduler._CaptureRunner(cfg, provider=None)
    assert seeded.commit_prebuilt(
        _prebuilt_capture(timestamp="2026-08-11T00:00:00+00:00", text="stale head")
    )
    marker = paths.capture_content_receipt_invalidation_marker()
    paths.atomic_write_private_text(marker, "pending\n")
    real_unlink = Path.unlink

    def fail_marker_unlink(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self == marker:
            raise OSError("marker filesystem unavailable")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_marker_unlink)
    restarted = scheduler._CaptureRunner(cfg, provider=None)

    assert restarted._last_fingerprint is None
    assert marker.is_file()

    monkeypatch.setattr(Path, "unlink", real_unlink)
    recovered = scheduler._CaptureRunner(cfg, provider=None)
    assert recovered._last_fingerprint is None
    assert not marker.exists()


def test_direct_ingest_marker_cleanup_failure_remains_fail_open(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persome.capture import scheduler

    cfg = load_config()
    cfg.capture.pause_on_lock = False
    stale = _prebuilt_capture(
        timestamp="2026-08-11T00:00:00+00:00",
        text="receipted state A",
    )
    seeded = scheduler._CaptureRunner(cfg.capture, provider=None)
    assert seeded.commit_prebuilt(stale)
    marker = paths.capture_content_receipt_invalidation_marker()
    real_unlink = Path.unlink

    def fail_marker_unlink(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self == marker:
            raise OSError("marker filesystem unavailable")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_marker_unlink)
    _, direct_payload = _payload()
    assert scheduler.ingest_capture(cfg, direct_payload)["id"]

    with scheduler.fts_store.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM capture_content_receipts").fetchone()[0] == 0
    assert marker.is_file()

    monkeypatch.setattr(Path, "unlink", real_unlink)
    restarted = scheduler._CaptureRunner(cfg.capture, provider=None)
    assert not marker.exists()
    assert restarted.commit_prebuilt({**stale, "timestamp": "2026-08-11T00:00:01+00:00"})


def test_capture_once_invalidates_stale_head_before_restart(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persome.capture import scheduler

    cfg = load_config().capture
    stale = _prebuilt_capture(
        timestamp="2026-08-11T00:00:00+00:00",
        text="receipted state A",
    )
    first = scheduler._CaptureRunner(cfg, provider=None)
    assert first.commit_prebuilt(stale)
    direct = _prebuilt_capture(
        timestamp="2026-08-11T00:00:01+00:00",
        text="direct state B",
    )
    monkeypatch.setattr(scheduler, "_build_capture", lambda *_args, **_kwargs: direct)

    assert scheduler.capture_once(cfg, provider=None) is not None

    with scheduler.fts_store.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM capture_content_receipts").fetchone()[0] == 0
    restarted = scheduler._CaptureRunner(cfg, provider=None)
    assert restarted.commit_prebuilt({**stale, "timestamp": "2026-08-11T00:00:02+00:00"})


def test_missing_newest_receipt_backing_does_not_promote_older_head(ac_root) -> None:
    from persome.capture import scheduler

    cfg = load_config().capture
    first = scheduler._CaptureRunner(cfg, provider=None)
    state_a = _prebuilt_capture(
        timestamp="2026-08-11T00:00:00+00:00",
        text="state A",
    )
    state_b = _prebuilt_capture(
        timestamp="2026-08-11T00:00:01+00:00",
        text="state B",
    )
    assert first.commit_prebuilt(state_a)
    newest_id = first.commit_prebuilt(state_b)
    assert newest_id is not None
    (paths.capture_buffer_dir() / f"{newest_id}.json").unlink()

    restarted = scheduler._CaptureRunner(cfg, provider=None)

    assert restarted.commit_prebuilt({**state_a, "timestamp": "2026-08-11T00:00:02+00:00"})


def test_runner_activation_reloads_after_interleaved_direct_write(ac_root) -> None:
    from persome.capture import scheduler

    cfg = load_config()
    cfg.capture.pause_on_lock = False
    state_a = _prebuilt_capture(
        timestamp="2026-08-11T00:00:00+00:00",
        text="state A",
    )
    seed = scheduler._CaptureRunner(cfg.capture, provider=None)
    assert seed.commit_prebuilt(state_a)

    constructed_before_direct_write = scheduler._CaptureRunner(cfg.capture, provider=None)
    _, direct_payload = _payload()
    assert scheduler.ingest_capture(cfg, direct_payload)["id"]

    scheduler._set_active_runner(constructed_before_direct_write)
    try:
        assert constructed_before_direct_write.commit_prebuilt(
            {**state_a, "timestamp": "2026-08-11T00:00:01+00:00"}
        )
    finally:
        scheduler._set_active_runner(None)


def test_receipt_failure_keeps_in_process_head_but_fails_open_after_restart(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persome.capture import scheduler

    def receipt_failure(*_args, **_kwargs):
        raise OSError("receipt database unavailable")

    monkeypatch.setattr(scheduler.content_receipt_store, "record_success", receipt_failure)
    cfg = load_config().capture
    runner = scheduler._CaptureRunner(cfg, provider=None)
    assert runner.commit_prebuilt(
        _prebuilt_capture(timestamp="2026-08-11T00:00:00+00:00", text="static state")
    )
    assert (
        runner.commit_prebuilt(
            _prebuilt_capture(timestamp="2026-08-11T00:00:01+00:00", text="static state")
        )
        is None
    )

    restarted = scheduler._CaptureRunner(cfg, provider=None)
    assert restarted.commit_prebuilt(
        _prebuilt_capture(timestamp="2026-08-11T00:00:02+00:00", text="static state")
    )
    assert len(list(paths.capture_buffer_dir().glob("*.json"))) == 2


def test_recent_a_b_a_uses_monotonic_horizon_when_wall_clock_moves_backward(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persome.capture import scheduler

    wall_now = [100.0]
    monotonic_now = [100.0]
    monkeypatch.setattr(scheduler.time, "time", lambda: wall_now[0])
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: monotonic_now[0])
    cfg = load_config().capture
    cfg.same_window_dedup_seconds = 5.0
    runner = scheduler._CaptureRunner(cfg, provider=None)
    state_a = _prebuilt_capture(
        timestamp="2026-08-11T00:00:00+00:00",
        text="state A",
    )
    state_b = _prebuilt_capture(
        timestamp="2026-08-11T00:00:01+00:00",
        text="state B",
    )

    assert runner.commit_prebuilt(state_a)
    wall_now[0] = 101.0
    monotonic_now[0] = 101.0
    assert runner.commit_prebuilt(state_b)
    wall_now[0] = 99.0
    monotonic_now[0] = 102.0

    assert runner.commit_prebuilt({**state_a, "timestamp": "2026-08-11T00:00:02+00:00"}) is None


def test_force_success_becomes_content_head(ac_root) -> None:
    from persome.capture import scheduler

    runner = scheduler._CaptureRunner(load_config().capture, provider=None)
    assert runner.commit_prebuilt(
        _prebuilt_capture(timestamp="2026-08-11T00:00:00+00:00", text="state A")
    )
    assert runner.commit_prebuilt(
        _prebuilt_capture(timestamp="2026-08-11T00:00:01+00:00", text="state B")
    )
    assert runner.commit_prebuilt(
        _prebuilt_capture(timestamp="2026-08-11T00:00:02+00:00", text="state A"),
        force=True,
    )
    assert (
        runner.commit_prebuilt(
            _prebuilt_capture(timestamp="2026-08-11T00:00:03+00:00", text="state A")
        )
        is None
    )


def test_runner_capture_and_receipt_share_maintenance_boundary(ac_root) -> None:
    from persome.capture import scheduler

    runner = scheduler._CaptureRunner(load_config().capture, provider=None)
    started = threading.Event()
    finished = threading.Event()

    def commit() -> None:
        started.set()
        runner.commit_prebuilt(
            _prebuilt_capture(timestamp="2026-08-11T00:00:00+00:00", text="atomic state")
        )
        finished.set()

    with scheduler.fts_store.exclusive_database_maintenance():
        worker = threading.Thread(target=commit)
        worker.start()
        assert started.wait(1)
        assert not finished.wait(0.1)

    worker.join(timeout=5)
    assert finished.is_set()
    with scheduler.fts_store.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM capture_content_receipts").fetchone()[0] == 1
