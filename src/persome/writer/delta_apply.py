"Deterministic application of gated personal-model deltas."

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..evomem import identity as identity_mod
from ..evomem import relation_extractor as rex
from ..evomem.engine import EvoMemory
from ..evomem.models import MemoryLayer
from ..evomem.person_graph import _slug as _entity_slug
from ..logger import get
from ..model.edit import AUDIT_SESSION_ID as _OWNER_EDIT_SESSION
from ..store import entries as entries_store
from ..store import event_occurrences as occurrences_store
from ..store import memory_delta_items as items_store
from ..store import memory_deltas as deltas_store
from ..store import model_candidates as candidates_store
from ..store import relation_edges as edges_store
from ..store.relation_edges import EntityKind, Predicate

logger = get("persome.writer.delta_apply")

SELF_IDENTITY = "self"
EVENT_PREFIX = "event:"


_KIND_PREFIX = {"person": "person", "org": "org", "project": "project", "artifact": "tool"}
_KIND_ENUM = {
    "person": EntityKind.PERSON,
    "org": EntityKind.ORG,
    "project": EntityKind.PROJECT,
    "artifact": EntityKind.ARTIFACT,
    "self": EntityKind.SELF,
    "event": EntityKind.EVENT,
}


@dataclass
class ApplyResult:
    entities_minted: int = 0
    entities_seen: int = 0
    assertions_minted: int = 0
    assertions_seen: int = 0
    edges_new: int = 0
    edges_reinforced: int = 0
    edges_closed: int = 0
    events_minted: int = 0
    floor_edges: int = 0
    supersedes_applied: int = 0
    candidates_pending: int = 0
    candidates_promoted: int = 0
    candidates_rejected: int = 0
    skipped_reason: str = ""
    errors: list[str] = field(default_factory=list)
    # Durable aggregate from ``memory_delta_items.geometry_changed``. ``None``
    # means a compatibility item was acknowledged before that receipt existed.
    geometry_changed: bool | None = None


@dataclass(frozen=True)
class _EventWindowContext:
    delta_id: int | None
    session_id: str
    window_start: datetime | str
    window_end: datetime | str


def _canonical_of(who: dict[str, Any] | None) -> str | None:
    if not isinstance(who, dict):
        return None

    return who.get("ref") or who.get("new_entity") or who.get("canonical")


def _entity_kind_map(clean: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    ambiguous: set[str] = set()
    for e in clean.get("entities") or []:
        c = _canonical_of(e)
        if c and e.get("kind") in _KIND_PREFIX:
            key = identity_mod.norm(c)
            if key in ambiguous:
                continue
            previous = out.get(key)
            if previous is not None and previous != e["kind"]:
                out.pop(key, None)
                ambiguous.add(key)
                continue
            out[key] = e["kind"]
    return out


def _find_entity_head(conn: sqlite3.Connection, file_name: str) -> str | None:
    try:
        row = conn.execute(
            "SELECT node_id FROM evo_nodes WHERE file_name = ? AND is_latest = 1"
            " AND status = 'active' LIMIT 1",
            (file_name,),
        ).fetchone()
        return row[0] if row else None
    except Exception:  # noqa: BLE001
        return None


def _entity_file(entity: dict[str, Any]) -> str | None:
    kind = entity.get("kind")
    canonical = _canonical_of(entity)
    if not canonical or kind not in _KIND_PREFIX or canonical == SELF_IDENTITY:
        return None
    return f"{_KIND_PREFIX[kind]}-{_entity_slug(canonical)}.md"


def _record_candidate(
    conn: sqlite3.Connection,
    *,
    candidate_kind: str,
    subject: str,
    text: str,
    evidence: dict[str, Any],
    session_id: str | None,
    window_start: datetime | str | None,
    window_end: datetime | str | None,
    result: ApplyResult,
) -> candidates_store.RecordResult | None:
    """Record one grounded candidate and expose its gate state in apply counts."""
    if session_id is None or window_start is None or window_end is None:
        return None
    recorded = candidates_store.record_evidence(
        conn,
        candidate_kind=candidate_kind,
        subject=subject,
        text=text,
        session_id=session_id,
        window_start=window_start,
        window_end=window_end,
        quote=str(evidence.get("quote") or ""),
        confidence=evidence.get("confidence", 0.0),
    )
    if recorded is None:
        return None
    if recorded.state.status == candidates_store.STATUS_PENDING:
        result.candidates_pending += 1
    elif recorded.state.status == candidates_store.STATUS_PROMOTED:
        result.candidates_promoted += 1
    elif recorded.state.status == candidates_store.STATUS_REJECTED:
        result.candidates_rejected += 1
    return recorded


def _apply_entities(conn: sqlite3.Connection, mem: EvoMemory, clean: dict, r: ApplyResult) -> None:
    now = datetime.now(UTC).isoformat()
    ended_files: list[str] = []
    for e in clean.get("entities") or []:
        try:
            if not isinstance(e, dict):
                continue
            kind = e.get("kind")
            canonical = _canonical_of(e)
            if not canonical or kind not in _KIND_PREFIX or canonical == SELF_IDENTITY:
                continue
            stem = f"{_KIND_PREFIX[kind]}-{_entity_slug(canonical)}"
            stored = f"{stem}.md"

            head = _find_entity_head(conn, stored)
            if head is None and _owner_withdrew(conn, stored, canonical):
                r.entities_seen += 1
                continue
            if head is None:
                mem.add_direct(
                    canonical,
                    layer=MemoryLayer.L5_KNOWLEDGE,
                    file_name=stem,
                    tags="entity",
                )
                r.entities_minted += 1
            else:
                r.entities_seen += 1
            if e.get("ended"):
                ended_files.append(stored)
        except Exception as exc:  # noqa: BLE001
            r.errors.append(f"entity: {exc}")

    if ended_files:
        _stamp_entities_valid_until(conn, ended_files, now)


def _stamp_entities_valid_until(conn: sqlite3.Connection, file_names: list[str], at: str) -> None:
    with contextlib.suppress(Exception):
        conn.commit()
        conn.executemany(
            "UPDATE evo_nodes SET valid_until = ? WHERE file_name = ? AND is_latest = 1"
            " AND valid_until IS NULL",
            [(at, fn) for fn in file_names],
        )
        conn.commit()


def _route_assertion_stem(
    conn: sqlite3.Connection, canonical: str, kinds: dict[str, str]
) -> str | None:
    slug = _entity_slug(canonical)
    kind = kinds.get(identity_mod.norm(canonical))
    if kind in _KIND_PREFIX:
        return f"{_KIND_PREFIX[kind]}-{slug}"
    for prefix in _KIND_PREFIX.values():
        if _find_entity_head(conn, f"{prefix}-{slug}.md") is not None:
            return f"{prefix}-{slug}"
    return None


def _assertion_exists(conn: sqlite3.Connection, stored: str, text: str) -> bool:
    """Whether this exact assertion is already the live wording in that file.

    Compares the *visible* body. A Point written by a correction stores its text
    plus a `<!-- supersedes: ... -->` marker, so an exact match against
    `content` never sees it — and the owner's own new wording gets minted a
    second time as a duplicate live Point the next time it is observed.
    """
    from ..store.entries import strip_supersede_provenance

    try:
        rows = conn.execute(
            "SELECT content, supersedes FROM evo_nodes WHERE file_name = ? AND is_latest = 1",
            (stored,),
        ).fetchall()
    except Exception:  # noqa: BLE001
        return False
    wanted = text.strip()
    for content, supersedes in rows:
        try:
            chain = {str(v) for v in json.loads(str(supersedes or "[]") or "[]")}
        except (TypeError, ValueError):
            chain = set()
        if strip_supersede_provenance(str(content or ""), supersedes=chain).strip() == wanted:
            return True
    return False


def _owner_withdrew(conn: sqlite3.Connection, stored: str, text: str) -> bool:
    """Whether the owner explicitly rejected this exact assertion.

    A withdrawn Point is not ``is_latest``, so the dedupe check above does not
    see it and the next observation of the same behaviour mints the claim again.
    "This is wrong about me" then lasts until the next time the owner does the
    thing — which is no rejection at all.

    Intent is read from the owner candidate decision and owner-edit audit trail
    rather than inferred from the row's shape. A retired Point is ``shadow``
    with a ``valid_until`` and no
    successor — but so is one the orphan reaper collected after its TTL, and so
    is anything else that retires a node without replacing it. Those are
    housekeeping, not decisions: a fact that aged out must be free to come back
    when the owner starts doing it again. Only an explicit rejection suppresses
    re-minting, and only of the wording the owner actually rejected.
    """
    wanted = text.strip()
    promoted_at: datetime | None = None
    try:
        candidates_store.ensure_schema(conn)
        normalized = candidates_store.canonical_text(wanted)
        rows = conn.execute(
            "SELECT candidate_key, candidate_kind, subject, status, decision_source"
            " FROM model_candidates WHERE canonical_text=?",
            (normalized,),
        ).fetchall()
        for candidate_key, candidate_kind, subject, status, decision_source in rows:
            kind = str(candidate_kind)
            canonical_subject = str(subject or "").strip()
            if not canonical_subject:
                continue
            slug = _entity_slug(canonical_subject)
            matches = False
            if kind == candidates_store.KIND_ASSERTION:
                if stored in {f"{prefix}-{slug}.md" for prefix in _KIND_PREFIX.values()}:
                    matches = True
            else:
                prefix = _KIND_PREFIX.get(kind)
                matches = bool(
                    prefix is not None
                    and stored == f"{prefix}-{slug}.md"
                    and candidates_store.canonical_text(canonical_subject) == normalized
                )
            if not matches or str(decision_source) != "owner_explicit":
                continue
            if str(status) == candidates_store.STATUS_REJECTED:
                return True
            if str(status) != candidates_store.STATUS_PROMOTED:
                continue
            decision = conn.execute(
                "SELECT created_at FROM model_candidate_decisions"
                " WHERE candidate_key=? AND source_kind='owner_explicit' AND to_status=?"
                " ORDER BY created_at DESC, decision_id DESC LIMIT 1",
                (str(candidate_key), candidates_store.STATUS_PROMOTED),
            ).fetchone()
            if decision is not None:
                try:
                    value = datetime.fromisoformat(str(decision[0]).replace("Z", "+00:00"))
                    if value.tzinfo is None:
                        value = value.astimezone()
                    value = value.astimezone(UTC)
                    if promoted_at is None or value > promoted_at:
                        promoted_at = value
                except (TypeError, ValueError):
                    pass
    except Exception:  # noqa: BLE001 — the independent audit remains authoritative
        pass

    try:
        rows = conn.execute(
            "SELECT payload, created_at FROM memory_deltas WHERE session_id = ?",
            (_OWNER_EDIT_SESSION,),
        ).fetchall()
    except Exception:  # noqa: BLE001 — a dedupe miss must never break ingestion
        return False
    matched_audit = False
    latest_audit_at: datetime | None = None
    audit_time_unknown = False
    for payload, created_at in rows:
        try:
            edit = json.loads(payload or "{}").get("owner_edit") or {}
        except (TypeError, ValueError):
            continue
        if edit.get("kind") != "point":
            continue
        # A rewrite is a rejection of the old wording just as much as a retire
        # is. The superseded Point is no longer `is_latest`, so re-observing the
        # text the owner replaced would mint it again and stand the discarded
        # version back up beside the correction.
        if edit.get("op") not in ("retire", "rewrite"):
            continue
        if str(edit.get("prior_text") or "").strip() != wanted:
            continue
        # Scoped to the file the decision was made in. The same sentence about a
        # different subject is a different claim, and one rejection must not
        # silence it everywhere.
        edited_file = str(edit.get("file_name") or "")
        if edited_file and edited_file != stored:
            continue
        matched_audit = True
        try:
            value = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.astimezone()
            value = value.astimezone(UTC)
            if latest_audit_at is None or value > latest_audit_at:
                latest_audit_at = value
        except (TypeError, ValueError):
            audit_time_unknown = True

    if not matched_audit:
        # An explicit promotion is independently authoritative even when the
        # candidate did not originate in an owner edit.
        return False
    # An explicit restore after the older retire/rewrite receipt supersedes that
    # append-only audit history. Unknown ordering fails closed on the withdrawal.
    return not (
        promoted_at is not None
        and not audit_time_unknown
        and latest_audit_at is not None
        and promoted_at > latest_audit_at
    )


def _apply_assertions(
    conn: sqlite3.Connection, mem: EvoMemory, clean: dict, kinds: dict[str, str], r: ApplyResult
) -> None:
    for a in clean.get("assertions") or []:
        try:
            if not isinstance(a, dict):
                continue
            text = str(a.get("text") or "").strip()
            canonical = _canonical_of(a.get("subject"))
            if not text or not canonical or canonical == SELF_IDENTITY:
                continue
            stem = _route_assertion_stem(conn, canonical, kinds)
            if stem is None:
                continue
            if _assertion_exists(conn, f"{stem}.md", text):
                r.assertions_seen += 1
                continue
            if _owner_withdrew(conn, f"{stem}.md", text):
                r.assertions_seen += 1
                continue
            tags = "fact"
            conf = a.get("confidence")
            if isinstance(conf, (int, float)):
                tags += f" confidence:{float(conf):.2f}"
            mem.add_direct(text, layer=MemoryLayer.L5_KNOWLEDGE, file_name=stem, tags=tags)
            r.assertions_minted += 1
        except Exception as exc:  # noqa: BLE001
            r.errors.append(f"assertion: {exc}")


def _materialize_promoted_assertions(
    conn: sqlite3.Connection,
    mem: EvoMemory,
    *,
    canonical: str,
    kinds: dict[str, str],
    result: ApplyResult,
) -> None:
    """Land promoted assertions once their subject Point finally exists."""
    for candidate in candidates_store.promoted_assertions_for_subject(conn, canonical):
        _apply_assertions(
            conn,
            mem,
            {
                "assertions": [
                    {
                        "subject": {"ref": canonical},
                        "text": candidate.text,
                    }
                ]
            },
            kinds,
            result,
        )


def _apply_relations(
    conn: sqlite3.Connection,
    clean: dict,
    kinds: dict[str, str],
    r: ApplyResult,
    *,
    effect_key: str | None = None,
) -> None:
    seen = rex._open_edges(conn)  # noqa: SLF001
    tally = rex._Tally()  # noqa: SLF001
    now = datetime.now(UTC).isoformat()
    for rel in clean.get("relations") or []:
        try:
            if not isinstance(rel, dict):
                continue
            src = _canonical_of(rel.get("src"))
            dst = _canonical_of(rel.get("dst"))
            pred_raw = rel.get("predicate")
            if not src or not dst or pred_raw not in {p.value for p in Predicate}:
                continue
            predicate = Predicate(pred_raw)
            src_kind = _endpoint_kind(src, kinds)
            dst_kind = _endpoint_kind(dst, kinds)
            if rel.get("ended") and effect_key is not None:
                ended = edges_store.end_edge_with_effect(
                    conn,
                    src_identity=src,
                    dst_identity=dst,
                    predicate=predicate,
                    src_kind=src_kind,
                    dst_kind=dst_kind,
                    provenance="inferred",
                    confidence=float(rel.get("confidence", 0.5)),
                    effect_key=effect_key,
                    label=rel.get("label"),
                    quote=str(rel.get("quote") or "")[:120] or None,
                    polarity=_norm_polarity(rel.get("polarity")),
                    at=now,
                )
                if ended.applied:
                    if ended.created:
                        r.edges_new += 1
                    else:
                        r.edges_reinforced += 1
                    r.edges_closed += 1
                continue
            before = tally.new
            try:
                rex._upsert_shadow(  # noqa: SLF001
                    conn,
                    seen,
                    tally,
                    src=src,
                    dst=dst,
                    predicate=predicate,
                    confidence=float(rel.get("confidence", 0.5)),
                    quote=str(rel.get("quote") or ""),
                    label=rel.get("label"),
                    observations=1,
                    src_kind=src_kind,
                    dst_kind=dst_kind,
                    polarity=_norm_polarity(rel.get("polarity")),
                    additive=bool(rel.get("cooccurrence")),
                    effect_key=(effect_key if bool(rel.get("cooccurrence")) else None),
                )
            except ValueError:
                continue
            if tally.new > before:
                r.edges_new += 1
            else:
                r.edges_reinforced += 1
            # ended -> close_edge (section 4.6, leg A)
            if rel.get("ended"):
                key = rex._edge_key(src, dst, predicate.value)  # noqa: SLF001
                eid = seen.get(key)
                if eid and edges_store.close_edge(conn, edge_id=eid, at=now):
                    r.edges_closed += 1
        except Exception as exc:  # noqa: BLE001
            r.errors.append(f"relation: {exc}")


def _event_window_context(
    conn: sqlite3.Connection,
    *,
    delta_id: int | None,
    session_id: str | None,
    window_start: datetime | str | None,
    window_end: datetime | str | None,
) -> _EventWindowContext | None:
    """Resolve the occurrence namespace without guessing from payload equality.

    Windowed modeling passes the full context explicitly.  A caller that only
    has a ``delta_id`` may resolve that exact row; context-free legacy callers
    retain the historical title-hash endpoint.  Identical JSON in an unrelated
    window is never treated as provenance.
    """
    explicit = (session_id, window_start, window_end)
    if any(value is not None for value in explicit):
        if not all(value is not None for value in explicit):
            raise ValueError("event occurrence context requires session_id and both window bounds")
        return _EventWindowContext(
            delta_id=delta_id,
            session_id=str(session_id),
            window_start=window_start,  # type: ignore[arg-type]
            window_end=window_end,  # type: ignore[arg-type]
        )

    try:
        if delta_id is not None:
            rows = conn.execute(
                "SELECT id, session_id, window_start, window_end, payload "
                "FROM memory_deltas WHERE id = ? LIMIT 1",
                (delta_id,),
            ).fetchall()
        else:
            return None
    except sqlite3.Error:
        return None

    if len(rows) != 1 or not str(rows[0][2] or "") or not str(rows[0][3] or ""):
        return None
    row = rows[0]
    return _EventWindowContext(
        delta_id=int(row[0]),
        session_id=str(row[1]),
        window_start=str(row[2]),
        window_end=str(row[3]),
    )


def _apply_events(
    conn: sqlite3.Connection,
    clean: dict,
    kinds: dict[str, str],
    r: ApplyResult,
    *,
    context: _EventWindowContext | None,
) -> None:
    seen = rex._open_edges(conn)  # noqa: SLF001
    tally = rex._Tally()  # noqa: SLF001
    for ev in clean.get("events") or []:
        try:
            if not isinstance(ev, dict):
                continue
            title = str(ev.get("title") or "").strip()
            if not title:
                continue
            participants = [
                canonical
                for participant in (ev.get("participants") or [])
                if (canonical := _canonical_of(participant))
            ]
            occurrence = None
            if context is not None:
                item_key = occurrences_store.make_item_key(
                    title=title,
                    participants=participants,
                    quote=str(ev.get("quote") or ""),
                    explicit=ev.get("item_key"),
                )
                occurrence, created = occurrences_store.upsert(
                    conn,
                    delta_id=context.delta_id,
                    session_id=context.session_id,
                    window_start=context.window_start,
                    window_end=context.window_end,
                    item_key=item_key,
                    title=title,
                    participants=participants,
                    quote=str(ev.get("quote") or ""),
                    confidence=float(ev.get("confidence", 0.5)),
                )
                eid = occurrence.endpoint
                if created:
                    r.events_minted += 1
            else:
                # Pre-windowed callers and already-existing endpoints retain the
                # historical title hash.  Do not pretend they have occurrence
                # receipts or synthesize rows for evidence that predates them.
                eid = (
                    EVENT_PREFIX + hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]  # noqa: S324
                )
                r.events_minted += 1
            for pc in participants:
                try:
                    rex._upsert_shadow(  # noqa: SLF001
                        conn,
                        seen,
                        tally,
                        src=pc if pc != SELF_IDENTITY else SELF_IDENTITY,
                        dst=eid,
                        predicate=Predicate.PARTICIPATES_IN,
                        confidence=float(ev.get("confidence", 0.5)),
                        quote=str(ev.get("quote") or title),
                        label="event",
                        observations=1,
                        src_kind=_endpoint_kind(pc, kinds),
                        dst_kind=EntityKind.EVENT.value,
                        valid_from=(occurrence.window_end if occurrence is not None else None),
                        source_kind=("occurrence" if occurrence is not None else None),
                        source_id=(occurrence.occurrence_id if occurrence is not None else None),
                        source_receipt=(
                            occurrence.source_receipt if occurrence is not None else None
                        ),
                    )
                except ValueError:
                    continue
        except Exception as exc:  # noqa: BLE001
            r.errors.append(f"event: {exc}")


def _apply_floor(
    conn: sqlite3.Connection,
    clean: dict,
    kinds: dict[str, str],
    r: ApplyResult,
    *,
    effect_key: str | None = None,
) -> None:
    seen = rex._open_edges(conn)  # noqa: SLF001
    tally = rex._Tally()  # noqa: SLF001
    for e in clean.get("entities") or []:
        if not isinstance(e, dict):
            continue
        canonical = _canonical_of(e)
        if not canonical or canonical == SELF_IDENTITY:
            continue
        stored = _entity_file(e)
        if stored is not None and _owner_withdrew(conn, stored, canonical):
            continue
        try:
            rex._upsert_shadow(  # noqa: SLF001
                conn,
                seen,
                tally,
                src=SELF_IDENTITY,
                dst=canonical,
                predicate=Predicate.ENGAGED_WITH,
                confidence=1.0,
                quote=str(e.get("quote") or ""),
                label="engaged",
                observations=1,
                src_kind=EntityKind.SELF.value,
                dst_kind=_endpoint_kind(canonical, kinds),
                additive=True,
                status="active",  # direct observed attention, not an inferred semantic claim
                effect_key=effect_key,
            )
        except ValueError:
            continue
    r.floor_edges = tally.new + tally.reinforced


def _endpoint_kind(identity: str, kinds: dict[str, str]) -> str:
    if identity == SELF_IDENTITY:
        return EntityKind.SELF.value
    if identity.startswith(EVENT_PREFIX):
        return EntityKind.EVENT.value
    k = kinds.get(identity_mod.norm(identity))
    return _KIND_ENUM[k].value if k in _KIND_ENUM else EntityKind.PERSON.value


def _apply_supersede(conn: sqlite3.Connection, clean: dict, r: ApplyResult) -> None:
    for item in clean.get("supersede") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("file", "")).strip()
        eid = str(item.get("entry_id", "")).strip()
        if not name or not eid:
            continue
        try:
            repl = str(item.get("replacement", "")).strip()
            reason = str(item.get("reason", "") or "memory update")[:300]
            if repl:
                entries_store.supersede_entry(
                    conn, name=name, old_entry_id=eid, new_content=repl, reason=reason
                )
            else:
                entries_store.mark_entry_deleted(conn, name=name, entry_id=eid)
            r.supersedes_applied += 1
        except Exception as exc:  # noqa: BLE001 — one bad target never drops the rest
            r.errors.append(f"supersede {name}#{eid}: {exc}")


def apply_delta(
    conn: sqlite3.Connection,
    cfg: Any,
    clean: dict,
    *,
    memory: EvoMemory | None = None,
    delta_id: int | None = None,
    session_id: str | None = None,
    window_start: datetime | str | None = None,
    window_end: datetime | str | None = None,
) -> ApplyResult:
    r = ApplyResult()
    if not clean:
        r.skipped_reason = "empty"
        return r
    mem = memory or EvoMemory()
    kinds = _entity_kind_map(clean)
    event_context = _event_window_context(
        conn,
        delta_id=delta_id,
        session_id=session_id,
        window_start=window_start,
        window_end=window_end,
    )
    _apply_supersede(conn, clean, r)
    _apply_entities(conn, mem, clean, r)

    if getattr(getattr(cfg, "memory_delta", None), "apply_assertions", False):
        _apply_assertions(conn, mem, clean, kinds, r)
    _apply_floor(conn, clean, kinds, r)
    _apply_relations(conn, clean, kinds, r)
    _apply_events(
        conn,
        clean,
        kinds,
        r,
        context=event_context,
    )
    return r


_COUNT_FIELDS = (
    "entities_minted",
    "entities_seen",
    "assertions_minted",
    "assertions_seen",
    "edges_new",
    "edges_reinforced",
    "edges_closed",
    "events_minted",
    "floor_edges",
    "supersedes_applied",
    "candidates_pending",
    "candidates_promoted",
    "candidates_rejected",
)


def _merge_result(target: ApplyResult, item: ApplyResult) -> None:
    for name in _COUNT_FIELDS:
        setattr(target, name, int(getattr(target, name)) + int(getattr(item, name)))
    target.errors.extend(item.errors)


def _changes_geometry(result: ApplyResult) -> bool:
    return any(
        int(getattr(result, field, 0) or 0) > 0
        for field in (
            "entities_minted",
            "assertions_minted",
            "edges_new",
            "edges_reinforced",
            "edges_closed",
            "events_minted",
            "floor_edges",
            "supersedes_applied",
        )
    )


def _load_geometry_receipt(
    conn: sqlite3.Connection,
    *,
    delta_id: int,
    result: ApplyResult,
) -> ApplyResult:
    result.geometry_changed = items_store.geometry_changed_for_delta(conn, delta_id=delta_id)
    return result


def apply_delta_item(
    conn: sqlite3.Connection,
    cfg: Any,
    clean: dict,
    *,
    item: items_store.ClaimedDeltaItem,
    memory: EvoMemory | None = None,
    delta_id: int | None = None,
    session_id: str | None = None,
    window_start: datetime | str | None = None,
    window_end: datetime | str | None = None,
) -> ApplyResult:
    """Apply one claimed item while retaining the full delta's identity map."""
    result = ApplyResult()
    mem = memory or EvoMemory()
    kinds = _entity_kind_map(clean)
    one = item.payload
    if item.kind == "entity":
        fragment = {"entities": [one]}
        stored = _entity_file(one)
        canonical = _canonical_of(one)
        existing = stored is not None and _find_entity_head(conn, stored) is not None
        owner_withdrew = bool(
            stored is not None
            and canonical is not None
            and _owner_withdrew(conn, stored, canonical)
        )
        candidate = None
        if not existing and not owner_withdrew and stored is not None and canonical is not None:
            candidate = _record_candidate(
                conn,
                candidate_kind=str(one.get("kind") or ""),
                subject=canonical,
                text=canonical,
                evidence=one,
                session_id=session_id,
                window_start=window_start,
                window_end=window_end,
                result=result,
            )
        if owner_withdrew:
            _apply_entities(conn, mem, fragment, result)
        elif existing or (
            candidate is not None and candidate.state.status == candidates_store.STATUS_PROMOTED
        ):
            _apply_entities(conn, mem, fragment, result)
            if (
                getattr(getattr(cfg, "memory_delta", None), "apply_assertions", False)
                and stored is not None
                and canonical is not None
                and _find_entity_head(conn, stored)
            ):
                _materialize_promoted_assertions(
                    conn,
                    mem,
                    canonical=canonical,
                    kinds=kinds,
                    result=result,
                )
            _apply_floor(conn, fragment, kinds, result, effect_key=item.effect_key)
    elif item.kind == "assertion":
        if getattr(getattr(cfg, "memory_delta", None), "apply_assertions", False):
            text = str(one.get("text") or "").strip()
            canonical = _canonical_of(one.get("subject"))
            stem = _route_assertion_stem(conn, canonical, kinds) if canonical else None
            already_decided = bool(
                stem
                and (
                    _assertion_exists(conn, f"{stem}.md", text)
                    or _owner_withdrew(conn, f"{stem}.md", text)
                )
            )
            candidate = None
            if not already_decided and text and canonical and canonical != SELF_IDENTITY:
                candidate = _record_candidate(
                    conn,
                    candidate_kind=candidates_store.KIND_ASSERTION,
                    subject=canonical,
                    text=text,
                    evidence=one,
                    session_id=session_id,
                    window_start=window_start,
                    window_end=window_end,
                    result=result,
                )
            if already_decided or (
                candidate is not None and candidate.state.status == candidates_store.STATUS_PROMOTED
            ):
                _apply_assertions(conn, mem, {"assertions": [one]}, kinds, result)
    elif item.kind == "relation":
        _apply_relations(
            conn,
            {"relations": [one]},
            kinds,
            result,
            effect_key=item.effect_key,
        )
    elif item.kind == "event":
        context = _event_window_context(
            conn,
            delta_id=delta_id,
            session_id=session_id,
            window_start=window_start,
            window_end=window_end,
        )
        _apply_events(
            conn,
            {"events": [one]},
            kinds,
            result,
            context=context,
        )
    else:
        result.errors.append(f"item: unsupported kind {item.kind!r}")
    return result


def apply_persisted_delta(
    conn: sqlite3.Connection,
    cfg: Any,
    clean: dict,
    *,
    delta_id: int,
    session_id: str,
    window_start: datetime | str,
    window_end: datetime | str,
    memory: EvoMemory | None = None,
) -> ApplyResult:
    """Resume an ordered item ledger until complete or one item fails.

    The ledger update intentionally follows the effect commit. A replay after
    a crash is safe because Points/assertions probe their live effect,
    occurrences upsert by stable ID, and additive or ending Lines claim
    ``effect_key`` in the same transaction as their graph mutation. Each item
    acknowledgement also stores whether that effect changed geometry, so a
    crash before the parent status publish cannot lose the structural-dirty bit.
    """
    result = ApplyResult()
    parent = conn.execute(
        "SELECT apply_status, item_ledger_version FROM memory_deltas WHERE id=?",
        (delta_id,),
    ).fetchone()
    if parent is None:
        result.errors.append(f"delta: persisted parent {delta_id} is missing")
        return result
    ledger_version = int(parent[1] or 0)
    has_effect_items = bool(items_store.build_items(clean))
    if (
        ledger_version < deltas_store.ITEM_LEDGER_VERSION
        and str(parent[0] or "") in {"pending", "failed"}
        and has_effect_items
    ):
        result.skipped_reason = "legacy_apply_ambiguous"
        result.errors.append(
            "delta: legacy pending/failed apply has no item ledger; partial effects are ambiguous"
        )
        return result
    items_store.seed(conn, delta_id=delta_id, payload=clean)
    if ledger_version < deltas_store.ITEM_LEDGER_VERSION:
        conn.execute(
            "UPDATE memory_deltas SET item_ledger_version=? WHERE id=?",
            (deltas_store.ITEM_LEDGER_VERSION, delta_id),
        )
    mem = memory or EvoMemory()
    while True:
        claimed = items_store.claim_next(conn, delta_id=delta_id)
        if claimed is None:
            counts = items_store.state_counts(conn, delta_id=delta_id)
            remaining = (
                counts[items_store.STATE_PENDING]
                + counts[items_store.STATE_FAILED]
                + counts[items_store.STATE_APPLYING]
            )
            if remaining:
                result.skipped_reason = "item_in_progress"
            return _load_geometry_receipt(conn, delta_id=delta_id, result=result)
        try:
            item_result = apply_delta_item(
                conn,
                cfg,
                clean,
                item=claimed,
                memory=mem,
                delta_id=delta_id,
                session_id=session_id,
                window_start=window_start,
                window_end=window_end,
            )
        except Exception as exc:  # noqa: BLE001 - persist the item-level retry state
            items_store.mark_failed(
                conn,
                delta_id=delta_id,
                item=claimed,
                error=f"{type(exc).__name__}: {exc}",
            )
            result.errors.append(f"{claimed.kind}: {exc}")
            return _load_geometry_receipt(conn, delta_id=delta_id, result=result)
        _merge_result(result, item_result)
        if item_result.errors:
            items_store.mark_failed(
                conn,
                delta_id=delta_id,
                item=claimed,
                error="; ".join(item_result.errors),
            )
            return _load_geometry_receipt(conn, delta_id=delta_id, result=result)
        item_geometry_changed: bool | None = _changes_geometry(item_result)
        if claimed.attempts > 1 and item_geometry_changed is False:
            # A reclaimed lease cannot distinguish "worker died before effect"
            # from "effect committed, worker died before ledger ack". An
            # idempotent replay reports no current change in both cases. Keep
            # that durable receipt unknown so the parent recovery conservatively
            # schedules one structural rebuild instead of erasing a real change.
            item_geometry_changed = None
        if not items_store.mark_applied(
            conn,
            delta_id=delta_id,
            item=claimed,
            effect_kind=claimed.kind,
            effect_id=claimed.effect_key,
            geometry_changed=item_geometry_changed,
        ):
            result.errors.append(f"{claimed.kind}: item claim was lost after effect commit")
            return _load_geometry_receipt(conn, delta_id=delta_id, result=result)


def _norm_polarity(p: Any) -> str:
    v = str(p or "0")
    return v if v in ("+", "-", "0") else "0"
