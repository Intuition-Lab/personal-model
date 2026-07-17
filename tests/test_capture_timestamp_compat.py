"""Upgrade safety for mixed legacy-local and canonical-UTC capture times."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from persome import cli
from persome.capture import scheduler
from persome.capture.timestamps import (
    parse_capture_path_timestamp,
    parse_capture_stem,
    parse_capture_timestamp,
)
from persome.mcp.captures import read_recent_capture
from persome.model import build as model_build
from persome.session import store as session_store
from persome.store import fts
from persome.timeline import aggregator
from persome.timeline import store as timeline_store
from persome.writer.chat_extractor import extract_chat_messages


def _capture(timestamp: str, marker: str) -> Path:
    return scheduler._write_capture(
        {
            "timestamp": timestamp,
            "schema_version": 2,
            "trigger": {"event_type": "manual"},
            "window_meta": {
                "app_name": "Chat",
                "title": marker,
                "bundle_id": "com.test.chat",
            },
            "focused_element": {"role": "AXTextArea", "value": marker},
            "visible_text": f"compatibility needle {marker}",
            "url": "",
        }
    )


def test_capture_stem_parser_supports_current_and_legacy_offsets() -> None:
    assert parse_capture_stem("2025-11-02T01-59-59-04-00") == datetime(
        2025, 11, 2, 5, 59, 59, tzinfo=UTC
    )
    assert parse_capture_stem("2025-11-02T01-00-00m05-00") == datetime(
        2025, 11, 2, 6, 0, tzinfo=UTC
    )
    assert parse_capture_stem("2025-11-02T06-00-01.123456p00-00") == datetime(
        2025, 11, 2, 6, 0, 1, 123456, tzinfo=UTC
    )
    assert parse_capture_stem("20251102T140000p0800") == datetime(2025, 11, 2, 6, 0, tzinfo=UTC)
    assert parse_capture_stem("2025-11-02 14-00-00p08-00") == datetime(
        2025, 11, 2, 6, 0, tzinfo=UTC
    )


def test_legacy_naive_and_basic_iso_use_historical_local_time(ac_root: Path, monkeypatch) -> None:
    original_timezone = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        assert parse_capture_timestamp("2025-01-15T12:30:00") == datetime(
            2025, 1, 15, 17, 30, tzinfo=UTC
        )
        aware = _capture("2025-01-15T17:00:00+00:00", "aware-older")
        naive = _capture("2025-01-15T12:30:00", "naive-newer")
        basic = _capture("20250115T124500-0500", "basic-newest")

        with fts.cursor() as conn:
            recent = fts.recent_captures(conn, limit=10)
            bounded = fts.search_captures(
                conn,
                query="compatibility needle",
                since="2025-01-15T17:40:00+00:00",
            )

        assert [row.id for row in recent] == [basic.stem, naive.stem, aware.stem]
        assert [row.id for row in bounded] == [basic.stem]

        expired = time.time() - 48 * 3600
        os.utime(naive, (expired, expired))
        scheduler.cleanup_buffer(
            retention_hours=24,
            processed_before_ts="2025-01-15T17:00:00+00:00",
        )
        assert naive.exists()
    finally:
        if original_timezone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_timezone
        time.tzset()


def test_dst_repeated_hour_naive_capture_is_not_age_deleted(ac_root: Path, monkeypatch) -> None:
    original_timezone = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        ambiguous = _capture("2025-11-02T01:30:00", "ambiguous-fold")
        expired = time.time() - 48 * 3600
        os.utime(ambiguous, (expired, expired))

        stats = scheduler.cleanup_buffer(
            retention_hours=0,
            processed_before_ts="2025-11-02T06:00:00+00:00",
        )

        assert stats["deleted"] == 0
        assert ambiguous.exists()
    finally:
        if original_timezone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_timezone
        time.tzset()


def test_nonstandard_legacy_iso_falls_back_to_bounded_json_timestamp(ac_root: Path) -> None:
    capture = _capture("2025-W03-3_12:30:00-05:00", "week-date")
    assert parse_capture_stem(capture.stem) is None
    assert parse_capture_path_timestamp(capture) == datetime(2025, 1, 15, 17, 30, tzinfo=UTC)
    exact = read_recent_capture(at=capture.stem)
    assert exact is not None and exact["file_stem"] == capture.stem

    expired = time.time() - 48 * 3600
    os.utime(capture, (expired, expired))
    stats = scheduler.cleanup_buffer(
        retention_hours=0,
        processed_before_ts="2025-01-15T17:31:00+00:00",
    )
    assert stats["deleted"] == 1
    assert not capture.exists()


def test_capture_fts_filters_and_orders_mixed_offsets_by_instant(ac_root: Path) -> None:
    before = _capture("2025-11-02T01:59:59-04:00", "before")
    after = _capture("2025-11-02T06:00:00+00:00", "after")

    with fts.cursor() as conn:
        recent = fts.recent_captures(conn, limit=10)
        bounded = fts.search_captures(
            conn,
            query="compatibility needle",
            since="2025-11-02T05:59:59.500000+00:00",
            until="2025-11-02T01:00:00-05:00",
        )

    assert [row.id for row in recent] == [after.stem, before.stem]
    assert [row.id for row in bounded] == [after.stem]


def test_cleanup_watermark_uses_instant_and_invalid_watermark_fails_closed(
    ac_root: Path,
) -> None:
    before = _capture("2025-11-02T01:59:59-04:00", "before")
    at_watermark = _capture("2025-11-02T01:00:00-05:00", "at-watermark")
    expired = time.time() - 48 * 3600
    for path in (before, at_watermark):
        os.utime(path, (expired, expired))

    stats = scheduler.cleanup_buffer(
        retention_hours=24,
        processed_before_ts="2025-11-02T06:00:00+00:00",
    )

    assert stats["deleted"] == 1
    assert not before.exists()
    assert at_watermark.exists()

    stats = scheduler.cleanup_buffer(
        retention_hours=0,
        processed_before_ts="not-an-iso-timestamp",
    )
    assert stats["deleted"] == 0
    assert at_watermark.exists()


def test_latest_capture_readers_and_timeline_scan_use_instant_order(ac_root: Path) -> None:
    before = _capture("2025-11-02T01:59:59-04:00", "before")
    after = _capture("2025-11-02T01:00:00-05:00", "after")

    recent = read_recent_capture()
    assert recent is not None
    assert recent["file_stem"] == after.stem
    assert cli._last_capture_info()[0] == "2025-11-02T01:00:00-05:00"

    files = aggregator.captures_in_window(
        datetime(2025, 11, 2, 5, 59, 0, tzinfo=UTC),
        datetime(2025, 11, 2, 6, 1, 0, tzinfo=UTC),
    )
    assert files == [before, after]
    utc_text = "2025-11-02T06:00:00+00:00"
    assert aggregator._short_time(utc_text) == datetime.fromisoformat(
        utc_text
    ).astimezone().strftime("%H:%M:%S")
    assert aggregator._short_time(utc_text, display_tz=timezone(timedelta(hours=-5))) == "01:00:00"


def test_timeline_store_treats_equivalent_offsets_as_the_same_window(ac_root: Path) -> None:
    before = timeline_store.TimelineBlock(
        id="before",
        start_time=datetime.fromisoformat("2025-11-02T01:58:00-04:00"),
        end_time=datetime.fromisoformat("2025-11-02T01:59:00-04:00"),
    )
    after = timeline_store.TimelineBlock(
        id="after",
        start_time=datetime.fromisoformat("2025-11-02T01:00:00-05:00"),
        end_time=datetime.fromisoformat("2025-11-02T01:01:00-05:00"),
    )
    with fts.cursor() as conn:
        timeline_store.insert(conn, before)
        timeline_store.insert(conn, after)
        assert timeline_store.has_window(
            conn,
            datetime(2025, 11, 2, 6, 0, tzinfo=UTC),
            datetime(2025, 11, 2, 6, 1, tzinfo=UTC),
        )
        latest = timeline_store.get_latest_end(conn)
        recent = timeline_store.query_recent(conn, limit=10)
        since = timeline_store.query_since(conn, datetime(2025, 11, 2, 5, 59, 30, tzinfo=UTC))
        bounded = timeline_store.query_range(
            conn,
            datetime(2025, 11, 2, 6, 0, tzinfo=UTC),
            datetime(2025, 11, 2, 6, 1, tzinfo=UTC),
        )

    assert latest == after.end_time
    assert [block.id for block in recent] == ["before", "after"]
    assert [block.id for block in since] == ["after"]
    assert [block.id for block in bounded] == ["after"]


def test_session_recovery_and_retry_order_mixed_offsets_by_instant(ac_root: Path) -> None:
    old_start = datetime.fromisoformat("2025-11-02T01:59:00-04:00")
    new_start = datetime.fromisoformat("2025-11-02T01:00:00-05:00")
    due_at = datetime.fromisoformat("2025-11-02T10:00:00+08:00")
    with fts.cursor() as conn:
        session_store.insert(conn, session_store.SessionRow(id="old", start_time=old_start))
        session_store.insert(conn, session_store.SessionRow(id="new", start_time=new_start))
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="retry",
                start_time=datetime.fromisoformat("2025-11-02T01:30:00+00:00"),
                end_time=datetime.fromisoformat("2025-11-02T01:45:00+00:00"),
                status="failed",
                next_retry_at=due_at,
            ),
        )
        recovered = session_store.recover_active(
            conn, recovered_at=datetime(2025, 11, 2, 6, 10, tzinfo=UTC)
        )
        due = session_store.list_due_for_retry(conn, now=datetime(2025, 11, 2, 2, 30, tzinfo=UTC))

    by_id = {row.id: row for row in recovered}
    assert by_id["old"].end_time == old_start + timedelta(microseconds=1)
    assert by_id["new"].end_time == new_start + timedelta(microseconds=1)
    assert [row.id for row in due] == ["retry"]


def test_capture_consumers_and_model_window_use_actual_instants(ac_root: Path) -> None:
    _capture("2025-11-02T01:59:59-04:00", "before")
    _capture("2025-11-02T01:00:00-05:00", "after")

    with fts.cursor() as conn:
        text, count, _gaps = extract_chat_messages(
            conn,
            "Chat",
            "2025-11-02T05:59:59.500000+00:00",
            "2025-11-02T06:00:00.500000+00:00",
        )

    assert count == 1
    assert "after" in text
    assert "before" not in text
    assert model_build._input_window() == {
        "start": "2025-11-02T01:59:59-04:00",
        "end": "2025-11-02T01:00:00-05:00",
    }
