"""DAO for windowed structured memory deltas and their apply audit.

One LLM reading of each newly flushed window emits a structured
``memory_delta {owner_alias_candidates, entities, assertions, relations, events}``.
The post-gate
payload is persisted here before deterministic application mints Points and
Lines. ``apply_status`` makes interrupted application retryable without
spending another LLM call or reinforcing an edge twice.

Rows are append-only; active-session flushes and terminal finalization create
one row per non-overlapping window and resume application from that row. The
payload column stores
the POST-GATE delta (after the deterministic quote/roster/predicate/confidence
gates in ``writer/memory_delta.py``), plus a ``dropped`` audit count so gate
strictness stays observable.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from ..logger import get

logger = get("persome.store.memory_deltas")

STATUS_SHADOW = "shadow"
CLAIM_EXTRACTING = "extracting"
CLAIM_FAILED = "failed"
CLAIM_PERSISTED = "persisted"
DEFAULT_CLAIM_LEASE_SECONDS = 15 * 60
ITEM_LEDGER_VERSION = 1


@dataclass(frozen=True)
class WindowClaim:
    """One canonical session-window claim returned by the SQLite CAS."""

    acquired: bool
    window_key: str
    token: str = ""
    state: str = ""
    delta_id: int = 0


class ClaimLostError(RuntimeError):
    """Raised when an expired claimant tries to persist after being replaced."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_deltas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    created_at TEXT NOT NULL,            -- ISO8601 consolidation time
    model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'shadow',
    payload TEXT NOT NULL DEFAULT '{}',  -- post-gate delta JSON
    dropped INTEGER NOT NULL DEFAULT 0,  -- items removed by the deterministic gates
    apply_status TEXT NOT NULL DEFAULT 'unknown',
    window_start TEXT NOT NULL DEFAULT '',
    window_end TEXT NOT NULL DEFAULT '',
    is_final INTEGER NOT NULL DEFAULT 1,
    item_ledger_version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memory_deltas_session ON memory_deltas(session_id);
CREATE INDEX IF NOT EXISTS idx_memory_deltas_created ON memory_deltas(created_at DESC);

-- This is deliberately separate from memory_deltas. Historical and explicit
-- owner-edit rows are append-only and may have empty or duplicate windows;
-- only bounded extraction windows participate in the claim protocol.
CREATE TABLE IF NOT EXISTS memory_delta_window_claims (
    session_id TEXT NOT NULL,
    window_key TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    state TEXT NOT NULL,
    claim_token TEXT NOT NULL DEFAULT '',
    lease_until TEXT NOT NULL DEFAULT '',
    delta_id INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, window_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_delta_window_claim_delta
    ON memory_delta_window_claims(delta_id) WHERE delta_id IS NOT NULL;
"""


def _utc(value: datetime) -> datetime:
    # Historical callers sometimes supplied naive local datetimes. Preserve
    # that interpretation, then normalize the instant before keying it.
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(UTC)


def _canonical_timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_window_key(window_start: datetime, window_end: datetime) -> str:
    """Return one representation for timezone-equivalent bounded windows."""
    start = _utc(window_start)
    end = _utc(window_end)
    if start >= end:
        raise ValueError("memory_delta window must have positive duration")
    return f"{_canonical_timestamp(start)}/{_canonical_timestamp(end)}"


def _claim_times(*, now: datetime | None, lease_seconds: int) -> tuple[str, str, datetime]:
    current = _utc(now or datetime.now(UTC))
    lease_until = current + timedelta(seconds=max(1, lease_seconds))
    return _canonical_timestamp(current), _canonical_timestamp(lease_until), current


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return _utc(parsed)


def ensure_schema(conn: sqlite3.Connection) -> None:
    from . import fts

    if fts.is_client_process():
        return
    conn.executescript(SCHEMA)
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memory_deltas)")}
    if "apply_status" not in columns:
        # Old rows may or may not have been applied. Treat them as processed;
        # only rows written by the new code carry a retryable failed state.
        conn.execute(
            "ALTER TABLE memory_deltas ADD COLUMN apply_status TEXT NOT NULL DEFAULT 'unknown'"
        )
    if "window_start" not in columns:
        conn.execute("ALTER TABLE memory_deltas ADD COLUMN window_start TEXT NOT NULL DEFAULT ''")
    if "window_end" not in columns:
        conn.execute("ALTER TABLE memory_deltas ADD COLUMN window_end TEXT NOT NULL DEFAULT ''")
    if "is_final" not in columns:
        conn.execute("ALTER TABLE memory_deltas ADD COLUMN is_final INTEGER NOT NULL DEFAULT 1")
    if "item_ledger_version" not in columns:
        # Pre-ledger rows may already have committed a subset of their effects.
        # Keep them explicitly unversioned so recovery can fail closed instead
        # of inventing exactly-once receipts after the fact.
        conn.execute(
            "ALTER TABLE memory_deltas ADD COLUMN item_ledger_version INTEGER NOT NULL DEFAULT 0"
        )


def insert(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    payload: dict,
    model: str = "",
    dropped: int = 0,
    status: str = STATUS_SHADOW,
    apply_status: str = "not_requested",
    created_at: datetime | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    is_final: bool = True,
) -> int:
    ensure_schema(conn)
    return _insert_row(
        conn,
        session_id=session_id,
        payload=payload,
        model=model,
        dropped=dropped,
        status=status,
        apply_status=apply_status,
        created_at=created_at,
        window_start=window_start,
        window_end=window_end,
        is_final=is_final,
    )


def _insert_row(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    payload: dict,
    model: str,
    dropped: int,
    status: str,
    apply_status: str,
    created_at: datetime | None,
    window_start: datetime | None,
    window_end: datetime | None,
    is_final: bool,
) -> int:
    ts = (created_at or datetime.now().astimezone()).isoformat()
    cur = conn.execute(
        "INSERT INTO memory_deltas"
        " (session_id, created_at, model, status, payload, dropped, apply_status,"
        " window_start, window_end, is_final)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            ts,
            model,
            status,
            json.dumps(payload, ensure_ascii=False),
            dropped,
            apply_status,
            window_start.isoformat() if window_start else "",
            window_end.isoformat() if window_end else "",
            1 if is_final else 0,
        ),
    )
    return int(cur.lastrowid or 0)


def claim_window(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    window_start: datetime,
    window_end: datetime,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
) -> WindowClaim:
    """Atomically acquire or observe one canonical extraction window.

    The initial INSERT is the serialization point for fresh windows. An
    expired or explicitly failed claim is reclaimed with a token-and-lease
    compare-and-swap, so an old worker cannot later bind its payload.
    """
    ensure_schema(conn)
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("memory_delta claim requires a session_id")
    key = canonical_window_key(window_start, window_end)
    start_text = _canonical_timestamp(window_start)
    end_text = _canonical_timestamp(window_end)
    now_text, lease_text, current = _claim_times(now=now, lease_seconds=lease_seconds)
    token = uuid.uuid4().hex
    inserted = conn.execute(
        "INSERT INTO memory_delta_window_claims"
        " (session_id, window_key, window_start, window_end, state, claim_token,"
        " lease_until, delta_id, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)"
        " ON CONFLICT(session_id, window_key) DO NOTHING",
        (
            sid,
            key,
            start_text,
            end_text,
            CLAIM_EXTRACTING,
            token,
            lease_text,
            now_text,
        ),
    )
    if inserted.rowcount == 1:
        return WindowClaim(
            acquired=True,
            window_key=key,
            token=token,
            state=CLAIM_EXTRACTING,
        )

    # A losing connection either observes the live/persisted claim or reclaims
    # an expired one. Loop once after a lost CAS to report the new winner.
    for _attempt in range(2):
        row = conn.execute(
            "SELECT state, claim_token, lease_until, delta_id"
            " FROM memory_delta_window_claims WHERE session_id=? AND window_key=?",
            (sid, key),
        ).fetchone()
        if row is None:
            # Only possible if a future maintenance path deletes claims. Retry
            # through this function rather than performing an unguarded write.
            return claim_window(
                conn,
                session_id=sid,
                window_start=window_start,
                window_end=window_end,
                now=current,
                lease_seconds=lease_seconds,
            )
        state = str(row[0] or "")
        current_token = str(row[1] or "")
        current_lease = str(row[2] or "")
        delta_id = int(row[3] or 0)
        if state == CLAIM_PERSISTED and delta_id:
            return WindowClaim(
                acquired=False,
                window_key=key,
                state=state,
                delta_id=delta_id,
            )
        lease_deadline = _parse_timestamp(current_lease)
        if state == CLAIM_EXTRACTING and lease_deadline is not None and lease_deadline > current:
            return WindowClaim(acquired=False, window_key=key, state=state)

        replaced = conn.execute(
            "UPDATE memory_delta_window_claims SET state=?, claim_token=?, lease_until=?,"
            " delta_id=NULL, updated_at=?"
            " WHERE session_id=? AND window_key=? AND state=? AND claim_token=?"
            " AND lease_until=? AND delta_id IS NULL",
            (
                CLAIM_EXTRACTING,
                token,
                lease_text,
                now_text,
                sid,
                key,
                state,
                current_token,
                current_lease,
            ),
        )
        if replaced.rowcount == 1:
            return WindowClaim(
                acquired=True,
                window_key=key,
                token=token,
                state=CLAIM_EXTRACTING,
            )
    return WindowClaim(acquired=False, window_key=key, state=CLAIM_EXTRACTING)


def fail_claim(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    window_start: datetime,
    window_end: datetime,
    token: str,
) -> bool:
    """Release a handled failure; a crashed worker relies on lease expiry."""
    ensure_schema(conn)
    key = canonical_window_key(window_start, window_end)
    changed = conn.execute(
        "UPDATE memory_delta_window_claims SET state=?, claim_token='', lease_until='',"
        " updated_at=? WHERE session_id=? AND window_key=? AND state=? AND claim_token=?"
        " AND delta_id IS NULL",
        (
            CLAIM_FAILED,
            _canonical_timestamp(datetime.now(UTC)),
            session_id,
            key,
            CLAIM_EXTRACTING,
            token,
        ),
    )
    return changed.rowcount == 1


def insert_for_claim(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    payload: dict,
    token: str,
    model: str = "",
    dropped: int = 0,
    status: str = STATUS_SHADOW,
    apply_status: str = "not_requested",
    created_at: datetime | None = None,
    window_start: datetime,
    window_end: datetime,
    is_final: bool = True,
) -> int:
    """Insert the payload and bind it to the still-owned claim atomically."""
    ensure_schema(conn)
    key = canonical_window_key(window_start, window_end)
    if conn.in_transaction:
        raise RuntimeError("insert_for_claim requires an autocommit connection")
    conn.execute("BEGIN IMMEDIATE")
    try:
        owned = conn.execute(
            "SELECT 1 FROM memory_delta_window_claims"
            " WHERE session_id=? AND window_key=? AND state=? AND claim_token=?"
            " AND delta_id IS NULL",
            (session_id, key, CLAIM_EXTRACTING, token),
        ).fetchone()
        if owned is None:
            raise ClaimLostError("memory_delta window claim is no longer owned")
        delta_id = _insert_row(
            conn,
            session_id=session_id,
            payload=payload,
            model=model,
            dropped=dropped,
            status=status,
            apply_status=apply_status,
            created_at=created_at,
            window_start=window_start,
            window_end=window_end,
            is_final=is_final,
        )
        # Seed the immutable item ledger inside the same transaction as the
        # parent payload. A malformed/divergent item set rolls both back, so a
        # persisted delta is never published without its retry definition.
        from . import memory_delta_items

        memory_delta_items.seed(conn, delta_id=delta_id, payload=payload)
        conn.execute(
            "UPDATE memory_deltas SET item_ledger_version=? WHERE id=?",
            (ITEM_LEDGER_VERSION, delta_id),
        )
        bound = conn.execute(
            "UPDATE memory_delta_window_claims SET state=?, claim_token='', lease_until='',"
            " delta_id=?, updated_at=?"
            " WHERE session_id=? AND window_key=? AND state=? AND claim_token=?"
            " AND delta_id IS NULL",
            (
                CLAIM_PERSISTED,
                delta_id,
                _canonical_timestamp(datetime.now(UTC)),
                session_id,
                key,
                CLAIM_EXTRACTING,
                token,
            ),
        )
        if bound.rowcount != 1:
            raise ClaimLostError("memory_delta window claim changed before payload bind")
        conn.execute("COMMIT")
        return delta_id
    except Exception:
        conn.execute("ROLLBACK")
        raise


def persisted_for_window(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    window_start: datetime,
    window_end: datetime,
) -> sqlite3.Row | None:
    """Return the payload bound to a canonical window claim, if complete."""
    ensure_schema(conn)
    key = canonical_window_key(window_start, window_end)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT m.* FROM memory_delta_window_claims c"
        " JOIN memory_deltas m ON m.id=c.delta_id"
        " WHERE c.session_id=? AND c.window_key=? AND c.state=?",
        (session_id, key, CLAIM_PERSISTED),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


def remember_persisted_window(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    window_start: datetime,
    window_end: datetime,
    delta_id: int,
) -> None:
    """Attach an existing non-empty legacy row to its canonical window key."""
    ensure_schema(conn)
    key = canonical_window_key(window_start, window_end)
    now = _canonical_timestamp(datetime.now(UTC))
    conn.execute(
        "INSERT INTO memory_delta_window_claims"
        " (session_id, window_key, window_start, window_end, state, claim_token,"
        " lease_until, delta_id, updated_at) VALUES (?, ?, ?, ?, ?, '', '', ?, ?)"
        " ON CONFLICT(session_id, window_key) DO NOTHING",
        (
            session_id,
            key,
            _canonical_timestamp(window_start),
            _canonical_timestamp(window_end),
            CLAIM_PERSISTED,
            delta_id,
            now,
        ),
    )


def recent(conn: sqlite3.Connection, *, limit: int = 20) -> list[sqlite3.Row]:
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    return list(
        conn.execute(
            "SELECT * FROM memory_deltas ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        )
    )


def latest_for_session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM memory_deltas WHERE session_id = ?"
        " ORDER BY created_at DESC, id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


def latest_for_window(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    window_start: datetime,
    window_end: datetime,
) -> sqlite3.Row | None:
    """Return the newest attempt for one instant-equivalent modeling window.

    Exact text is the fast path. The bounded legacy fallback accepts Z,
    ``+00:00``, and other timezone offsets that denote the same instants.
    Once observed, the caller attaches the row to the canonical claim table.
    """
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM memory_deltas WHERE session_id=? AND window_start=? AND window_end=?"
        " ORDER BY id DESC LIMIT 1",
        (session_id, window_start.isoformat(), window_end.isoformat()),
    ).fetchone()
    if row is not None:
        return cast(sqlite3.Row, row)

    target = canonical_window_key(window_start, window_end)
    candidates = conn.execute(
        "SELECT * FROM memory_deltas WHERE session_id=?"
        " AND window_start<>'' AND window_end<>'' ORDER BY id DESC",
        (session_id,),
    ).fetchall()
    for candidate in candidates:
        try:
            candidate_key = canonical_window_key(
                datetime.fromisoformat(str(candidate["window_start"])),
                datetime.fromisoformat(str(candidate["window_end"])),
            )
        except (TypeError, ValueError):
            continue
        if candidate_key == target:
            return cast(sqlite3.Row, candidate)
    return None


def next_for_session_start(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    window_start: datetime,
    through: datetime,
) -> sqlite3.Row | None:
    """Return the earliest persisted window beginning at one watermark."""
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM memory_deltas m WHERE session_id=? AND window_start=?"
        " AND window_end>window_start AND window_end<=?"
        " AND id=(SELECT MAX(id) FROM memory_deltas WHERE session_id=m.session_id"
        " AND window_start=m.window_start AND window_end=m.window_end)"
        " ORDER BY window_end ASC LIMIT 1",
        (session_id, window_start.isoformat(), through.isoformat()),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


def set_apply_status(conn: sqlite3.Connection, delta_id: int, status: str) -> None:
    ensure_schema(conn)
    conn.execute("UPDATE memory_deltas SET apply_status=? WHERE id=?", (status, delta_id))


def stats(conn: sqlite3.Connection) -> dict:
    """Aggregate the latest attempt for every distinct session window."""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT session_id, payload, dropped FROM memory_deltas m"
        " WHERE id = (SELECT MAX(id) FROM memory_deltas"
        " WHERE session_id=m.session_id AND window_start=m.window_start"
        " AND window_end=m.window_end)"
    ).fetchall()
    heads = {
        "owner_alias_candidates": 0,
        "entities": 0,
        "assertions": 0,
        "relations": 0,
        "events": 0,
    }
    dropped = 0
    for _sid, payload, drop in rows:
        dropped += int(drop or 0)
        try:
            delta = json.loads(payload)
        except (TypeError, ValueError):
            continue
        for head in heads:
            items = delta.get(head)
            if isinstance(items, list):
                heads[head] += len(items)
    total = conn.execute("SELECT COUNT(*) FROM memory_deltas").fetchone()[0]
    return {
        "rows": int(total),
        "sessions": len({row[0] for row in rows}),
        "heads": heads,
        "dropped_by_gates": dropped,
    }
