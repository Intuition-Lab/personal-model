"""Auditable, evidence-gated candidates for durable Point creation.

This store is deliberately independent from the Point writer.  A first
observation records a candidate and its evidence; it does not authorize a
durable entity or assertion.  Two distinct, known sessions promote the same
canonical candidate.  Multiple windows from one session remain useful audit
evidence but cannot satisfy that independence gate.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from ..capture.timestamps import parse_capture_timestamp

KIND_PERSON = "person"
KIND_ORG = "org"
KIND_PROJECT = "project"
KIND_ARTIFACT = "artifact"
KIND_ASSERTION = "assertion"
CANDIDATE_KINDS = frozenset({KIND_PERSON, KIND_ORG, KIND_PROJECT, KIND_ARTIFACT, KIND_ASSERTION})

STATUS_PENDING = "pending"
STATUS_REJECTED = "rejected"
STATUS_PROMOTED = "promoted"
STATUSES = frozenset({STATUS_PENDING, STATUS_REJECTED, STATUS_PROMOTED})

PROMOTION_SESSIONS = 2

DECISION_PROMOTE = "promote"
DECISION_REJECT = "reject"
OwnerDecision = Literal["promote", "reject"]

_UNKNOWN_SESSION_KEYS = frozenset({"unknown", "none", "null", "n/a", "na", "unspecified", "-"})
_KEY_RE = re.compile(r"^[0-9a-f]{32}$")
_EVIDENCE_RECEIPT_RE = re.compile(r"^\u27e8([0-9a-f]{32}):model_candidate_evidence\u27e9$")

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS model_candidates (
        candidate_key        TEXT PRIMARY KEY,
        candidate_kind       TEXT NOT NULL,
        subject              TEXT NOT NULL,
        text                 TEXT NOT NULL,
        canonical_subject    TEXT NOT NULL,
        canonical_text       TEXT NOT NULL,
        status               TEXT NOT NULL,
        evidence_count       INTEGER NOT NULL DEFAULT 0,
        independent_sessions INTEGER NOT NULL DEFAULT 0,
        first_seen_at         TEXT NOT NULL,
        last_seen_at          TEXT NOT NULL,
        promoted_at           TEXT,
        rejected_at           TEXT,
        decision_source       TEXT NOT NULL DEFAULT 'observation',
        CHECK(candidate_kind IN ('person','org','project','artifact','assertion')),
        CHECK(status IN ('pending','rejected','promoted'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_model_candidates_status
        ON model_candidates(status, last_seen_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS model_candidate_evidence (
        evidence_receipt TEXT PRIMARY KEY,
        candidate_key    TEXT NOT NULL,
        session_id       TEXT NOT NULL,
        window_start     TEXT NOT NULL,
        window_end       TEXT NOT NULL,
        source_kind      TEXT NOT NULL,
        quote            TEXT NOT NULL,
        confidence       REAL NOT NULL,
        payload_hash     TEXT NOT NULL,
        created_at       TEXT NOT NULL,
        UNIQUE(candidate_key, session_id, window_start, window_end),
        FOREIGN KEY(candidate_key) REFERENCES model_candidates(candidate_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_model_candidate_evidence_candidate
        ON model_candidate_evidence(candidate_key, created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS model_candidate_decisions (
        decision_id    TEXT PRIMARY KEY,
        candidate_key  TEXT NOT NULL,
        from_status    TEXT NOT NULL,
        to_status      TEXT NOT NULL,
        source_kind    TEXT NOT NULL,
        source_receipt TEXT NOT NULL,
        reason         TEXT NOT NULL DEFAULT '',
        created_at     TEXT NOT NULL,
        UNIQUE(candidate_key, source_kind, source_receipt),
        FOREIGN KEY(candidate_key) REFERENCES model_candidates(candidate_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_model_candidate_decisions_candidate
        ON model_candidate_decisions(candidate_key, created_at)
    """,
)


class CandidateCollisionError(RuntimeError):
    """A stable candidate/evidence/decision identity carried different payload."""


@dataclass(frozen=True)
class CandidateState:
    candidate_key: str
    candidate_kind: str
    subject: str
    text: str
    status: str
    evidence_count: int
    independent_sessions: int
    decision_source: str
    first_seen_at: str
    last_seen_at: str
    promoted_at: str | None = None
    rejected_at: str | None = None


@dataclass(frozen=True)
class RecordResult:
    state: CandidateState
    evidence_receipt: str
    evidence_created: bool
    promoted_now: bool


@dataclass(frozen=True)
class CandidateEvidence:
    evidence_receipt: str
    candidate_key: str
    session_id: str
    window_start: str
    window_end: str
    source_kind: str
    quote: str
    confidence: float
    created_at: str


@dataclass(frozen=True)
class CandidateDecision:
    decision_id: str
    candidate_key: str
    from_status: str
    to_status: str
    source_kind: str
    source_receipt: str
    reason: str
    created_at: str


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create owner-only candidate tables without committing caller work."""
    from . import fts

    if fts.is_client_process():
        return
    for statement in _SCHEMA_STATEMENTS:
        conn.execute(statement)


def canonical_text(value: object) -> str:
    """NFKC, whitespace-folded, case-insensitive candidate identity text."""
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.split()).casefold()


def _display_text(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.split())


def _candidate_payload(
    *, candidate_kind: object, subject: object, text: object
) -> tuple[str, str, str, str, str] | None:
    kind = canonical_text(candidate_kind)
    display_subject = _display_text(subject)
    display_text = _display_text(text)
    canonical_subject = canonical_text(display_subject)
    canonical_body = canonical_text(display_text)
    if (
        kind not in CANDIDATE_KINDS
        or not canonical_subject
        or not canonical_body
        or len(display_subject) > 500
        or len(display_text) > 4000
        or not any(character.isalnum() for character in display_subject)
        or not any(character.isalnum() for character in display_text)
    ):
        return None
    return kind, display_subject, display_text, canonical_subject, canonical_body


def make_candidate_key(*, candidate_kind: str, subject: str, text: str) -> str:
    """Return the stable identity for one exact canonical Point candidate."""
    payload = _candidate_payload(candidate_kind=candidate_kind, subject=subject, text=text)
    if payload is None:
        raise ValueError("invalid model candidate kind, subject, or text")
    kind, _subject, _text, canonical_subject, canonical_body = payload
    encoded = json.dumps(
        {"kind": kind, "subject": canonical_subject, "text": canonical_body},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def canonical_session_id(value: object) -> str | None:
    """Return a usable opaque session ID, rejecting explicit unknown sentinels."""
    session_id = _display_text(value)
    if not session_id or canonical_text(session_id) in _UNKNOWN_SESSION_KEYS:
        return None
    return session_id


def canonical_timestamp(value: datetime | str) -> str | None:
    if isinstance(value, datetime):
        parsed = value.astimezone() if value.tzinfo is None else value
    else:
        parsed = parse_capture_timestamp(value)
    if parsed is None:
        return None
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


def canonical_window(
    window_start: datetime | str, window_end: datetime | str
) -> tuple[str, str] | None:
    start = canonical_timestamp(window_start)
    end = canonical_timestamp(window_end)
    if start is None or end is None or end <= start:
        return None
    return start, end


def make_evidence_receipt(
    *,
    candidate_key: str,
    session_id: str,
    window_start: datetime | str,
    window_end: datetime | str,
) -> str:
    """One retry-stable receipt for a candidate in one canonical session window."""
    key = str(candidate_key or "").strip()
    session = canonical_session_id(session_id)
    window = canonical_window(window_start, window_end)
    if not _KEY_RE.fullmatch(key) or session is None or window is None:
        raise ValueError("invalid candidate key, session, or evidence window")
    start, end = window
    encoded = json.dumps(
        {
            "candidate_key": key,
            "session_id": session,
            "window_start": start,
            "window_end": end,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]
    return f"\u27e8{digest}:model_candidate_evidence\u27e9"


def parse_evidence_receipt(value: object) -> str | None:
    match = _EVIDENCE_RECEIPT_RE.fullmatch(str(value or "").strip())
    return match.group(1) if match else None


def _known_session(conn: sqlite3.Connection, session_id: str) -> bool:
    try:
        return (
            conn.execute("SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (session_id,)).fetchone()
            is not None
        )
    except sqlite3.Error:
        return False


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _state_from_row(row: sqlite3.Row | tuple) -> CandidateState:
    return CandidateState(
        candidate_key=str(row[0]),
        candidate_kind=str(row[1]),
        subject=str(row[2]),
        text=str(row[3]),
        status=str(row[4]),
        evidence_count=int(row[5]),
        independent_sessions=int(row[6]),
        first_seen_at=str(row[7]),
        last_seen_at=str(row[8]),
        promoted_at=str(row[9]) if row[9] is not None else None,
        rejected_at=str(row[10]) if row[10] is not None else None,
        decision_source=str(row[11]),
    )


_STATE_SELECT = (
    "candidate_key, candidate_kind, subject, text, status, evidence_count, "
    "independent_sessions, first_seen_at, last_seen_at, promoted_at, rejected_at, "
    "decision_source"
)


def _get_state(conn: sqlite3.Connection, candidate_key: str) -> CandidateState | None:
    row = conn.execute(
        f"SELECT {_STATE_SELECT} FROM model_candidates WHERE candidate_key = ? LIMIT 1",
        (candidate_key,),
    ).fetchone()
    return _state_from_row(row) if row is not None else None


def get(conn: sqlite3.Connection, candidate_key: str) -> CandidateState | None:
    ensure_schema(conn)
    return _get_state(conn, str(candidate_key or "").strip())


def find(
    conn: sqlite3.Connection, *, candidate_kind: str, subject: str, text: str
) -> CandidateState | None:
    try:
        key = make_candidate_key(candidate_kind=candidate_kind, subject=subject, text=text)
    except ValueError:
        return None
    return get(conn, key)


def promoted_assertions_for_subject(conn: sqlite3.Connection, subject: str) -> list[CandidateState]:
    """Return promoted assertion candidates waiting on or attached to a subject Point."""
    ensure_schema(conn)
    canonical_subject = canonical_text(subject)
    if not canonical_subject:
        return []
    rows = conn.execute(
        f"SELECT {_STATE_SELECT} FROM model_candidates "
        "WHERE candidate_kind=? AND canonical_subject=? AND status=? "
        "ORDER BY first_seen_at, candidate_key",
        (KIND_ASSERTION, canonical_subject, STATUS_PROMOTED),
    ).fetchall()
    return [_state_from_row(row) for row in rows]


def _ensure_candidate(
    conn: sqlite3.Connection,
    *,
    key: str,
    payload: tuple[str, str, str, str, str],
    now: str,
) -> tuple[CandidateState, bool]:
    kind, subject, text, canonical_subject, canonical_body = payload
    cur = conn.execute(
        "INSERT INTO model_candidates "
        "(candidate_key, candidate_kind, subject, text, canonical_subject, canonical_text, "
        " status, evidence_count, independent_sessions, first_seen_at, last_seen_at, "
        " promoted_at, rejected_at, decision_source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, NULL, NULL, 'observation') "
        "ON CONFLICT(candidate_key) DO NOTHING",
        (
            key,
            kind,
            subject,
            text,
            canonical_subject,
            canonical_body,
            STATUS_PENDING,
            now,
            now,
        ),
    )
    row = conn.execute(
        "SELECT candidate_kind, canonical_subject, canonical_text "
        "FROM model_candidates WHERE candidate_key = ?",
        (key,),
    ).fetchone()
    if row is None:
        raise RuntimeError("candidate insert did not produce a readable row")
    expected = (kind, canonical_subject, canonical_body)
    actual = tuple(str(value) for value in row)
    if actual != expected:
        raise CandidateCollisionError("candidate key resolved to divergent canonical payload")
    state = _get_state(conn, key)
    assert state is not None
    return state, cur.rowcount > 0


def _evidence_payload_hash(*, source_kind: str, quote: str, confidence: float) -> str:
    encoded = json.dumps(
        {"source_kind": source_kind, "quote": quote, "confidence": confidence},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _insert_decision(
    conn: sqlite3.Connection,
    *,
    candidate_key: str,
    from_status: str,
    to_status: str,
    source_kind: str,
    source_receipt: str,
    reason: str,
    now: str,
) -> None:
    payload = {
        "candidate_key": candidate_key,
        "from_status": from_status,
        "to_status": to_status,
        "source_kind": source_kind,
        "source_receipt": source_receipt,
        "reason": reason,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    decision_id = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]
    conn.execute(
        "INSERT INTO model_candidate_decisions "
        "(decision_id, candidate_key, from_status, to_status, source_kind, "
        " source_receipt, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(candidate_key, source_kind, source_receipt) DO NOTHING",
        (
            decision_id,
            candidate_key,
            from_status,
            to_status,
            source_kind,
            source_receipt,
            reason,
            now,
        ),
    )
    row = conn.execute(
        "SELECT decision_id, from_status, to_status, reason "
        "FROM model_candidate_decisions WHERE candidate_key = ? "
        "AND source_kind = ? AND source_receipt = ?",
        (candidate_key, source_kind, source_receipt),
    ).fetchone()
    expected = (decision_id, from_status, to_status, reason)
    actual = tuple(str(value) for value in row) if row is not None else ()
    if actual != expected:
        raise CandidateCollisionError("decision receipt resolved to divergent payload")


def record_evidence(
    conn: sqlite3.Connection,
    *,
    candidate_kind: str,
    subject: str,
    text: str,
    session_id: str,
    window_start: datetime | str,
    window_end: datetime | str,
    quote: str,
    confidence: float,
    source_kind: str = "memory_delta",
) -> RecordResult | None:
    """Record one window and promote only after two distinct known sessions.

    Invalid or unknown session context returns ``None`` without creating a
    candidate.  An exact retry returns the same receipt with
    ``evidence_created=False``.  Reusing that stable receipt with a different
    payload raises :class:`CandidateCollisionError`.
    """
    payload = _candidate_payload(candidate_kind=candidate_kind, subject=subject, text=text)
    session = canonical_session_id(session_id)
    window = canonical_window(window_start, window_end)
    evidence = str(quote or "").strip()
    source = canonical_text(source_kind)
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        return None
    if (
        payload is None
        or session is None
        or window is None
        or not evidence
        or not source
        or len(source) > 80
        or not 0.0 <= conf <= 1.0
        or not _known_session(conn, session)
    ):
        return None

    start, end = window
    key = make_candidate_key(candidate_kind=payload[0], subject=payload[1], text=payload[2])
    receipt = make_evidence_receipt(
        candidate_key=key,
        session_id=session,
        window_start=start,
        window_end=end,
    )
    evidence_hash = _evidence_payload_hash(
        source_kind=source,
        quote=evidence,
        confidence=conf,
    )
    now = _now()
    ensure_schema(conn)
    savepoint = "model_candidate_record"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        state, candidate_created = _ensure_candidate(conn, key=key, payload=payload, now=now)
        cur = conn.execute(
            "INSERT INTO model_candidate_evidence "
            "(evidence_receipt, candidate_key, session_id, window_start, window_end, "
            " source_kind, quote, confidence, payload_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(evidence_receipt) DO NOTHING",
            (
                receipt,
                key,
                session,
                start,
                end,
                source,
                evidence,
                conf,
                evidence_hash,
                now,
            ),
        )
        row = conn.execute(
            "SELECT candidate_key, session_id, window_start, window_end, source_kind, quote, "
            "confidence, payload_hash FROM model_candidate_evidence "
            "WHERE evidence_receipt = ?",
            (receipt,),
        ).fetchone()
        expected = (key, session, start, end, source, evidence, conf, evidence_hash)
        actual = tuple(row) if row is not None else ()
        if actual != expected:
            raise CandidateCollisionError("evidence receipt resolved to divergent payload")

        if candidate_created:
            _insert_decision(
                conn,
                candidate_key=key,
                from_status="",
                to_status=STATUS_PENDING,
                source_kind="first_observation",
                source_receipt=receipt,
                reason="first grounded observation",
                now=now,
            )

        evidence_count, session_count = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT session_id) "
            "FROM model_candidate_evidence WHERE candidate_key = ?",
            (key,),
        ).fetchone()
        promoted_now = state.status == STATUS_PENDING and int(session_count) >= PROMOTION_SESSIONS
        next_status = STATUS_PROMOTED if promoted_now else state.status
        conn.execute(
            "UPDATE model_candidates SET evidence_count = ?, independent_sessions = ?, "
            "last_seen_at = CASE WHEN ? THEN ? ELSE last_seen_at END, status = ?, "
            "promoted_at = CASE WHEN ? THEN COALESCE(promoted_at, ?) ELSE promoted_at END, "
            "decision_source = CASE WHEN ? THEN 'session_threshold' ELSE decision_source END "
            "WHERE candidate_key = ?",
            (
                int(evidence_count),
                int(session_count),
                1 if cur.rowcount > 0 else 0,
                now,
                next_status,
                1 if promoted_now else 0,
                now,
                1 if promoted_now else 0,
                key,
            ),
        )
        if promoted_now:
            _insert_decision(
                conn,
                candidate_key=key,
                from_status=STATUS_PENDING,
                to_status=STATUS_PROMOTED,
                source_kind="session_threshold",
                source_receipt=receipt,
                reason=f"observed in {PROMOTION_SESSIONS} independent sessions",
                now=now,
            )
        updated = _get_state(conn, key)
        assert updated is not None
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return RecordResult(
            state=updated,
            evidence_receipt=receipt,
            evidence_created=cur.rowcount > 0,
            promoted_now=promoted_now,
        )
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def record_owner_decision(
    conn: sqlite3.Connection,
    *,
    candidate_kind: str,
    subject: str,
    text: str,
    decision: OwnerDecision,
    source_receipt: str,
    reason: str = "",
) -> CandidateState | None:
    """Apply an explicit owner promote/reject decision without session quorum.

    This is intentionally a separate API from inferred observations.  Callers
    must supply a durable owner-edit receipt; an empty receipt fails closed.
    """
    payload = _candidate_payload(candidate_kind=candidate_kind, subject=subject, text=text)
    owner_receipt = str(source_receipt or "").strip()
    owner_reason = str(reason or "").strip()[:1000]
    if payload is None or decision not in {DECISION_PROMOTE, DECISION_REJECT} or not owner_receipt:
        return None
    target = STATUS_PROMOTED if decision == DECISION_PROMOTE else STATUS_REJECTED
    key = make_candidate_key(candidate_kind=payload[0], subject=payload[1], text=payload[2])
    now = _now()
    ensure_schema(conn)
    savepoint = "model_candidate_owner_decision"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        state, created = _ensure_candidate(conn, key=key, payload=payload, now=now)
        existing_decision = conn.execute(
            "SELECT to_status, reason FROM model_candidate_decisions "
            "WHERE candidate_key = ? AND source_kind = 'owner_explicit' "
            "AND source_receipt = ? LIMIT 1",
            (key, owner_receipt),
        ).fetchone()
        if existing_decision is not None:
            if (str(existing_decision[0]), str(existing_decision[1])) != (target, owner_reason):
                raise CandidateCollisionError(
                    "owner decision receipt resolved to divergent payload"
                )
            # A replay of an older owner receipt must be idempotent, not undo a
            # newer explicit decision that moved the candidate again.
            current = _get_state(conn, key)
            assert current is not None
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            return current
        prior = "" if created else state.status
        _insert_decision(
            conn,
            candidate_key=key,
            from_status=prior,
            to_status=target,
            source_kind="owner_explicit",
            source_receipt=owner_receipt,
            reason=owner_reason,
            now=now,
        )
        conn.execute(
            "UPDATE model_candidates SET status = ?, last_seen_at = ?, "
            "promoted_at = CASE WHEN ? THEN COALESCE(promoted_at, ?) ELSE promoted_at END, "
            "rejected_at = CASE WHEN ? THEN COALESCE(rejected_at, ?) ELSE rejected_at END, "
            "decision_source = 'owner_explicit' WHERE candidate_key = ?",
            (
                target,
                now,
                1 if target == STATUS_PROMOTED else 0,
                now,
                1 if target == STATUS_REJECTED else 0,
                now,
                key,
            ),
        )
        updated = _get_state(conn, key)
        assert updated is not None
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return updated
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def evidence_for(conn: sqlite3.Connection, candidate_key: str) -> list[CandidateEvidence]:
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT evidence_receipt, candidate_key, session_id, window_start, window_end, "
        "source_kind, quote, confidence, created_at FROM model_candidate_evidence "
        "WHERE candidate_key = ? ORDER BY created_at, evidence_receipt",
        (str(candidate_key or "").strip(),),
    ).fetchall()
    return [
        CandidateEvidence(
            evidence_receipt=str(row[0]),
            candidate_key=str(row[1]),
            session_id=str(row[2]),
            window_start=str(row[3]),
            window_end=str(row[4]),
            source_kind=str(row[5]),
            quote=str(row[6]),
            confidence=float(row[7]),
            created_at=str(row[8]),
        )
        for row in rows
    ]


def decisions_for(conn: sqlite3.Connection, candidate_key: str) -> list[CandidateDecision]:
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT decision_id, candidate_key, from_status, to_status, source_kind, "
        "source_receipt, reason, created_at FROM model_candidate_decisions "
        "WHERE candidate_key = ? ORDER BY created_at, decision_id",
        (str(candidate_key or "").strip(),),
    ).fetchall()
    return [
        CandidateDecision(
            decision_id=str(row[0]),
            candidate_key=str(row[1]),
            from_status=str(row[2]),
            to_status=str(row[3]),
            source_kind=str(row[4]),
            source_receipt=str(row[5]),
            reason=str(row[6]),
            created_at=str(row[7]),
        )
        for row in rows
    ]
