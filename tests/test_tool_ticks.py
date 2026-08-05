"""Tests for MCP tool-call telemetry persistence."""

from __future__ import annotations

from persome.store import fts
from persome.store import tool_ticks as tt


def _tick(conn, *, ts, tool, client=""):  # noqa: ANN001
    tt.record_tick(conn, ts=ts, tool=tool, client=client)


def test_stats_counts_and_buckets(ac_root) -> None:  # noqa: ANN001
    with fts.cursor() as conn:
        _tick(conn, ts="2026-08-01T10:00:00+08:00", tool="search", client="claude-code")
        _tick(conn, ts="2026-08-01T10:01:00+08:00", tool="search", client="claude-code")
        _tick(conn, ts="2026-08-01T10:02:00+08:00", tool="current_context", client="codex")
        _tick(conn, ts="2026-08-02T09:00:00+08:00", tool="search")
        s = tt.stats(conn)
    assert s["total"] == 4
    assert s["by_tool"] == {"search": 3, "current_context": 1}
    assert s["by_client"] == {"claude-code": 2, "codex": 1, "": 1}
    assert s["by_day"] == {"2026-08-01": 3, "2026-08-02": 1}
    assert s["first_ts"] == "2026-08-01T10:00:00+08:00"
    assert s["last_ts"] == "2026-08-02T09:00:00+08:00"


def test_stats_window_filter(ac_root) -> None:  # noqa: ANN001
    with fts.cursor() as conn:
        _tick(conn, ts="2026-08-01T10:00:00+08:00", tool="search")
        _tick(conn, ts="2026-08-02T10:00:00+08:00", tool="chat")
        s = tt.stats(conn, since="2026-08-02T00:00", until="2026-08-03T00:00")
    assert s["total"] == 1
    assert s["by_tool"] == {"chat": 1}
    assert s["by_day"] == {"2026-08-02": 1}
    assert s["since"] == "2026-08-02T00:00"
    assert s["until"] == "2026-08-03T00:00"


def test_stats_empty(ac_root) -> None:  # noqa: ANN001
    with fts.cursor() as conn:
        s = tt.stats(conn)
    assert s["total"] == 0
    assert s["by_tool"] == {}
    assert s["by_client"] == {}
    assert s["by_day"] == {}
    assert s["first_ts"] is None
    assert s["last_ts"] is None
    assert s["since"] is None
    assert s["until"] is None


def test_record_tick_returns_rowid_and_roundtrips(ac_root) -> None:  # noqa: ANN001
    with fts.cursor() as conn:
        rid = tt.record_tick(
            conn, ts="2026-08-01T10:00:00+08:00", tool="search", client="claude-code"
        )
        assert rid > 0
        row = conn.execute(
            "SELECT ts, tool, client FROM tool_ticks WHERE id = ?", (rid,)
        ).fetchone()
    assert row["ts"] == "2026-08-01T10:00:00+08:00"
    assert row["tool"] == "search"
    assert row["client"] == "claude-code"


def test_prune_keeps_recent(ac_root) -> None:  # noqa: ANN001
    with fts.cursor() as conn:
        for i in range(10):
            _tick(conn, ts=f"2026-08-01T10:{i:02d}:00+08:00", tool="search")
        deleted = tt.prune(conn, keep=4)
        s = tt.stats(conn)
    assert deleted == 6
    assert s["total"] == 4


def test_server_call_tool_records_tick(ac_root) -> None:  # noqa: ANN001
    """The FastMCP.call_tool override records exactly one local tick per call."""
    import asyncio

    from persome.mcp import server as mcp_server

    server = mcp_server.build_server(auth_enabled=False, include_http_routes=False)
    asyncio.run(server.call_tool("list_memories", {}))
    with fts.cursor() as conn:
        s = tt.stats(conn)
    assert s["total"] == 1
    assert s["by_tool"] == {"list_memories": 1}
    # No request context in a direct call — attribution degrades to "".
    assert s["by_client"] == {"": 1}


def test_tick_failure_never_breaks_the_call(ac_root, monkeypatch) -> None:  # noqa: ANN001
    """A telemetry write failure is swallowed; the tool call still succeeds."""
    import asyncio

    from persome.mcp import server as mcp_server

    def _boom(*a, **k):  # noqa: ANN001, ANN002, ANN003
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(tt, "record_tick", _boom)
    server = mcp_server.build_server(auth_enabled=False, include_http_routes=False)
    result = asyncio.run(server.call_tool("list_memories", {}))
    assert result is not None
