"""Durable identities for discrete event occurrences and their conservative series.

An occurrence is one event item emitted for one canonical session window.  A
series is deliberately narrower than a topic: only the normalized title plus
the sorted canonical participant set may make two occurrences siblings.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..capture.timestamps import parse_capture_timestamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_occurrences (
    occurrence_id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL,
    delta_id INTEGER,
    session_id TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    item_key TEXT NOT NULL,
    title TEXT NOT NULL,
    participants TEXT NOT NULL DEFAULT '[]',
    quote TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, window_start, window_end, item_key)
)
"""

_RECEIPT_RE = re.compile(r"^\u27e8([0-9a-f]{24}):event_occurrences\u27e9$")


@dataclass(frozen=True)
class EventOccurrence:
    occurrence_id: str
    series_id: str
    session_id: str
    window_start: str
    window_end: str
    item_key: str
    title: str
    participants: tuple[str, ...]
    quote: str
    confidence: float
    created_at: str
    delta_id: int | None = None

    @property
    def endpoint(self) -> str:
        return f"event:occurrence:{self.occurrence_id}"

    @property
    def source_receipt(self) -> str:
        return receipt_for(self.occurrence_id)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the owner-only occurrence projection without committing caller work."""
    from . import fts

    if fts.is_client_process():
        return
    conn.execute(SCHEMA)
    conn.execute(
        """CREATE INDEX IF NOT EXISTS ix_event_occurrences_series
        ON event_occurrences(series_id, window_start)"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS ix_event_occurrences_session
        ON event_occurrences(session_id, window_start)"""
    )


def _display_text(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def _identity_key(value: object) -> str:
    return _display_text(value).casefold()


def canonical_timestamp(value: datetime | str) -> str:
    if isinstance(value, datetime):
        parsed = value.astimezone() if value.tzinfo is None else value
    else:
        parsed = parse_capture_timestamp(value)
    if parsed is None:
        raise ValueError(f"invalid event occurrence timestamp: {value!r}")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


def canonical_participants(values: Iterable[object]) -> tuple[str, ...]:
    """Return a stable display tuple ordered by normalized canonical identity."""
    candidates: dict[str, set[str]] = {}
    for value in values:
        display = _display_text(value)
        key = display.casefold()
        if display and key:
            candidates.setdefault(key, set()).add(display)
    return tuple(min(candidates[key]) for key in sorted(candidates))


def make_series_id(title: str, participants: Iterable[object]) -> str:
    payload = {
        "title": _identity_key(title),
        "participants": sorted(
            {_identity_key(value) for value in participants if _identity_key(value)}
        ),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def make_item_key(
    *,
    title: str,
    participants: Iterable[object],
    quote: str = "",
    explicit: object = None,
) -> str:
    """Canonical item key used inside one modeling window.

    A future upstream may provide an explicit stable key.  Until then, quote is
    included so two same-title episodes in one window are not collapsed merely
    because they share participants.
    """
    if _display_text(explicit):
        payload: Any = {"explicit": _identity_key(explicit)}
    else:
        payload = {
            "title": _identity_key(title),
            "participants": sorted(
                {_identity_key(value) for value in participants if _identity_key(value)}
            ),
            "quote": _identity_key(quote),
        }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def make_occurrence_id(
    *,
    session_id: str,
    window_start: datetime | str,
    window_end: datetime | str,
    item_key: str,
) -> str:
    session = _display_text(session_id)
    item = _display_text(item_key)
    if not session or not item:
        raise ValueError("event occurrence session_id and item_key must be non-empty")
    payload = {
        "session_id": session,
        "window_start": canonical_timestamp(window_start),
        "window_end": canonical_timestamp(window_end),
        "item_key": item,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def receipt_for(occurrence_id: str) -> str:
    occurrence = str(occurrence_id or "").strip()
    if not re.fullmatch(r"[0-9a-f]{24}", occurrence):
        raise ValueError(f"invalid event occurrence id: {occurrence_id!r}")
    return f"\u27e8{occurrence}:event_occurrences\u27e9"


def parse_receipt(value: object) -> str | None:
    match = _RECEIPT_RE.fullmatch(str(value or "").strip())
    return match.group(1) if match else None


def upsert(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    window_start: datetime | str,
    window_end: datetime | str,
    item_key: str,
    title: str,
    participants: Iterable[object],
    quote: str = "",
    confidence: float = 0.5,
    delta_id: int | None = None,
) -> tuple[EventOccurrence, bool]:
    ensure_schema(conn)
    start = canonical_timestamp(window_start)
    end = canonical_timestamp(window_end)
    if end <= start:
        raise ValueError("event occurrence window_end must be after window_start")
    display_title = _display_text(title)
    if not display_title:
        raise ValueError("event occurrence title must be non-empty")
    people = canonical_participants(participants)
    conf = float(confidence)
    if not 0.0 <= conf <= 1.0:
        raise ValueError("event occurrence confidence must be in [0, 1]")
    series_id = make_series_id(display_title, people)
    occurrence_id = make_occurrence_id(
        session_id=session_id,
        window_start=start,
        window_end=end,
        item_key=item_key,
    )
    created_at = datetime.now(UTC).isoformat()
    cur = conn.execute(
        "INSERT INTO event_occurrences "
        "(occurrence_id, series_id, delta_id, session_id, window_start, window_end, "
        " item_key, title, participants, quote, confidence, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(occurrence_id) DO NOTHING",
        (
            occurrence_id,
            series_id,
            delta_id,
            _display_text(session_id),
            start,
            end,
            _display_text(item_key),
            display_title,
            json.dumps(people, ensure_ascii=False),
            _display_text(quote),
            conf,
            created_at,
        ),
    )
    occurrence = get(conn, occurrence_id)
    if occurrence is None:
        raise RuntimeError("event occurrence insert did not produce a readable row")
    expected = (
        series_id,
        _display_text(session_id),
        start,
        end,
        _display_text(item_key),
        display_title,
        people,
        _display_text(quote),
        conf,
        delta_id,
    )
    actual = (
        occurrence.series_id,
        occurrence.session_id,
        occurrence.window_start,
        occurrence.window_end,
        occurrence.item_key,
        occurrence.title,
        occurrence.participants,
        occurrence.quote,
        occurrence.confidence,
        occurrence.delta_id,
    )
    if actual != expected:
        raise RuntimeError("event occurrence identity maps to divergent payload")
    return occurrence, cur.rowcount > 0


def get(conn: sqlite3.Connection, occurrence_id: str) -> EventOccurrence | None:
    try:
        row = conn.execute(
            "SELECT * FROM event_occurrences WHERE occurrence_id = ? LIMIT 1",
            (str(occurrence_id or "").strip(),),
        ).fetchone()
    except sqlite3.Error:
        return None
    return _from_row(row) if row is not None else None


def recent(conn: sqlite3.Connection, *, limit: int = 500) -> list[EventOccurrence]:
    try:
        rows = conn.execute(
            "SELECT * FROM event_occurrences ORDER BY window_end DESC, occurrence_id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [_from_row(row) for row in rows]


def _from_row(row: sqlite3.Row | tuple[Any, ...]) -> EventOccurrence:
    if isinstance(row, sqlite3.Row):
        get_value = row.__getitem__
    else:
        columns = (
            "occurrence_id",
            "series_id",
            "delta_id",
            "session_id",
            "window_start",
            "window_end",
            "item_key",
            "title",
            "participants",
            "quote",
            "confidence",
            "created_at",
        )
        values = dict(zip(columns, row, strict=False))
        get_value = values.__getitem__
    try:
        raw_participants = json.loads(str(get_value("participants") or "[]"))
    except (TypeError, ValueError):
        raw_participants = []
    participants = (
        canonical_participants(raw_participants) if isinstance(raw_participants, list) else ()
    )
    return EventOccurrence(
        occurrence_id=str(get_value("occurrence_id")),
        series_id=str(get_value("series_id")),
        delta_id=(int(get_value("delta_id")) if get_value("delta_id") is not None else None),
        session_id=str(get_value("session_id")),
        window_start=str(get_value("window_start")),
        window_end=str(get_value("window_end")),
        item_key=str(get_value("item_key")),
        title=str(get_value("title")),
        participants=participants,
        quote=str(get_value("quote") or ""),
        confidence=float(get_value("confidence")),
        created_at=str(get_value("created_at")),
    )
