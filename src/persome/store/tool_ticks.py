"""MCP tool-call telemetry persistence and aggregation.

Local-only usage counting: which MCP tools were called, when, and by which
client. Rows never leave ``index.db`` — see ``SECURITY_PRIVACY.md`` ("no
telemetry or update phone-home"); this table exists so the owner can see
their own usage (heatmap / leverage metrics) and export bare daily counts
by explicit choice (``persome stats export``). Arguments and results are
deliberately NOT recorded.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ..logger import get

logger = get("persome.store.tool_ticks")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,              -- ISO8601 call time (local, with offset)
    tool TEXT NOT NULL,            -- MCP tool name (e.g. 'search')
    client TEXT NOT NULL,          -- clientInfo.name best-effort ('' unknown)
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_ticks_ts ON tool_ticks(ts DESC);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    from . import fts

    if fts.is_client_process():
        return
    conn.executescript(SCHEMA)


def record_tick(
    conn: sqlite3.Connection,
    *,
    ts: str,
    tool: str,
    client: str = "",
) -> int:
    """Insert one tool-call telemetry row. Returns the row id.

    Only the tool name and timestamp are stored — never arguments or
    results. ``client`` is the MCP clientInfo name when the session
    exposes one, else the empty string.
    """
    ensure_schema(conn)
    cur = conn.execute(
        """
        INSERT INTO tool_ticks (ts, tool, client, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            ts,
            str(tool or ""),
            str(client or ""),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def stats(conn: sqlite3.Connection, *, since: str = "", until: str = "￿") -> dict:
    """Tool-call telemetry over ``[since, until)`` by ts.

    Returns:
        - ``total``     — number of calls in the window.
        - ``by_tool``   — ``{<tool>: count}``.
        - ``by_client`` — ``{<client>: count}`` (``""`` bucket = unknown).
        - ``by_day``    — ``{YYYY-MM-DD: count}`` daily totals; this is the
                           series retention math consumes (a day is "active"
                           when its count is > 0).
        - ``first_ts`` / ``last_ts`` — bounds of the recorded window
                           (``None`` when empty).
        - ``since`` / ``until`` — echoed bounds (``None`` when unbounded).
    """
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ts, tool, client FROM tool_ticks WHERE ts >= ? AND ts < ? ORDER BY ts",
        (since, until),
    ).fetchall()
    total = len(rows)
    by_tool: dict[str, int] = {}
    by_client: dict[str, int] = {}
    by_day: dict[str, int] = {}
    for r in rows:
        by_tool[str(r["tool"])] = by_tool.get(str(r["tool"]), 0) + 1
        by_client[str(r["client"])] = by_client.get(str(r["client"]), 0) + 1
        day = str(r["ts"])[:10]
        by_day[day] = by_day.get(day, 0) + 1
    return {
        "total": total,
        "by_tool": by_tool,
        "by_client": by_client,
        "by_day": by_day,
        "first_ts": str(rows[0]["ts"]) if rows else None,
        "last_ts": str(rows[-1]["ts"]) if rows else None,
        "since": since or None,
        "until": until if until != "￿" else None,
    }


def prune(conn: sqlite3.Connection, *, keep: int = 50000) -> int:
    """Keep only the most recent ``keep`` rows (bounded telemetry). Returns the
    number of rows deleted."""
    ensure_schema(conn)
    cur = conn.execute(
        "DELETE FROM tool_ticks WHERE id NOT IN "
        "(SELECT id FROM tool_ticks ORDER BY id DESC LIMIT ?)",
        (keep,),
    )
    conn.commit()
    return cur.rowcount
