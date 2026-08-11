"""Bounded success receipts for capture content deduplication.

The capture JSON remains the authoritative observation.  These rows contain
only a versioned content hash plus its successful capture identity and commit
time, allowing the S0 runner to restore its dedup head after a daemon restart.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

RECEIPT_LIMIT = 128

_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS capture_content_receipts (
        sequence     INTEGER PRIMARY KEY AUTOINCREMENT,
        fingerprint  TEXT NOT NULL,
        capture_id   TEXT NOT NULL,
        committed_at TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS ix_capture_content_receipts_fingerprint
        ON capture_content_receipts(fingerprint, sequence)""",
)
SCHEMA = ";\n".join(_SCHEMA_STATEMENTS) + ";\n"


@dataclass(frozen=True)
class CaptureContentReceipt:
    sequence: int
    fingerprint: str
    capture_id: str
    committed_at: str


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the owner-managed receipt table when schema ownership permits."""
    from . import fts

    if fts.is_client_process():
        return
    # `executescript` implicitly commits an open transaction. Keep schema
    # creation composable with capture write/clean transactions instead.
    for statement in _SCHEMA_STATEMENTS:
        conn.execute(statement)


def load_recent(
    conn: sqlite3.Connection,
    *,
    limit: int = RECEIPT_LIMIT,
) -> list[CaptureContentReceipt]:
    """Return the newest successful receipts first, bounded by ``limit``."""
    ensure_schema(conn)
    bounded_limit = max(0, int(limit))
    rows = conn.execute(
        "SELECT sequence, fingerprint, capture_id, committed_at "
        "FROM capture_content_receipts ORDER BY sequence DESC LIMIT ?",
        (bounded_limit,),
    ).fetchall()
    return [
        CaptureContentReceipt(
            sequence=int(row[0]),
            fingerprint=str(row[1]),
            capture_id=str(row[2]),
            committed_at=str(row[3]),
        )
        for row in rows
    ]


def record_success(
    conn: sqlite3.Connection,
    *,
    fingerprint: str,
    capture_id: str,
    committed_at: str,
    keep: int = RECEIPT_LIMIT,
) -> CaptureContentReceipt:
    """Record one successful capture and prune older receipts atomically."""
    if not fingerprint:
        raise ValueError("capture content receipt requires a fingerprint")
    if not capture_id:
        raise ValueError("capture content receipt requires a capture_id")
    if not committed_at:
        raise ValueError("capture content receipt requires committed_at")
    if keep < 1:
        raise ValueError("capture content receipt keep must be positive")

    ensure_schema(conn)
    savepoint = "capture_content_receipt_record"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        cursor = conn.execute(
            "INSERT INTO capture_content_receipts"
            " (fingerprint, capture_id, committed_at) VALUES (?, ?, ?)",
            (fingerprint, capture_id, committed_at),
        )
        sequence = int(cursor.lastrowid or 0)
        conn.execute(
            "DELETE FROM capture_content_receipts WHERE sequence NOT IN "
            "(SELECT sequence FROM capture_content_receipts "
            "ORDER BY sequence DESC LIMIT ?)",
            (int(keep),),
        )
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise

    return CaptureContentReceipt(
        sequence=sequence,
        fingerprint=fingerprint,
        capture_id=capture_id,
        committed_at=committed_at,
    )


def clear(conn: sqlite3.Connection) -> int:
    """Delete all content receipts and return the removed row count."""
    ensure_schema(conn)
    cursor = conn.execute("DELETE FROM capture_content_receipts")
    return int(cursor.rowcount)


def delete_for_captures(conn: sqlite3.Connection, capture_ids: list[str]) -> int:
    """Delete receipts backed by ``capture_ids`` inside the caller transaction."""
    requested = {capture_id for capture_id in capture_ids if capture_id}
    if not requested:
        return 0
    ensure_schema(conn)
    receipt_ids = {
        str(row[0])
        for row in conn.execute("SELECT DISTINCT capture_id FROM capture_content_receipts")
    }
    removed = 0
    # Intersect before issuing statements: one retention pass may remove many
    # thousands of raw captures, while this table contains at most 128 rows.
    for capture_id in requested & receipt_ids:
        cursor = conn.execute(
            "DELETE FROM capture_content_receipts WHERE capture_id=?",
            (capture_id,),
        )
        removed += int(cursor.rowcount)
    return removed
