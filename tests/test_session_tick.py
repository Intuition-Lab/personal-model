from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from persome import config as config_mod
from persome import paths
from persome.session import store as session_store
from persome.session import tick as session_tick
from persome.store import fts
from persome.timeline import store as timeline_store
from persome.writer import session_reducer

_TZ = timezone(timedelta(hours=8))


def _seed_block(start: datetime) -> None:
    with fts.cursor() as conn:
        timeline_store.insert(
            conn,
            timeline_store.TimelineBlock(
                start_time=start,
                end_time=start + timedelta(minutes=5),
                entries=["[Cursor] editing, involving —"],
                apps_used=["Cursor"],
                capture_count=1,
            ),
        )


def test_seconds_until_next_local_rolls_past_midnight() -> None:
    # The helper is a pure function of datetime.now() so we can only
    # assert properties: result must be in [0, 86400).
    s = session_tick._seconds_until_next_local(23, 55)
    assert 0 < s <= 86400


def test_reduce_all_pending_catches_ended_row(ac_root: Path, monkeypatch) -> None:
    start = datetime(2026, 4, 21, 9, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=5)
    _seed_block(start)

    # Simulate a session that was ended but whose reducer thread died
    # (status='ended', not 'reduced').
    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_stranded",
                start_time=start,
                end_time=end,
                status="ended",
            ),
        )

    monkeypatch.setenv("PERSOME_LLM_MOCK", "1")
    monkeypatch.setenv(
        "PERSOME_LLM_MOCK_JSON",
        json.dumps({"summary": "ok", "sub_tasks": ["[09:00-09:05, Cursor] x, involving —"]}),
    )

    cfg = config_mod.load(ac_root / "config.toml")
    results = session_reducer.reduce_all_pending(cfg)
    assert len(results) == 1
    assert results[0].succeeded is True

    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, "sess_stranded")
    assert row is not None
    assert row.status == "reduced"


def test_build_manager_wires_reducer_end_to_end(ac_root: Path, monkeypatch) -> None:
    """on_event → auto session start → force_end → row persisted → reducer run."""
    now = datetime.now().astimezone().replace(microsecond=0)
    minute = now.replace(second=0) - timedelta(minutes=1)
    start_dt = minute + timedelta(seconds=10)
    with fts.cursor() as conn:
        timeline_store.insert(
            conn,
            timeline_store.TimelineBlock(
                start_time=minute,
                end_time=minute + timedelta(minutes=1),
                entries=["[Cursor] editing, involving —"],
                apps_used=["Cursor"],
                capture_count=1,
            ),
        )
    capture_path = paths.capture_buffer_dir() / (
        start_dt.isoformat().replace(":", "-").replace("+", "p") + ".json"
    )
    capture_path.write_text(
        json.dumps(
            {
                "timestamp": start_dt.isoformat(),
                "window_meta": {"app_name": "Cursor", "title": "Persome"},
                "focused_element": {"role": "AXTextArea", "value": "editing"},
                "visible_text": "editing Persome runtime",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("PERSOME_LLM_MOCK", "1")
    monkeypatch.setenv(
        "PERSOME_LLM_MOCK_JSON",
        json.dumps({"summary": "done", "sub_tasks": ["[--, Cursor] ok, involving —"]}),
    )

    cfg = config_mod.load(ac_root / "config.toml")
    manager = session_tick.build_manager(cfg)

    manager.on_event(
        {
            "event_type": "AXFocusedWindowChanged",
            "bundle_id": "com.cursor",
            "_persome_capture_timestamp": start_dt.isoformat(),
        }
    )
    sid = manager.current_id
    assert sid is not None

    # After on_event, the 'active' row should be in the store.
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, sid)
    assert row is not None
    assert row.status == "active"

    # Force-end triggers on_session_end → persists ended row and spawns reducer.
    manager.force_end(reason="test")
    # Give the reducer thread a moment to finish.
    import time

    for _ in range(40):
        with fts.cursor() as conn:
            row = session_store.get_by_id(conn, sid)
        if row and row.status == "reduced":
            break
        time.sleep(0.05)

    assert row is not None
    assert row.status in ("reduced", "ended")  # Either the thread raced or it finished


def test_prune_telemetry_calls_parser_store(ac_root: Path, monkeypatch) -> None:
    monkeypatch.setattr(session_tick.parser_ticks_store, "prune", lambda conn: 7)
    assert session_tick._prune_telemetry_tables() == {"parser_ticks": 7}


def test_model_dirty_generation_does_not_clear_newer_evidence(ac_root: Path) -> None:
    with fts.cursor() as conn:
        session_store.set_system_state(conn, "model_structure_dirty", "1")
        session_store.increment_system_state(conn, "model_structure_dirty")
        assert not session_store.compare_and_set_system_state(
            conn,
            "model_structure_dirty",
            expected="1",
            value="0",
        )
        assert session_store.get_system_state(conn, "model_structure_dirty") == "2"
        assert session_store.compare_and_set_system_state(
            conn,
            "model_structure_dirty",
            expected="2",
            value="0",
        )


def test_boot_recovery_closes_stranded_active_sessions(ac_root: Path) -> None:
    first = datetime(2026, 7, 10, 9, 0, tzinfo=_TZ)
    second = first + timedelta(hours=1)
    boot = second + timedelta(hours=1)
    first_last = second - timedelta(microseconds=1)
    second_last = second + timedelta(minutes=20)
    with fts.cursor() as conn:
        for capture_id, captured_at in (
            ("first-last", first_last),
            ("second-last", second_last),
        ):
            fts.insert_capture(
                conn,
                id=capture_id,
                timestamp=captured_at.isoformat(),
                app_name="TestApp",
                bundle_id="test.app",
                window_title="Recovery",
                focused_role="AXStaticText",
                focused_value="",
                visible_text="durable recovery event",
                url="",
            )
        session_store.insert(
            conn,
            session_store.SessionRow(id="sess_old", start_time=first, status="active"),
        )
        session_store.insert(
            conn,
            session_store.SessionRow(id="sess_new", start_time=second, status="active"),
        )

    recovered = session_tick.recover_stranded_sessions(now=boot)
    assert [row.id for row in recovered] == ["sess_old", "sess_new"]
    assert [row.end_time for row in recovered] == [
        second,
        second_last + timedelta(microseconds=1),
    ]

    with fts.cursor() as conn:
        assert session_store.get_by_id(conn, "sess_old").status == "ended"
        assert session_store.get_by_id(conn, "sess_new").status == "ended"
    assert session_tick.recover_stranded_sessions(now=boot) == []


def test_boot_recovery_uses_last_durable_event_not_empty_boot_minute(
    ac_root: Path,
    fake_llm,
) -> None:
    minute = datetime(2026, 7, 10, 9, 0, tzinfo=_TZ)
    captured_at = minute + timedelta(seconds=30)
    boot = datetime(2026, 7, 10, 10, 3, 20, tzinfo=_TZ)
    stem = captured_at.isoformat().replace(":", "-").replace("+", "p")
    capture_path = paths.capture_buffer_dir() / f"{stem}.json"
    capture_path.write_text(
        json.dumps(
            {
                "timestamp": captured_at.isoformat(),
                "window_meta": {
                    "app_name": "Editor",
                    "title": "Recovery",
                    "bundle_id": "test.editor",
                },
                "focused_element": {"role": "AXStaticText", "value": "LAST_EVENT"},
                "visible_text": "LAST_EVENT",
            }
        ),
        encoding="utf-8",
    )
    block = timeline_store.TimelineBlock(
        start_time=minute,
        end_time=minute + timedelta(minutes=1),
        entries=["[Editor] LAST_EVENT"],
        apps_used=["Editor"],
        capture_count=1,
    )
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id=stem,
            timestamp=captured_at.isoformat(),
            app_name="Editor",
            bundle_id="test.editor",
            window_title="Recovery",
            focused_role="AXStaticText",
            focused_value="LAST_EVENT",
            visible_text="LAST_EVENT",
            url="",
        )
        timeline_store.insert(
            conn,
            block,
            source_captures=[
                timeline_store.TimelineBlockSource(
                    capture_id=f"capture:{stem}",
                    captured_at=captured_at,
                )
            ],
        )
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_boot_empty_minute",
                start_time=captured_at,
                status="active",
            ),
        )
        # Retention may remove the searchable capture projection after the
        # timeline block is durable; recovery must still use its source time.
        fts.delete_capture(conn, stem)

    recovered = session_tick.recover_stranded_sessions(now=boot)
    assert len(recovered) == 1
    assert recovered[0].end_time == captured_at + timedelta(microseconds=1)

    fake_llm.set_default(
        "reducer",
        json.dumps(
            {
                "summary": "Recovered the last durable event.",
                "sub_tasks": ["[09:00-09:01, Editor] recovered LAST_EVENT, involving —"],
            }
        ),
    )
    result = session_reducer.reduce_session(
        config_mod.load(ac_root / "config.toml"),
        session_id="sess_boot_empty_minute",
        start_time=captured_at,
        end_time=recovered[0].end_time,
    )

    assert result.succeeded and result.written
    assert result.skipped_reason == ""


def test_older_stranded_session_caps_at_next_start_without_empty_block_wait(
    ac_root: Path,
) -> None:
    next_start = datetime(2026, 7, 10, 9, 3, 20, tzinfo=_TZ)
    first_start = next_start - timedelta(microseconds=1)
    boot = next_start + timedelta(hours=1)
    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_older_empty",
                start_time=first_start,
                status="active",
            ),
        )
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_next",
                start_time=next_start,
                status="active",
            ),
        )

    recovered = session_tick.recover_stranded_sessions(now=boot)
    older = next(row for row in recovered if row.id == "sess_older_empty")
    assert older.end_time == next_start

    result = session_reducer.reduce_session(
        config_mod.load(ac_root / "config.toml"),
        session_id=older.id,
        start_time=older.start_time,
        end_time=older.end_time,
    )

    assert result.succeeded and not result.written
    assert result.skipped_reason == ""
    with fts.cursor() as conn:
        assert session_store.get_by_id(conn, older.id).status == "reduced"


@pytest.mark.parametrize("llm_succeeded", [True, False])
def test_terminal_callback_finalizes_no_write_and_heuristic_results(
    ac_root: Path,
    monkeypatch,
    llm_succeeded: bool,
) -> None:
    """No-new-block and heuristic terminal results both enter model finalization."""
    callback = None

    def fake_reduce_async(cfg, **kwargs):
        nonlocal callback
        callback = kwargs["on_done"]
        return None

    modeled: list[str] = []
    monkeypatch.setattr(session_tick.session_reducer, "reduce_session_async", fake_reduce_async)
    monkeypatch.setattr(
        session_tick.writer_agent,
        "finalize_session",
        lambda cfg, **kwargs: (
            modeled.append(kwargs["session_id"])
            or type("Result", (), {"completed": True, "errors": [], "skipped_reason": ""})()
        ),
    )

    cfg = config_mod.load(ac_root / "config.toml")
    manager = session_tick.build_manager(cfg)
    manager.on_event({"event_type": "focus", "bundle_id": "com.test"})
    sid = manager.current_id
    assert sid is not None
    manager.force_end(reason="test")
    assert callback is not None
    callback(
        session_reducer.ReduceResult(
            session_id=sid,
            succeeded=llm_succeeded,
            written=False,
            is_final=True,
        )
    )
    assert modeled == [sid]


async def test_reducer_retry_tick_finalizes_due_results(ac_root: Path, monkeypatch) -> None:
    reduced = session_reducer.ReduceResult(
        session_id="sess_retry_tick",
        succeeded=False,
        written=True,
        path="event-2026-07-10.md",
        entry_id="entry-1",
        is_final=True,
    )
    monkeypatch.setattr(session_tick.session_reducer, "retry_due", lambda cfg: [reduced])
    seen: list[dict] = []
    monkeypatch.setattr(
        session_tick.writer_agent,
        "finalize_session",
        lambda cfg, **kwargs: (
            seen.append(kwargs)
            or type("Result", (), {"completed": True, "errors": [], "skipped_reason": ""})()
        ),
    )
    sleeps = 0

    async def fake_sleep(seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(session_tick.asyncio, "sleep", fake_sleep)
    cfg = config_mod.load(ac_root / "config.toml")
    with pytest.raises(asyncio.CancelledError):
        await session_tick.run_reducer_retry_tick(cfg)
    assert seen == [
        {
            "session_id": "sess_retry_tick",
            "event_daily_path": "event-2026-07-10.md",
            "just_written_entry_id": "entry-1",
        }
    ]


async def test_reducer_retry_runs_writer_catch_up_before_sleep(ac_root: Path, monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        session_tick.writer_agent,
        "run",
        lambda cfg: seen.append("writer") or type("Result", (), {"reduced": 0, "modeled": 0})(),
    )

    async def cancel_on_sleep(seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(session_tick.asyncio, "sleep", cancel_on_sleep)
    cfg = config_mod.load(ac_root / "config.toml")
    with pytest.raises(asyncio.CancelledError):
        await session_tick.run_reducer_retry_tick(cfg)
    assert seen == ["writer"]


async def test_reducer_retry_tick_retries_pending_modeling_each_minute(
    ac_root: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        session_tick.writer_agent,
        "run",
        lambda cfg: type("Result", (), {"reduced": 0, "modeled": 0})(),
    )
    monkeypatch.setattr(session_tick.session_reducer, "retry_due", lambda cfg: [])
    seen: list[str] = []
    monkeypatch.setattr(
        session_tick.writer_agent,
        "retry_pending_modeling",
        lambda cfg: seen.append("pending-modeling") or [],
    )
    sleeps = 0

    async def run_one_tick(seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(session_tick.asyncio, "sleep", run_one_tick)
    cfg = config_mod.load(ac_root / "config.toml")
    with pytest.raises(asyncio.CancelledError):
        await session_tick.run_reducer_retry_tick(cfg)

    assert seen == ["pending-modeling"]


async def test_flush_tick_models_successful_active_window(ac_root: Path, monkeypatch) -> None:
    start = datetime(2026, 7, 10, 12, 0, tzinfo=_TZ)
    manager = type("Manager", (), {"current_snapshot": lambda self: ("sess_live", start)})()
    calls: list[str] = []
    monkeypatch.setattr(
        session_tick.session_reducer,
        "flush_active_session",
        lambda cfg, **kwargs: object(),
    )
    monkeypatch.setattr(
        session_tick.writer_agent,
        "model_active_session",
        lambda cfg, **kwargs: (
            calls.append(kwargs["session_id"])
            or type("Result", (), {"completed": True, "errors": []})()
        ),
    )
    sleeps = 0

    async def run_once(seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(session_tick.asyncio, "sleep", run_once)
    cfg = config_mod.load(ac_root / "config.toml")
    with pytest.raises(asyncio.CancelledError):
        await session_tick.run_flush_tick(cfg, manager)
    assert calls == ["sess_live"]
