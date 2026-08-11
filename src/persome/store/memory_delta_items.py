"""Item-level apply ledger for persisted windowed memory deltas.

The parent ``memory_deltas`` row proves that extraction was persisted once.
This table proves which deterministic effects completed.  Claims are ordered
and leased; effect code remains responsible for an idempotent receipt/probe so
a crash between the effect commit and the final ledger update can be replayed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from . import event_occurrences, relation_edges

STATE_PENDING = "pending"
STATE_APPLYING = "applying"
STATE_APPLIED = "applied"
STATE_FAILED = "failed"
DEFAULT_LEASE_SECONDS = 15 * 60

_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_delta_items (
    delta_id INTEGER NOT NULL,
    item_kind TEXT NOT NULL,
    item_key TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    payload TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    claim_token TEXT NOT NULL DEFAULT '',
    lease_until TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    effect_kind TEXT NOT NULL DEFAULT '',
    effect_id TEXT NOT NULL DEFAULT '',
    geometry_changed INTEGER CHECK (geometry_changed IN (0, 1)),
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (delta_id, item_kind, item_key)
)
"""

_INDEXES = (
    """CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_delta_items_ordinal
    ON memory_delta_items(delta_id, ordinal)""",
    """CREATE INDEX IF NOT EXISTS ix_memory_delta_items_state
    ON memory_delta_items(delta_id, state, ordinal)""",
)
SCHEMA = ";\n".join((_TABLE_SCHEMA.strip(), *_INDEXES)) + ";\n"


class DeltaItemCollisionError(RuntimeError):
    """One stable item identity mapped to different payloads."""


@dataclass(frozen=True)
class DeltaItem:
    kind: str
    key: str
    ordinal: int
    payload_hash: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ClaimedDeltaItem(DeltaItem):
    delta_id: int
    token: str
    attempts: int

    @property
    def effect_key(self) -> str:
        return f"memory-delta:{self.delta_id}:{self.kind}:{self.key}"


def ensure_schema(conn: sqlite3.Connection) -> None:
    from . import fts

    if fts.is_client_process():
        return
    conn.execute(_TABLE_SCHEMA)
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memory_delta_items)")}
    if "geometry_changed" not in columns:
        # Existing applied rows predate the durable geometry receipt. Keep them
        # NULL (unknown) so recovery schedules a conservative rebuild instead
        # of laundering the missing fact into a false negative.
        conn.execute(
            "ALTER TABLE memory_delta_items ADD COLUMN geometry_changed INTEGER"
            " CHECK (geometry_changed IN (0, 1))"
        )
    for statement in _INDEXES:
        conn.execute(statement)


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.astimezone()
    return current.astimezone(UTC)


def _timestamp(value: datetime | None = None) -> str:
    return _now(value).isoformat(timespec="microseconds")


def _norm(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    return " ".join(text.split()).casefold()


def _canonical_of(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("ref") or value.get("new_entity") or value.get("canonical") or "")


def _encoded(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(payload: object) -> str:
    return hashlib.sha256(_encoded(payload).encode("utf-8")).hexdigest()


def _fallback_key(kind: str, payload: dict[str, Any]) -> str:
    return f"{kind}:{_hash(payload)[:32]}"


def _item_key(kind: str, payload: dict[str, Any]) -> str:
    if kind == "entity":
        canonical = _norm(_canonical_of(payload))
        entity_kind = _norm(payload.get("kind"))
        if canonical and entity_kind:
            return f"entity:{entity_kind}:{canonical}"
    elif kind == "assertion":
        subject = _norm(_canonical_of(payload.get("subject")))
        text = _norm(payload.get("text"))
        if subject and text:
            return f"assertion:{subject}:{hashlib.sha256(text.encode()).hexdigest()[:24]}"
    elif kind == "relation":
        src = _canonical_of(payload.get("src"))
        dst = _canonical_of(payload.get("dst"))
        predicate = str(payload.get("predicate") or "")
        if src and dst and predicate:
            try:
                edge_key = relation_edges.canonical_edge_key(src, dst, predicate)
            except ValueError:
                pass
            else:
                operation = _encoded(
                    {
                        "edge_key": edge_key,
                        "ended": bool(payload.get("ended")),
                        "polarity": str(payload.get("polarity") or "0"),
                    }
                )
                return f"relation:{hashlib.sha256(operation.encode()).hexdigest()[:32]}"
    elif kind == "event":
        title = str(payload.get("title") or "")
        participants = [
            canonical
            for raw in (payload.get("participants") or [])
            if (canonical := _canonical_of(raw))
        ]
        if title:
            return "event:" + event_occurrences.make_item_key(
                title=title,
                participants=participants,
                quote=str(payload.get("quote") or ""),
                explicit=payload.get("item_key"),
            )
    return _fallback_key(kind, payload)


def build_items(payload: dict[str, Any]) -> list[DeltaItem]:
    """Build one deterministic, ordered item set from a post-gate payload."""
    items: list[DeltaItem] = []
    seen: dict[tuple[str, str], str] = {}
    for head, kind in (
        ("entities", "entity"),
        ("assertions", "assertion"),
        ("relations", "relation"),
        ("events", "event"),
    ):
        values = payload.get(head) or []
        if not isinstance(values, list):
            continue
        for raw in values:
            if not isinstance(raw, dict):
                continue
            item_payload = dict(raw)
            key = _item_key(kind, item_payload)
            payload_hash = _hash(item_payload)
            identity = (kind, key)
            prior_hash = seen.get(identity)
            if prior_hash is not None:
                if prior_hash != payload_hash:
                    raise DeltaItemCollisionError(f"item {kind}/{key} has divergent candidates")
                continue
            seen[identity] = payload_hash
            items.append(
                DeltaItem(
                    kind=kind,
                    key=key,
                    ordinal=len(items),
                    payload_hash=payload_hash,
                    payload=item_payload,
                )
            )
    return items


def seed(conn: sqlite3.Connection, *, delta_id: int, payload: dict[str, Any]) -> list[DeltaItem]:
    """Seed or verify the immutable ledger definition for one parent delta."""
    ensure_schema(conn)
    expected = build_items(payload)
    now = _timestamp()
    for item in expected:
        conn.execute(
            "INSERT INTO memory_delta_items"
            " (delta_id, item_kind, item_key, ordinal, payload_hash, payload, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(delta_id, item_kind, item_key) DO NOTHING",
            (
                delta_id,
                item.kind,
                item.key,
                item.ordinal,
                item.payload_hash,
                _encoded(item.payload),
                now,
                now,
            ),
        )

    rows = conn.execute(
        "SELECT item_kind, item_key, ordinal, payload_hash FROM memory_delta_items"
        " WHERE delta_id=? ORDER BY ordinal",
        (delta_id,),
    ).fetchall()
    actual = [(str(row[0]), str(row[1]), int(row[2]), str(row[3])) for row in rows]
    wanted = [(item.kind, item.key, item.ordinal, item.payload_hash) for item in expected]
    if actual != wanted:
        raise DeltaItemCollisionError(f"delta {delta_id} ledger does not match parent payload")
    return expected


def claim_next(
    conn: sqlite3.Connection,
    *,
    delta_id: int,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> ClaimedDeltaItem | None:
    """Claim the earliest non-applied item; never overtake a live predecessor."""
    ensure_schema(conn)
    if conn.in_transaction:
        raise RuntimeError("memory_delta item claim requires an autocommit connection")
    current = _now(now)
    now_text = _timestamp(current)
    lease_text = _timestamp(current + timedelta(seconds=max(1, int(lease_seconds))))
    token = uuid.uuid4().hex
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT item_kind, item_key, ordinal, payload_hash, payload, state,"
            " claim_token, lease_until, attempts FROM memory_delta_items"
            " WHERE delta_id=? AND state<>? ORDER BY ordinal LIMIT 1",
            (delta_id, STATE_APPLIED),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        state = str(row[5])
        lease_until = str(row[7] or "")
        if state == STATE_APPLYING:
            try:
                deadline = datetime.fromisoformat(lease_until).astimezone(UTC)
            except (TypeError, ValueError):
                deadline = datetime.min.replace(tzinfo=UTC)
            if deadline > current:
                conn.execute("COMMIT")
                return None
        changed = conn.execute(
            "UPDATE memory_delta_items SET state=?, claim_token=?, lease_until=?,"
            " attempts=attempts+1, error='', updated_at=?"
            " WHERE delta_id=? AND item_kind=? AND item_key=? AND state=? AND claim_token=?"
            " AND lease_until=?",
            (
                STATE_APPLYING,
                token,
                lease_text,
                now_text,
                delta_id,
                str(row[0]),
                str(row[1]),
                state,
                str(row[6] or ""),
                lease_until,
            ),
        )
        if changed.rowcount != 1:
            conn.execute("ROLLBACK")
            return None
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return ClaimedDeltaItem(
        kind=str(row[0]),
        key=str(row[1]),
        ordinal=int(row[2]),
        payload_hash=str(row[3]),
        payload=json.loads(str(row[4])),
        delta_id=delta_id,
        token=token,
        attempts=int(row[8] or 0) + 1,
    )


def mark_applied(
    conn: sqlite3.Connection,
    *,
    delta_id: int,
    item: ClaimedDeltaItem,
    effect_kind: str = "",
    effect_id: str = "",
    geometry_changed: bool | None = None,
) -> bool:
    ensure_schema(conn)
    changed = conn.execute(
        "UPDATE memory_delta_items SET state=?, claim_token='', lease_until='',"
        " effect_kind=?, effect_id=?, geometry_changed=?, error='', updated_at=?"
        " WHERE delta_id=? AND item_kind=? AND item_key=? AND state=? AND claim_token=?",
        (
            STATE_APPLIED,
            effect_kind,
            effect_id,
            None if geometry_changed is None else int(geometry_changed),
            _timestamp(),
            delta_id,
            item.kind,
            item.key,
            STATE_APPLYING,
            item.token,
        ),
    )
    return changed.rowcount == 1


def mark_failed(
    conn: sqlite3.Connection,
    *,
    delta_id: int,
    item: ClaimedDeltaItem,
    error: str,
) -> bool:
    ensure_schema(conn)
    changed = conn.execute(
        "UPDATE memory_delta_items SET state=?, claim_token='', lease_until='', error=?,"
        " updated_at=? WHERE delta_id=? AND item_kind=? AND item_key=?"
        " AND state=? AND claim_token=?",
        (
            STATE_FAILED,
            str(error or "")[:1000],
            _timestamp(),
            delta_id,
            item.kind,
            item.key,
            STATE_APPLYING,
            item.token,
        ),
    )
    return changed.rowcount == 1


def state_counts(conn: sqlite3.Connection, *, delta_id: int) -> dict[str, int]:
    ensure_schema(conn)
    counts = {state: 0 for state in (STATE_PENDING, STATE_APPLYING, STATE_APPLIED, STATE_FAILED)}
    for state, count in conn.execute(
        "SELECT state, COUNT(*) FROM memory_delta_items WHERE delta_id=? GROUP BY state",
        (delta_id,),
    ).fetchall():
        counts[str(state)] = int(count)
    return counts


def geometry_changed_for_delta(conn: sqlite3.Connection, *, delta_id: int) -> bool | None:
    """Return the durable geometry effect across applied items.

    ``None`` means at least one applied item predates the receipt or used the
    compatibility API without reporting its effect. Callers must treat that as
    unknown/conservatively changed, never as a proven no-op.
    """
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT geometry_changed FROM memory_delta_items WHERE delta_id=? AND state=?",
        (delta_id, STATE_APPLIED),
    ).fetchall()
    if not rows:
        return False
    values = [row[0] for row in rows]
    if any(value == 1 for value in values):
        return True
    if any(value is None for value in values):
        return None
    if any(value != 0 for value in values):
        raise RuntimeError(f"delta {delta_id} has an invalid geometry receipt")
    return False
