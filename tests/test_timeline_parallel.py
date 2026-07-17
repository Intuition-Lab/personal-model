"""Tests for parallel timeline window processing (max_parallel_windows > 1)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from persome import config as config_mod
from persome import paths
from persome.store import fts
from persome.timeline import tick

_TZ = timezone(timedelta(hours=8))

_SIMPLE_PAYLOAD = json.dumps(
    {"entries": ["[TestApp] user did something"]},
    ensure_ascii=False,
)


def _stem(ts: datetime) -> str:
    return ts.isoformat().replace(":", "-").replace("+", "p")


def _write_capture(ts: datetime) -> Path:
    payload = {
        "timestamp": ts.isoformat(),
        "schema_version": 2,
        "trigger": {"event_type": "focus"},
        "window_meta": {"app_name": "TestApp", "title": "Test", "bundle_id": "com.test"},
        "focused_element": {"role": "AXTextField", "value": "hello"},
        "visible_text": "some text",
    }
    path = paths.capture_buffer_dir() / f"{_stem(ts)}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _seed_windows(base: datetime, count: int) -> None:
    """Write one capture per window for ``count`` consecutive 1-min windows."""
    for i in range(count):
        window_start = base + timedelta(minutes=i)
        _write_capture(window_start + timedelta(seconds=10))


def test_run_once_parallel_backlog(ac_root: Path, fake_llm) -> None:
    """N pending windows → N blocks produced when max_parallel_windows > 1."""
    fake_llm.set_default("timeline", _SIMPLE_PAYLOAD)

    # Seed 4 consecutive 1-min windows starting 10 min in the past.
    n = 4
    now = datetime.now().astimezone()
    base = now.replace(second=0, microsecond=0) - timedelta(minutes=n + 1)
    _seed_windows(base, n)

    # Override max_parallel_windows to 4 to exercise the parallel path.
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.timeline.max_parallel_windows = 4  # type: ignore[attr-defined]

    produced = tick._run_once(cfg)

    assert produced == n

    with fts.cursor() as conn:
        rows = conn.execute("SELECT COUNT(*) FROM timeline_blocks").fetchone()
    assert rows[0] == n


def test_run_once_sequential_with_max_parallel_1(ac_root: Path, fake_llm) -> None:
    """max_parallel_windows=1 falls back to serial path; produces same result."""
    fake_llm.set_default("timeline", _SIMPLE_PAYLOAD)

    n = 3
    now = datetime.now().astimezone()
    base = now.replace(second=0, microsecond=0) - timedelta(minutes=n + 1)
    _seed_windows(base, n)

    cfg = config_mod.load(ac_root / "config.toml")
    cfg.timeline.max_parallel_windows = 1  # type: ignore[attr-defined]

    produced = tick._run_once(cfg)

    assert produced == n


def test_parallel_gap_is_retried_and_cleanup_preserves_unabsorbed_capture(
    ac_root: Path,
    fake_llm,
    monkeypatch,
) -> None:
    fake_llm.set_default("timeline", _SIMPLE_PAYLOAD)
    current_floor = datetime(2026, 7, 17, 12, 0, tzinfo=_TZ)
    base = current_floor - timedelta(minutes=3)
    capture_times = [base + timedelta(minutes=index, seconds=10) for index in range(3)]
    paths_by_time = {timestamp: _write_capture(timestamp) for timestamp in capture_times}
    with fts.cursor() as conn:
        for timestamp in capture_times:
            fts.insert_capture(
                conn,
                id=_stem(timestamp),
                timestamp=timestamp.isoformat(),
                app_name="TestApp",
                bundle_id="com.test",
                window_title="Test",
                focused_role="AXTextField",
                focused_value="hello",
                visible_text="some text",
                url="",
            )

    cfg = config_mod.load(ac_root / "config.toml")
    cfg.timeline.cold_lookback_minutes = 3
    cfg.timeline.max_parallel_windows = 3
    cfg.capture.buffer_retention_hours = 0
    cfg.capture.buffer_max_mb = 0
    monkeypatch.setattr(tick, "_now", lambda: current_floor)

    middle_start = base + timedelta(minutes=1)
    attempts: dict[datetime, int] = {}
    original = tick.aggregator.produce_block_for_window

    def fail_middle_once(cfg, *, start, end):  # type: ignore[no-untyped-def]
        attempts[start] = attempts.get(start, 0) + 1
        if start == middle_start and attempts[start] == 1:
            raise RuntimeError("synthetic middle-window failure")
        return original(cfg, start=start, end=end)

    monkeypatch.setattr(tick.aggregator, "produce_block_for_window", fail_middle_once)

    assert tick._run_once(cfg) == 2
    for capture_path in paths_by_time.values():
        os.utime(capture_path, (0, 0))

    stats = tick._cleanup_buffer_once(cfg)
    middle_path = paths_by_time[capture_times[1]]
    assert stats["deleted"] == 1
    assert middle_path.exists()
    with fts.cursor() as conn:
        assert conn.execute(
            "SELECT 1 FROM captures WHERE id=?", (_stem(capture_times[1]),)
        ).fetchone()

    assert tick._run_once(cfg) == 1
    assert attempts[middle_start] == 2
    with fts.cursor() as conn:
        assert tick.store.has_window(
            conn,
            middle_start,
            middle_start + timedelta(minutes=1),
        )
