"""DAO for the horizontal relation layer of the user-centric memory graph.

Implements the predicate closed set, ``src×dst`` completeness table, schema,
write entrances, and an as-of-T read helper.

**Why a separate table (§2.5/§2.6).** evomem (``evo_nodes`` + SUPERSEDE chains) is the
*vertical / temporal* axis — each node's own version history. This table is the ORTHOGONAL
*horizontal / relational* axis: first-class **directed relation edges BETWEEN entities**
(person / org / project / event / artifact). An edge addresses stable *identities*
(person_graph canonical name / project slug), NEVER a specific version node — evomem resolves
an identity to its as-of-T state (that is the §2.5 interface; not this module's job).

**Persistence discipline (§2.6, §5) — this table is a persistent (bitemporal) graph.**

- *append-only*: rows are never physically deleted. A relationship ending is
  :func:`close_edge` (stamps ``valid_to``), and ``created_at`` (transaction time) is immutable.
- *two time dimensions*: ``created_at`` = when Persome learned the edge (the persistence /
  version axis, monotonic); ``valid_from`` / ``valid_to`` = when the fact holds in the world
  (valid-time). :func:`edges_as_of` filters on **valid-time**.

**Default is inert.** :func:`add_edge` writes ``status='shadow'`` by default, so extracted
edges reach neither retrieval nor the digest until proven (§4.3). This module wires no
retrieval; ``edges_as_of`` is the read primitive P0-3 / P1 build on.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from ..evomem.models import MemoryStatus
from ..logger import get

logger = get("persome.store.relation_edges")


class RelationEdgeMigrationError(RuntimeError):
    """The legacy edge set cannot be made canonically unique without adjudication."""


class RelationEdgeEffectConflict(RuntimeError):
    """One effect key was reused for a different logical relation edge."""


@dataclass(frozen=True)
class EndEdgeEffectResult:
    """Outcome of one receipt-backed create/reinforce-and-close operation."""

    edge_id: str
    created: bool
    applied: bool


class EntityKind(StrEnum):
    """Closed entity-kind set used to validate relation endpoints."""

    SELF = "self"
    PERSON = "person"
    ORG = "org"
    PROJECT = "project"
    EVENT = "event"
    ARTIFACT = "artifact"


class Predicate(StrEnum):
    """Closed relation predicates with an open-text label for finer semantics.

    ``engaged_with`` is the dense, kind-independent co-occurrence floor. The
    remaining predicates add typed semantic structure without determining graph
    connectivity.
    """

    ENGAGED_WITH = "engaged_with"
    PARTICIPATES_IN = "participates_in"
    PART_OF = "part_of"
    REPORTS_TO = "reports_to"
    KNOWS = "knows"
    ABOUT = "about"
    DEPENDS_ON = "depends_on"


PROVENANCE: frozenset[str] = frozenset({"user_committed", "inferred"})

_K = EntityKind


def _pairs(
    srcs: set[EntityKind], dsts: set[EntityKind]
) -> frozenset[tuple[EntityKind, EntityKind]]:
    return frozenset((s, d) for s in srcs for d in dsts)


_ALL_DST = {_K.PERSON, _K.ORG, _K.PROJECT, _K.EVENT, _K.ARTIFACT}
_LEGAL_ENDPOINTS: dict[Predicate, frozenset[tuple[EntityKind, EntityKind]]] = {
    Predicate.ENGAGED_WITH: _pairs({_K.SELF, _K.PERSON, _K.ORG}, _ALL_DST),
    Predicate.PARTICIPATES_IN: _pairs({_K.SELF, _K.PERSON, _K.ORG}, {_K.PROJECT, _K.EVENT}),
    Predicate.PART_OF: (
        _pairs({_K.SELF, _K.PERSON, _K.ORG}, {_K.ORG})
        | _pairs({_K.PROJECT}, {_K.ORG})
        | _pairs({_K.ARTIFACT}, {_K.PROJECT})
    ),
    Predicate.REPORTS_TO: _pairs({_K.SELF, _K.PERSON}, {_K.PERSON}),
    Predicate.KNOWS: _pairs({_K.SELF, _K.PERSON}, {_K.PERSON}),
    Predicate.ABOUT: _pairs({_K.EVENT, _K.ARTIFACT}, {_K.PROJECT, _K.PERSON, _K.ORG, _K.EVENT}),
    Predicate.DEPENDS_ON: _pairs(
        {_K.PROJECT, _K.EVENT, _K.ARTIFACT}, {_K.PROJECT, _K.EVENT, _K.ARTIFACT}
    ),
}


_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS relation_edges (
    edge_id      TEXT PRIMARY KEY,
    edge_key     TEXT NOT NULL,          -- canonical logical identity; not a display label
    src_identity TEXT NOT NULL,          -- stable canonical identity, never a version node ID
    dst_identity TEXT NOT NULL,
    predicate    TEXT NOT NULL,          -- one closed-set predicate
    label        TEXT,                   -- open-text relation semantics
    valid_from   TEXT NOT NULL,          -- validity start in ISO 8601
    valid_to     TEXT,                   -- NULL while currently valid
    provenance   TEXT NOT NULL,          -- 'user_committed' | 'inferred'
    confidence   REAL NOT NULL,
    quote        TEXT,                   -- short source excerpt supporting the relation
    status       TEXT NOT NULL,          -- MemoryStatus: 'shadow'|'active'|'superseded'|'archived'
    created_at   TEXT NOT NULL,          -- immutable transaction time
    observations INTEGER NOT NULL DEFAULT 1,  -- monotone supporting-evidence count
    last_observed_at TEXT,               -- latest reinforcement in ISO 8601
    recall_count INTEGER NOT NULL DEFAULT 0  -- increments when a delivered chain uses this edge
)
"""

_ORDINARY_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_edges_src ON relation_edges(src_identity, valid_from)",
    "CREATE INDEX IF NOT EXISTS ix_edges_dst ON relation_edges(dst_identity, valid_from)",
)
_OPEN_EDGE_INDEX = "uq_relation_edges_open_edge_key"
_OPEN_EDGE_INDEX_SQL = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {_OPEN_EDGE_INDEX}
ON relation_edges(edge_key)
WHERE valid_to IS NULL AND status IN ('active', 'shadow')
"""
_EFFECT_SCHEMA = """
CREATE TABLE IF NOT EXISTS relation_edge_effects (
    effect_key           TEXT PRIMARY KEY,
    edge_id              TEXT NOT NULL,
    edge_key             TEXT NOT NULL,
    applied_observations INTEGER NOT NULL CHECK (applied_observations = 1),
    created_at           TEXT NOT NULL
)
"""
_EFFECT_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_relation_edge_effects_edge_id ON relation_edge_effects(edge_id)"
)

# Kept as the importable base schema string. ``ensure_schema`` installs the
# partial unique index only after its fail-closed legacy-data preflight.
SCHEMA = ";\n".join((_TABLE_SCHEMA.strip(), *_ORDINARY_INDEXES)) + ";\n"


# stamp neutral '0'; the LLM relation pass may stamp ± when the quote carries

POLARITIES = frozenset({"+", "-", "0"})

# Columns added after the first shipped schema — ensure_schema back-fills them on old DBs.
_EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (
    # Nullable only for ALTER TABLE compatibility. Fresh databases use the
    # NOT NULL declaration above, and the migration fills every legacy row
    # before installing the partial unique index.
    ("edge_key", "TEXT"),
    ("observations", "INTEGER NOT NULL DEFAULT 1"),
    ("last_observed_at", "TEXT"),
    ("recall_count", "INTEGER NOT NULL DEFAULT 0"),
    # §7-6 graph-projection axes: kinds were validated at add_edge but never
    # be recovered from the table; polarity had no storage at all.
    ("src_kind", "TEXT"),
    ("dst_kind", "TEXT"),
    ("polarity", "TEXT NOT NULL DEFAULT '0'"),
    # Evidence handle for model exports. Nullable keeps old non-activity
    # extractors compatible; when one field is supplied, add_edge requires all
    # three so an exported line never carries a half-formed receipt.
    ("source_kind", "TEXT"),
    ("source_id", "TEXT"),
    ("source_receipt", "TEXT"),
)


def canonical_edge_parts(
    src_identity: str, dst_identity: str, predicate: str | Predicate
) -> tuple[str, str, str]:
    """Return the shared logical identity parts for one relation.

    Activity identities retain the existing ``event:<id>`` compatibility
    normalization. ``knows`` is symmetric, so endpoint order is irrelevant;
    every other predicate remains directed.
    """
    from ..model.activity_source import normalize_activity_identity

    pred = Predicate(str(predicate)).value
    src = normalize_activity_identity(str(src_identity).strip())
    dst = normalize_activity_identity(str(dst_identity).strip())
    if not src or not dst:
        raise ValueError("relation_edges: src_identity / dst_identity must be non-empty")
    if pred == Predicate.KNOWS.value:
        src, dst = sorted((src, dst))
    return src, dst, pred


def canonical_edge_key(src_identity: str, dst_identity: str, predicate: str | Predicate) -> str:
    """Return a collision-safe, stable database key for a logical relation."""
    return json.dumps(
        canonical_edge_parts(src_identity, dst_identity, predicate),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _migration_rows(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    previous_factory = conn.row_factory
    conn.row_factory = None
    try:
        return list(
            conn.execute(
                "SELECT edge_id, src_identity, dst_identity, predicate, status, valid_to,"
                " observations, edge_key FROM relation_edges ORDER BY edge_id"
            ).fetchall()
        )
    finally:
        conn.row_factory = previous_factory


def _backfill_canonical_keys(conn: sqlite3.Connection) -> None:
    """Fill legacy keys, refusing ambiguous open rows instead of merging them."""
    updates: list[tuple[str, str]] = []
    open_rows: dict[str, list[tuple[str, str, int]]] = {}
    invalid: list[tuple[str, str]] = []
    for edge_id, src, dst, predicate, status, valid_to, observations, stored_key in _migration_rows(
        conn
    ):
        eid = str(edge_id)
        try:
            key = canonical_edge_key(str(src), str(dst), str(predicate))
        except ValueError as exc:
            invalid.append((eid, str(exc)))
            continue
        if stored_key != key:
            updates.append((key, eid))
        if valid_to is None and str(status) in {
            MemoryStatus.ACTIVE.value,
            MemoryStatus.SHADOW.value,
        }:
            open_rows.setdefault(key, []).append((eid, str(status), int(observations or 1)))

    collisions = {key: rows for key, rows in open_rows.items() if len(rows) > 1}
    if invalid or collisions:
        details: list[str] = []
        for edge_id, reason in invalid[:10]:
            details.append(f"invalid edge_id={edge_id}: {reason}")
        for key, rows in list(collisions.items())[:10]:
            members = ", ".join(
                f"{edge_id}(status={status}, observations={observations})"
                for edge_id, status, observations in rows
            )
            details.append(f"edge_key={key}: {members}")
        omitted = len(invalid) + len(collisions) - len(details)
        if omitted > 0:
            details.append(f"... and {omitted} more conflict groups")
        raise RelationEdgeMigrationError(
            "relation_edges canonical-key migration refused: existing open edges conflict; "
            "no rows were merged and observations were not summed. " + "; ".join(details)
        )

    if updates:
        conn.executemany("UPDATE relation_edges SET edge_key=? WHERE edge_id=?", updates)


def ensure_schema(conn: sqlite3.Connection) -> None:
    from . import fts

    if fts.is_client_process():
        return
    savepoint = f"relation_edges_schema_{uuid.uuid4().hex}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        conn.execute(_TABLE_SCHEMA)
        for statement in _ORDINARY_INDEXES:
            conn.execute(statement)
        have = {row[1] for row in conn.execute("PRAGMA table_info(relation_edges)").fetchall()}
        for name, decl in _EXTRA_COLUMNS:
            if name not in have:
                conn.execute(f"ALTER TABLE relation_edges ADD COLUMN {name} {decl}")

        index_exists = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                (_OPEN_EDGE_INDEX,),
            ).fetchone()
            is not None
        )
        if not index_exists:
            _backfill_canonical_keys(conn)
            conn.execute(_OPEN_EDGE_INDEX_SQL)
        conn.execute(_EFFECT_SCHEMA)
        conn.execute(_EFFECT_INDEX)
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        logger.error("relation_edges schema migration failed closed", exc_info=True)
        raise


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_effect_key(effect_key: str | None) -> str | None:
    if effect_key is None:
        return None
    key = str(effect_key).strip()
    if not key:
        raise ValueError("relation_edges: effect_key must be non-empty when supplied")
    return key


def effect_already_applied(
    conn: sqlite3.Connection,
    *,
    effect_key: str,
    edge_key: str,
) -> bool:
    """Return whether one effect receipt already belongs to this logical Line.

    The receipt outlives a physical validity interval. That is deliberate: a
    retry after the Line closed is still the same effect and must not create a
    replacement interval. Reusing an effect key for another logical Line fails
    closed instead of silently stealing the receipt.
    """
    ensure_schema(conn)
    effect = _normalize_effect_key(effect_key)
    assert effect is not None
    receipt = conn.execute(
        "SELECT edge_key FROM relation_edge_effects WHERE effect_key=?",
        (effect,),
    ).fetchone()
    if receipt is None:
        return False
    stored_key = str(receipt[0])
    if stored_key != str(edge_key):
        raise RelationEdgeEffectConflict(
            f"relation_edges: effect_key {effect!r} already belongs to "
            f"edge_key {stored_key!r}, not {edge_key!r}"
        )
    return True


@contextmanager
def _effect_transaction(conn: sqlite3.Connection):
    """Own one atomic receipt+edge mutation while preserving DAO commit semantics."""
    savepoint = f"relation_edge_effect_{uuid.uuid4().hex}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        yield
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    else:
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        # The historical DAO commits every successful write. If the caller had
        # an outer transaction this deliberately preserves that existing API.
        conn.commit()


def add_edge(
    conn: sqlite3.Connection,
    *,
    src_identity: str,
    dst_identity: str,
    predicate: str | Predicate,
    src_kind: str | EntityKind,
    dst_kind: str | EntityKind,
    provenance: str,
    confidence: float,
    label: str | None = None,
    quote: str | None = None,
    valid_from: str | None = None,
    status: str | MemoryStatus = MemoryStatus.SHADOW,
    created_at: str | None = None,
    edge_id: str | None = None,
    observations: int = 1,
    polarity: str = "0",
    source_kind: str | None = None,
    source_id: str | None = None,
    source_receipt: str | None = None,
    effect_key: str | None = None,
) -> str:
    """Append one relation edge. Returns its ``edge_id``.

    Deterministic, no LLM. Every input is validated against the §4.2 closed sets — an
    illegal predicate, an illegal ``(src_kind, dst_kind)`` for that predicate, an unknown
    provenance, an out-of-range confidence, or an empty identity all raise ``ValueError``
    (the caller is expected to have made a decision; we do not silently coerce).

    Defaults to ``status='shadow'`` so the edge is inert until proven (§4.3).
    ``effect_key`` is the exactly-once receipt for an initial additive effect:
    the edge and receipt are committed atomically. It must identify this one
    canonical relation effect, not a window containing several relations.
    """
    ensure_schema(conn)

    # Closed-set validation (§4.2) — StrEnum(...) raises ValueError for anything off-set.
    pred = Predicate(str(predicate))
    sk = EntityKind(str(src_kind))
    dk = EntityKind(str(dst_kind))
    if (sk, dk) not in _LEGAL_ENDPOINTS[pred]:
        raise ValueError(
            f"relation_edges: illegal endpoints {sk.value}->{dk.value} for predicate "
            f"{pred.value} (§4.2 completeness table)"
        )
    prov = str(provenance)
    if prov not in PROVENANCE:
        raise ValueError(
            f"relation_edges: unknown provenance {prov!r} (expected one of {sorted(PROVENANCE)})"
        )
    conf = float(confidence)
    if not 0.0 <= conf <= 1.0:
        raise ValueError(f"relation_edges: confidence {conf} out of [0,1]")
    src = str(src_identity).strip()
    dst = str(dst_identity).strip()
    if not src or not dst:
        raise ValueError("relation_edges: src_identity / dst_identity must be non-empty")
    edge_key = canonical_edge_key(src, dst, pred)
    st = MemoryStatus(str(status))
    effect = _normalize_effect_key(effect_key)

    obs = int(observations)
    if obs < 1:
        raise ValueError(f"relation_edges: observations {obs} must be >= 1")
    if effect is not None:
        if obs != 1:
            raise ValueError(
                "relation_edges: an effect-keyed initial edge must represent exactly "
                "one observation"
            )
        if st not in {MemoryStatus.ACTIVE, MemoryStatus.SHADOW}:
            raise ValueError("relation_edges: effect_key requires an open active/shadow edge")
    pol = str(polarity)
    if pol not in POLARITIES:
        raise ValueError(f"relation_edges: polarity {pol!r} not in {sorted(POLARITIES)}")
    source = tuple(
        str(value).strip() if value is not None else ""
        for value in (source_kind, source_id, source_receipt)
    )
    if any(source) and not all(source):
        raise ValueError(
            "relation_edges: source_kind, source_id, and source_receipt must be supplied together"
        )

    eid = edge_id or uuid.uuid4().hex
    vf = valid_from or _now_iso()
    ts = created_at or _now_iso()
    insert_sql = """
        INSERT INTO relation_edges
            (edge_id, edge_key, src_identity, dst_identity, predicate, label, valid_from,
             valid_to, provenance, confidence, quote, status, created_at, observations,
             src_kind, dst_kind, polarity, last_observed_at, source_kind, source_id,
             source_receipt)
        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    insert_params = (
        eid,
        edge_key,
        src,
        dst,
        pred.value,
        label,
        vf,
        prov,
        conf,
        quote,
        st.value,
        ts,
        obs,
        sk.value,
        dk.value,
        pol,
        vf,  # birth stamp: last observed = the first evidence moment
        source[0] or None,
        source[1] or None,
        source[2] or None,
    )
    if effect is None:
        conn.execute(insert_sql, insert_params)
        conn.commit()
    else:
        with _effect_transaction(conn):
            conn.execute(insert_sql, insert_params)
            conn.execute(
                "INSERT INTO relation_edge_effects"
                " (effect_key, edge_id, edge_key, applied_observations, created_at)"
                " VALUES (?, ?, ?, 1, ?)",
                (effect, eid, edge_key, ts),
            )
    return eid


def find_open_edge(conn: sqlite3.Connection, *, edge_key: str) -> str | None:
    """Resolve one canonical key to its open active/shadow row, if present."""
    ensure_schema(conn)
    row = conn.execute(
        "SELECT edge_id FROM relation_edges WHERE edge_key=? AND valid_to IS NULL"
        " AND status IN ('active','shadow')",
        (str(edge_key),),
    ).fetchone()
    return str(row[0]) if row is not None else None


def close_edge(conn: sqlite3.Connection, *, edge_id: str, at: str | None = None) -> bool:
    """Close a relation's valid-time interval (stamp ``valid_to``). Append-only: it fires once
    (``WHERE valid_to IS NULL`` refuses a re-close / reopen) and never touches ``created_at``.
    Together with :func:`reinforce_edge` these are the only TWO mutations this table allows —
    both monotone (close happens once; observations only grow). Returns whether a row closed.
    """
    ensure_schema(conn)
    ts = at or _now_iso()
    cur = conn.execute(
        "UPDATE relation_edges SET valid_to = ? WHERE edge_id = ? AND valid_to IS NULL",
        (ts, edge_id),
    )
    conn.commit()
    return cur.rowcount > 0


def end_edge_with_effect(
    conn: sqlite3.Connection,
    *,
    src_identity: str,
    dst_identity: str,
    predicate: str | Predicate,
    src_kind: str | EntityKind,
    dst_kind: str | EntityKind,
    provenance: str,
    confidence: float,
    effect_key: str,
    label: str | None = None,
    quote: str | None = None,
    status: str | MemoryStatus = MemoryStatus.SHADOW,
    polarity: str = "0",
    at: str | None = None,
) -> EndEdgeEffectResult:
    """Apply one relation-ending item exactly once.

    The effect receipt is claimed in the same transaction that either closes
    the existing open logical Line or creates its closed historical interval.
    A retry therefore cannot manufacture a second closed row. If a process
    dies after an earlier open-edge upsert but before this call, the next retry
    closes that existing row safely.
    """
    ensure_schema(conn)
    pred = Predicate(str(predicate))
    sk = EntityKind(str(src_kind))
    dk = EntityKind(str(dst_kind))
    if (sk, dk) not in _LEGAL_ENDPOINTS[pred]:
        raise ValueError(
            f"relation_edges: illegal endpoints {sk.value}->{dk.value} for predicate "
            f"{pred.value} (§4.2 completeness table)"
        )
    prov = str(provenance)
    if prov not in PROVENANCE:
        raise ValueError(
            f"relation_edges: unknown provenance {prov!r} (expected one of {sorted(PROVENANCE)})"
        )
    conf = float(confidence)
    if not 0.0 <= conf <= 1.0:
        raise ValueError(f"relation_edges: confidence {conf} out of [0,1]")
    src = str(src_identity).strip()
    dst = str(dst_identity).strip()
    if not src or not dst:
        raise ValueError("relation_edges: src_identity / dst_identity must be non-empty")
    st = MemoryStatus(str(status))
    if st not in {MemoryStatus.ACTIVE, MemoryStatus.SHADOW}:
        raise ValueError("relation_edges: ended effect requires active/shadow status")
    pol = str(polarity)
    if pol not in POLARITIES:
        raise ValueError(f"relation_edges: polarity {pol!r} not in {sorted(POLARITIES)}")
    effect = _normalize_effect_key(effect_key)
    assert effect is not None
    logical_key = canonical_edge_key(src, dst, pred)
    ts = at or _now_iso()

    with _effect_transaction(conn):
        receipt = conn.execute(
            "SELECT edge_id, edge_key FROM relation_edge_effects WHERE effect_key=?",
            (effect,),
        ).fetchone()
        if receipt is not None:
            if str(receipt[1]) != logical_key:
                raise RelationEdgeEffectConflict(
                    f"relation_edges: effect_key {effect!r} already belongs to "
                    f"edge_key {receipt[1]!r}, not {logical_key!r}"
                )
            return EndEdgeEffectResult(str(receipt[0]), created=False, applied=False)

        open_row = conn.execute(
            "SELECT edge_id FROM relation_edges WHERE edge_key=? AND valid_to IS NULL"
            " AND status IN ('active','shadow')",
            (logical_key,),
        ).fetchone()
        edge_id = str(open_row[0]) if open_row is not None else uuid.uuid4().hex
        claimed = conn.execute(
            "INSERT OR IGNORE INTO relation_edge_effects"
            " (effect_key, edge_id, edge_key, applied_observations, created_at)"
            " VALUES (?, ?, ?, 1, ?)",
            (effect, edge_id, logical_key, ts),
        )
        if claimed.rowcount != 1:
            # A concurrent writer won after our initial read. SQLite serializes
            # the INSERT; resolve and validate its durable receipt.
            receipt = conn.execute(
                "SELECT edge_id, edge_key FROM relation_edge_effects WHERE effect_key=?",
                (effect,),
            ).fetchone()
            if receipt is None or str(receipt[1]) != logical_key:
                raise RelationEdgeEffectConflict(
                    f"relation_edges: effect_key {effect!r} changed during ended apply"
                )
            return EndEdgeEffectResult(str(receipt[0]), created=False, applied=False)

        if open_row is not None:
            changed = conn.execute(
                "UPDATE relation_edges SET confidence=MAX(confidence, ?),"
                " observations=MAX(observations, 1), last_observed_at=?, valid_to=?,"
                " status=CASE WHEN ?='active' THEN 'active' ELSE status END"
                " WHERE edge_id=? AND valid_to IS NULL",
                (conf, ts, ts, st.value, edge_id),
            )
            if changed.rowcount != 1:
                raise RuntimeError(
                    "relation_edges: ended effect claimed a receipt but its open edge changed"
                )
            return EndEdgeEffectResult(edge_id, created=False, applied=True)

        conn.execute(
            "INSERT INTO relation_edges"
            " (edge_id, edge_key, src_identity, dst_identity, predicate, label, valid_from,"
            " valid_to, provenance, confidence, quote, status, created_at, observations,"
            " src_kind, dst_kind, polarity, last_observed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
            (
                edge_id,
                logical_key,
                src,
                dst,
                pred.value,
                label,
                ts,
                ts,
                prov,
                conf,
                quote,
                st.value,
                ts,
                sk.value,
                dk.value,
                pol,
                ts,
            ),
        )
        return EndEdgeEffectResult(edge_id, created=True, applied=True)


def close_edges_quoted_in(
    conn: sqlite3.Connection, content: str, *, at: str | None = None
) -> list[str]:
    """Close every open edge whose evidence ``quote`` is a substring of
    ``content`` — the §4.6 human-adjudication leg: when a contradiction verdict
    retires a fact entry, the relations that entry evidenced end WITH it.
    Deterministic, bounded, idempotent (already-closed edges don't match the
    ``valid_to IS NULL`` guard); returns the closed edge_ids. Fail-open on an
    empty/whitespace content (closes nothing — never mass-close on bad input).
    """
    ensure_schema(conn)
    hay = (content or "").strip()
    if not hay:
        return []
    ts = at or _now_iso()
    closed: list[str] = []
    for row in conn.execute(
        "SELECT edge_id, quote FROM relation_edges WHERE valid_to IS NULL"
        " AND quote IS NOT NULL AND quote != ''"
    ).fetchall():
        if str(row[1]).strip() and str(row[1]).strip() in hay:
            conn.execute(
                "UPDATE relation_edges SET valid_to = ? WHERE edge_id = ? AND valid_to IS NULL",
                (ts, row[0]),
            )
            closed.append(row[0])
    if closed:
        conn.commit()
        logger.info("close_edges_quoted_in: closed %d edges", len(closed))
    return closed


def _reinforce_additive_effect(
    conn: sqlite3.Connection,
    *,
    edge_id: str,
    effect_key: str,
    confidence: float | None,
    at: str,
) -> bool:
    """Atomically claim one effect receipt and, only for its winner, add one."""
    with _effect_transaction(conn):
        receipt_cur = conn.execute(
            "INSERT OR IGNORE INTO relation_edge_effects"
            " (effect_key, edge_id, edge_key, applied_observations, created_at)"
            " SELECT ?, edge_id, edge_key, 1, ? FROM relation_edges"
            " WHERE edge_id=? AND valid_to IS NULL",
            (effect_key, at, edge_id),
        )
        receipt_added = receipt_cur.rowcount > 0
        if not receipt_added:
            receipt = conn.execute(
                "SELECT edge_id, edge_key FROM relation_edge_effects WHERE effect_key=?",
                (effect_key,),
            ).fetchone()
            current = conn.execute(
                "SELECT edge_key FROM relation_edges WHERE edge_id=?", (edge_id,)
            ).fetchone()
            current_key = str(current[0]) if current is not None else None
            if receipt is not None and str(receipt[1]) != current_key:
                raise RelationEdgeEffectConflict(
                    f"relation_edges: effect_key {effect_key!r} already belongs to "
                    f"edge_key {receipt[1]!r}, not {current_key!r}"
                )
            if receipt is not None and str(receipt[0]) != str(edge_id):
                # The same logical Line was closed and later reopened under a
                # new physical edge_id. The old effect was already counted in
                # the prior validity interval; its retry is a successful no-op,
                # not a vote for the reopened interval and not a conflict.
                return False

        conf_grew = False
        if confidence is not None:
            conf_cur = conn.execute(
                "UPDATE relation_edges SET confidence=MAX(confidence, ?), last_observed_at=?"
                " WHERE edge_id=? AND valid_to IS NULL AND confidence < ?",
                (confidence, at, edge_id, confidence),
            )
            conf_grew = conf_cur.rowcount > 0

        obs_grew = False
        if receipt_added:
            obs_cur = conn.execute(
                "UPDATE relation_edges SET observations=observations + 1, last_observed_at=?"
                " WHERE edge_id=? AND valid_to IS NULL",
                (at, edge_id),
            )
            if obs_cur.rowcount != 1:
                raise RuntimeError(
                    "relation_edges: effect receipt was claimed but its open edge was not updated"
                )
            obs_grew = True
    return obs_grew or conf_grew


def reinforce_edge(
    conn: sqlite3.Connection,
    *,
    edge_id: str,
    observations: int,
    confidence: float | None = None,
    at: str | None = None,
    additive: bool = False,
    effect_key: str | None = None,
) -> bool:
    """Monotone evidence reinforcement: raise an OPEN edge's strength to ``observations``.

    Strength is the number of distinct supporting evidence items. The caller
    computes the count FROM the evidence itself and this sets ``observations =
    MAX(current, given)`` — so re-running the extraction over the same data is a no-op
    (idempotent), while genuinely new evidence raises it.

    ``additive=True`` switches to **increment** semantics (``observations += given``) for
    the ① ``engaged_with`` attention floor: each session that re-engages an entity is a
    NEW piece of evidence, so the floor's strength must ACCUMULATE = distinct-session
    count = attention weight. MAX-of-1 (the caller passing 1 every session) would freeze
    it at 1 (the point-layer bug); increment fixes it. Callers must fire once per session
    (the session-end callback does); a re-run repair is the deterministic recompute from
    ``memory_deltas`` distinct-session count. Supplying a relation-specific
    ``effect_key`` makes this additive path exactly-once: claiming its unique
    receipt and adding one observation happen in the same SQLite transaction.
    A retry of that effect does not increment; a distinct effect adds one.
    Legacy additive callers may omit the key and retain the old at-least-once
    behavior. ``effect_key`` is invalid on the default MAX path.

    ``confidence`` **likewise only
    ratchets up (MAX), INDEPENDENTLY of whether ``observations`` grew** (issue #453): the
    two axes move on their own gates, so a caller that keeps ``observations`` pinned (the
    LLM `reports_to` pass and the activity pass both default `observations=1`) can still
    lift the edge's confidence with stronger evidence. Never touches ``created_at`` /
    ``valid_from``; refuses closed edges. Returns True iff the strength actually grew on
    EITHER axis (observations rose OR confidence ratcheted up).
    """
    ensure_schema(conn)
    obs = int(observations)
    if obs < 1:
        raise ValueError(f"relation_edges: observations {obs} must be >= 1")
    effect = _normalize_effect_key(effect_key)
    if effect is not None and not additive:
        raise ValueError("relation_edges: effect_key requires additive=True")
    if effect is not None and obs != 1:
        raise ValueError(
            "relation_edges: an effect-keyed additive reinforcement must add exactly "
            "one observation"
        )
    conf = None
    if confidence is not None:
        conf = float(confidence)
        if not 0.0 <= conf <= 1.0:
            raise ValueError(f"relation_edges: confidence {conf} out of [0,1]")
    ts = at or _now_iso()
    if effect is not None:
        return _reinforce_additive_effect(
            conn,
            edge_id=edge_id,
            effect_key=effect,
            confidence=conf,
            at=ts,
        )
    # Confidence ratchet — gated ONLY on confidence actually rising, NOT on observations
    # growth (the #453 bug: the MAX used to ride the `observations < ?` UPDATE, so a
    # never-growing observations count froze confidence at its first-seen value). Runs only
    # when a confidence is supplied; `confidence < ?` makes rowcount>0 mean it truly grew,
    # and never ratchets down (the column is NOT NULL, so no NULL edge case).
    conf_grew = False
    if conf is not None:
        conf_cur = conn.execute(
            "UPDATE relation_edges SET confidence = MAX(confidence, ?), last_observed_at = ? "
            "WHERE edge_id = ? AND valid_to IS NULL AND confidence < ?",
            (conf, ts, edge_id, conf),
        )
        conf_grew = conf_cur.rowcount > 0
    # Observations ratchet — MAX (idempotent) by default; additive (increment) for the ① floor.
    if additive:
        obs_cur = conn.execute(
            "UPDATE relation_edges SET observations = observations + ?, last_observed_at = ? "
            "WHERE edge_id = ? AND valid_to IS NULL",
            (obs, ts, edge_id),
        )
    else:
        obs_cur = conn.execute(
            "UPDATE relation_edges SET observations = ?, last_observed_at = ? "
            "WHERE edge_id = ? AND valid_to IS NULL AND observations < ?",
            (obs, ts, edge_id, obs),
        )
    conn.commit()
    return obs_cur.rowcount > 0 or conf_grew


def edges_as_of(
    conn: sqlite3.Connection,
    identities: Iterable[str],
    *,
    as_of: str | None = None,
    status: str | MemoryStatus = MemoryStatus.ACTIVE,
) -> list[sqlite3.Row]:
    """Edges touching any of ``identities`` that are **valid at ``as_of``** (default now) —
    the first hop of the §4.6 traversal.

    Valid-time filter: ``valid_from <= as_of AND (valid_to IS NULL OR as_of < valid_to)``.
    ``status`` defaults to ``active`` so ``shadow`` edges stay out of any traversal — which is
    what keeps P0 extraction (writes ``shadow``) inert against retrieval.
    """
    ensure_schema(conn)
    ids = [str(i).strip() for i in identities if str(i).strip()]
    if not ids:
        return []
    ts = as_of or _now_iso()
    st = MemoryStatus(str(status)).value
    placeholders = ",".join("?" * len(ids))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""
        SELECT * FROM relation_edges
        WHERE status = ?
          AND (src_identity IN ({placeholders}) OR dst_identity IN ({placeholders}))
          AND valid_from <= ?
          AND (valid_to IS NULL OR ? < valid_to)
        ORDER BY valid_from
        """,
        (st, *ids, *ids, ts, ts),
    ).fetchall()
    return list(rows)


def neighbors(
    conn: sqlite3.Connection,
    seeds: Iterable[str],
    *,
    depth: int = 2,
    as_of: str | None = None,
    status: str | MemoryStatus = MemoryStatus.ACTIVE,
    include_shadow: bool = False,
) -> set[str]:
    """Identities reachable within ``depth`` hops of ``seeds`` via edges valid
    at ``as_of`` — the relation head's production traversal primitive.

    Returns the REACHED identities only (seeds excluded). ``status`` defaults
    to ``active``, so shadow edges stay out of retrieval — with
    today's extraction writing shadow-only, production traversal honestly
    reaches nothing until edges are proven and promoted. ``include_shadow``
    additionally walks shadow edges (§7-3 gain unlock: audited-clean shadow
    may vote, downweighted by the caller — see ``fts.search_associative``).
    """
    frontier = {str(s).strip() for s in seeds if str(s).strip()}
    seen = set(frontier)
    reached: set[str] = set()
    for _ in range(max(0, depth)):
        if not frontier:
            break
        rows = list(edges_as_of(conn, frontier, as_of=as_of, status=status))
        if include_shadow:
            # hallucination on the full shadow population), so shadow edges may
            # join TRAVERSAL when the caller opts in — retrieval then downweights
            # the shadow-reached pool (fts), it never equals ACTIVE.
            rows += list(edges_as_of(conn, frontier, as_of=as_of, status=MemoryStatus.SHADOW))
        nxt: set[str] = set()
        for r in rows:
            for end in (r["src_identity"], r["dst_identity"]):
                if end not in seen:
                    nxt.add(end)
        reached |= nxt
        seen |= nxt
        frontier = nxt
    return reached


# Semantic predicates that are written SHADOW and earn ACTIVE through
# repeated evidence. ``engaged_with`` stays out: the dense co-occurrence
# floor is active at write time by design and would flood the fan-out cap.
_PROMOTABLE_PREDICATES: tuple[str, ...] = (
    Predicate.KNOWS.value,
    Predicate.PARTICIPATES_IN.value,
    Predicate.ABOUT.value,
    Predicate.REPORTS_TO.value,
    Predicate.PART_OF.value,
    Predicate.DEPENDS_ON.value,
)


def promote_edges(
    conn: sqlite3.Connection,
    *,
    min_observations: int = 3,
    max_per_identity: int = 20,
    predicates: Iterable[str] = _PROMOTABLE_PREDICATES,
) -> int:
    """Promote shadow edges to ACTIVE using evidence and fan-out gates.

    The original design
    §7-3, designed WITH the RRF pool weights, PR #504 finding).

    Two gates, and the second is the load-bearing one:

    1. **Evidence floor** — ``observations ≥ min_observations``: a
       once-co-occurred pair is not a proven relation.
    2. **Fan-out cap** — per source identity, only the TOP ``max_per_identity``
       strongest edges (by observations, then recency) promote. The cutover
       A/B showed naive threshold promotion makes retrieval WORSE (slotted
       bucket −8~−12pp): the relation head expands EVERY active neighbor into
       a contains-pool, so promotion volume IS dilution volume. A naive
       threshold promotes a hub's entire adjacency; the cap bounds the
       expansion fan-out by construction — the strongest relations are the
       ones worth spreading activation through, exactly the §3.1 residency
       logic (top-K by evidence) applied to edges.

    The cap is shared per source identity ACROSS every promotable predicate —
    dilution is a property of the identity's expansion fan-out, not of one
    predicate — so a hub cannot exceed the cap by spreading edges over
    predicates. ``engaged_with`` is never promoted here (active at write time).

    Idempotent. Already-ACTIVE edges reserve slots before any SHADOW candidate
    is considered, including active edges below today's evidence floor. This
    keeps the cap hard across repeated runs while preserving the no-demotion
    contract. Returns the number promoted.
    """
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    preds = tuple(dict.fromkeys(Predicate(str(predicate)).value for predicate in predicates))
    if not preds or max_per_identity <= 0:
        return 0
    placeholders = ",".join("?" * len(preds))
    active_rows = conn.execute(
        "SELECT src_identity, COUNT(*) AS active_count FROM relation_edges"
        f" WHERE predicate IN ({placeholders})"  # noqa: S608 — placeholders, not values
        " AND valid_to IS NULL AND status = ? GROUP BY src_identity",
        (*preds, MemoryStatus.ACTIVE.value),
    ).fetchall()
    taken = {row["src_identity"]: int(row["active_count"]) for row in active_rows}
    rows = conn.execute(
        "SELECT edge_id, src_identity, observations FROM relation_edges"
        f" WHERE predicate IN ({placeholders})"  # noqa: S608 — placeholders, not values
        " AND valid_to IS NULL AND status = ? AND observations >= ?"
        " ORDER BY src_identity, observations DESC, created_at DESC",
        (*preds, MemoryStatus.SHADOW.value, min_observations),
    ).fetchall()
    promoted = 0
    for row in rows:
        src = row["src_identity"]
        if taken.get(src, 0) >= max_per_identity:
            continue
        updated = conn.execute(
            "UPDATE relation_edges SET status = ?"
            " WHERE edge_id = ? AND status = ? AND valid_to IS NULL",
            (MemoryStatus.ACTIVE.value, row["edge_id"], MemoryStatus.SHADOW.value),
        )
        if updated.rowcount == 0:
            continue
        taken[src] = taken.get(src, 0) + 1
        promoted += 1
    return promoted


def bump_recall(conn: sqlite3.Connection, edge_ids: Iterable[str]) -> None:
    """Reinforce every edge traversed by a delivered chain.

    Every edge a
    delivered tree chain walked gets ``recall_count += 1`` — the read side of
    the consolidation axis (``observations`` is the write side). Feeds the
    strength bias and, later, tiered forgetting (an often-recalled edge resists
    down-precision). Best-effort by contract: callers treat failure as a no-op.
    """
    ids = [str(e).strip() for e in edge_ids if str(e).strip()]
    if not ids:
        return
    ensure_schema(conn)
    conn.executemany(
        "UPDATE relation_edges SET recall_count = recall_count + 1 WHERE edge_id = ?",
        [(eid,) for eid in ids],
    )
