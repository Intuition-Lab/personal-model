"""Deterministic owner edits to the live personal model.

One writer, shared by the local HTTP route and the CLI, for the two things a
memory owner can say about a modeled object they are looking at: *this should
read differently*, and *this is wrong about me*.

Zero LLM. The caller already pinned the object by id, so there is nothing to
retrieve, rank, or guess — which also means an edit works offline and can never
return "I could not find what you meant" to a save button.

Two layers, two mechanisms, for one reason: what would otherwise destroy the
edit differs between them.

* **Point** — routed through :func:`persome.store.entries.supersede_entry`, the
  repository's single memory-mutation choke point. Nothing in the build pipeline
  rewrites an existing Point (``model/build.py`` short-circuits its evomem
  baseline once ``evo_nodes`` is populated), so the sanctioned supersede is
  already durable. Going around it would skip the write freeze, the bitemporal
  stamp, the FTS and vector synchronisation, and the markdown authority — and
  ``upsert_node`` would clobber the row from markdown on the next shadow write.

* **Face / Volume / Root** — marked ``provenance='authored'`` in place. Here the
  destroyer is derivation itself: ``record_face`` overwrites ``signature`` on
  every re-mine, and ``upsert_root`` closes the live apex and inserts a new row
  on every synthesis. The marker is what makes both stand down. It needs no new
  table and no schema revision: ``provenance`` is an unconstrained TEXT column
  that already carries values beyond the extractor vocabulary.

Every edit also leaves an audit row in ``memory_deltas`` carrying the text it
displaced, which is what lets a reader see *what* was changed rather than only
*that* something was.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ..evidence import OWNER_EDIT_TAG
from ..logger import get
from ..store import files as files_mod

logger = get("persome.model.edit")

KINDS: frozenset[str] = frozenset({"point", "face", "volume", "root"})
OPS: frozenset[str] = frozenset({"rewrite", "retire"})

# Geometry level in `schema_faces` for each editable schema kind. Points do not
# appear here — they live in `evo_nodes` and take the supersede path.
_LEVEL_BY_KIND: dict[str, int] = {"face": 1, "volume": 2, "root": 3}

# The audit trail is the record of owner intent, so its session id is part of
# the contract between the editor and anything that must respect a decision.
AUDIT_SESSION_ID = "owner-edit"
MAX_REPLACEMENT_CHARS = 4_000
MAX_REASON_CHARS = 500


@dataclass(frozen=True)
class EditResult:
    """Outcome of one owner edit.

    ``ok`` is False for every rejected edit; ``reason`` then names the rejection
    in a form a caller can map to an HTTP status or a CLI message.

    ``shadow_misses`` is the increase in the evomem shadow-write miss counter
    across the edit. A Point edit can succeed at the markdown layer and still
    fail to move the Point the owner clicked — ``_shadow_write`` bails out when
    the baseline is missing, when an entry cannot be re-parsed, or when a
    supersede chain would end up partial. Reporting the delta is what keeps that
    from being displayed as an unqualified success.
    """

    ok: bool
    kind: str
    target_id: str
    op: str
    new_id: str = ""
    prior_text: str = ""
    # Memory file the decision applies to, for Point edits. A rejection is
    # scoped to its subject: the same sentence about someone else is a
    # different claim.
    file_name: str = ""
    reason: str = ""
    shadow_misses: int = 0
    applied: list[str] = field(default_factory=list)


def _loggable(value: str, limit: int = 128) -> str:
    """Flatten a caller-supplied value so it cannot forge log lines.

    Ids reaching the HTTP route are already rejected for control characters,
    but the CLI reaches this module directly and an audit log that a caller can
    write arbitrary lines into is not an audit log.
    """
    flattened = "".join(
        " " if character < " " or character == "\x7f" else character for character in str(value)
    )
    return flattened[:limit]


def _reject(kind: str, target_id: str, op: str, reason: str) -> EditResult:
    logger.info(
        "model edit rejected: %s %s %s — %s",
        _loggable(kind),
        _loggable(target_id),
        _loggable(op),
        reason,
    )
    return EditResult(ok=False, kind=kind, target_id=target_id, op=op, reason=reason)


def replacement_forges_an_entry(text: str) -> bool:
    """Whether this text would be re-read as a memory entry heading.

    Memory files are the store's source of truth and are re-parsed by splitting
    on ``## [timestamp] {id: ...}``. A correction body is written into that file
    verbatim, so a line of that exact shape does not stay text — it becomes a
    second entry. The consequences are not cosmetic: the correction is truncated
    at that line, the forged id collides with a real one and overwrites that
    Point's canonical content, and ``rebuild_index`` then fails permanently on
    the duplicate id, disabling restore and index recovery.

    No malice is required. The viewer shows the id of the object being
    corrected, and the id in a pasted entry is exactly the kind of thing an
    owner might paste back in.
    """
    return files_mod.ENTRY_HEADING_RE.search(str(text or "")) is not None


def _point_file_is_editable(file_name: str) -> bool:
    """Whether a correction to a Point in this file can actually reach the model.

    Mirrors the structural half of ``evomem.inversion.routes_to_engine`` — but
    not its ``evomem_active()`` term, which describes the configured write
    authority rather than the file. Under markdown authority every ordinary
    memory file is still perfectly editable; it simply takes the markdown path.

    What is *not* editable: ``skills/**`` projections, which cannot route into
    subdirectories, and append-only ``event-*.md`` logs, which never enter
    ``evo_nodes``. A supersede there would rewrite the markdown and leave the
    Point exactly where it was, so the owner would be told a correction landed
    that their model never reflects.
    """
    if "/" in file_name:
        return False
    try:
        return files_mod.validate_prefix(file_name) != "event"
    except ValueError:
        return False


def _point_backing(conn: sqlite3.Connection, file_name: str) -> str | None:
    """Where this Point's canonical text lives: ``"markdown"``, ``"evomem"``, or
    ``None`` when nothing backs it.

    Markdown wins when the file is on disk, because there it is the source of
    truth and the supersede must go through it. Otherwise a ``files`` row means
    the Point is evo-native — created by the delta-apply path, which writes
    ``evo_nodes`` directly — and the evomem engine is the only place it can be
    corrected. Neither means the Point is a remnant of something removed, and
    there is nothing to correct.
    """
    try:
        if files_mod.memory_path(file_name).is_file():
            return "markdown"
    except Exception:  # noqa: BLE001 — an unresolvable name is not editable either
        return None
    try:
        row = conn.execute("SELECT 1 FROM files WHERE path = ? LIMIT 1", (file_name,)).fetchone()
    except sqlite3.Error:
        return None
    return "evomem" if row is not None else None


def backing_map(conn: sqlite3.Connection, file_names: set[str]) -> dict[str, str | None]:
    """Resolve the backing of many files at once.

    The snapshot needs this for every projected Point, so it must not be one
    query per Point. Markdown presence is a stat per distinct file; the `files`
    table is read in a single pass.
    """
    known: set[str] = set()
    try:
        known = {str(r[0]) for r in conn.execute("SELECT path FROM files")}
    except sqlite3.Error:
        known = set()
    out: dict[str, str | None] = {}
    for name in file_names:
        try:
            on_disk = files_mod.memory_path(name).is_file()
        except Exception:  # noqa: BLE001
            out[name] = None
            continue
        out[name] = "markdown" if on_disk else ("evomem" if name in known else None)
    return out


def point_refusal(
    *,
    file_name: str,
    status: str,
    is_latest: bool,
    valid_until: str | None,
    superseded_by_empty: bool,
    backing: str | None,
) -> str:
    """Why this Point cannot be corrected, or ``""`` when it can.

    The same rules `_edit_point` enforces, in a form the snapshot can evaluate
    for every Point it projects. The viewer must not offer an action the writer
    will refuse: on a real model that was 46% of the Points on screen, each one
    inviting a correction and then declining it.
    """
    if not file_name:
        return "point_has_no_file"
    if not _point_file_is_editable(file_name):
        return "point_not_editable_in_this_file"
    if status == "archived":
        return "point_archived"
    if valid_until and not is_latest and superseded_by_empty:
        return "point_already_retired"
    if not is_latest:
        return "point_superseded"
    if backing is None:
        return "point_file_missing"
    return ""


def _semantic_tags(raw: str) -> list[str]:
    """Split an ``evo_nodes.tags`` cell into its semantic tags, minus our marker.

    The marker is stripped so re-editing an already-edited Point does not
    accumulate duplicates.
    """
    return [tag for tag in str(raw or "").split() if tag and tag != OWNER_EDIT_TAG]


def _audit(
    conn: sqlite3.Connection,
    *,
    kind: str,
    target_id: str,
    op: str,
    prior_text: str,
    new_text: str,
    reason: str,
    new_id: str,
    file_name: str,
) -> None:
    """Record what this edit displaced. Never fatal — the edit itself has landed."""
    from ..store import memory_deltas

    try:
        memory_deltas.insert(
            conn,
            session_id=AUDIT_SESSION_ID,
            payload={
                "owner_edit": {
                    "kind": kind,
                    "target_id": target_id,
                    "op": op,
                    "prior_text": prior_text,
                    "new_text": new_text,
                    "reason": reason,
                    "new_id": new_id,
                    "file_name": file_name,
                }
            },
            status="active",
            apply_status="applied",
        )
    except Exception:  # noqa: BLE001 — an unrecorded audit must not undo a real edit
        logger.exception("owner edit audit row failed for %s %s", kind, target_id)


def _mark_structure_dirty(conn: sqlite3.Connection) -> None:
    """Ask the daemon to re-derive downstream geometry on its own schedule.

    Deliberately not an inline re-synthesis: that would spend an LLM round-trip
    on a click, and would refresh Faces and the Root while leaving Volumes
    stale. The refresh tick rebuilds all of it.
    """
    from ..session import store as session_store

    try:
        session_store.increment_system_state(conn, "model_structure_dirty")
    except Exception:  # noqa: BLE001 — a missed refresh nudge must not undo the edit
        logger.exception("could not flag model_structure_dirty after an owner edit")


def _edit_point(
    conn: sqlite3.Connection, *, target_id: str, op: str, replacement: str, reason: str
) -> EditResult:
    from ..evomem import inversion as evo_inversion
    from ..store import entries

    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT file_name, content, tags, status, valid_until, superseded_by, is_latest"
            " FROM evo_nodes WHERE node_id = ? LIMIT 1",
            (target_id,),
        ).fetchone()
    except sqlite3.Error:
        return _reject("point", target_id, op, "point_lookup_failed")
    if row is None:
        return _reject("point", target_id, op, "unknown_point")

    file_name = str(row["file_name"] or "")
    if not file_name:
        # Without a file the supersede has nowhere to land and the receipt would
        # degenerate to `⟨id:⟩`, which resolves to nothing.
        return _reject("point", target_id, op, "point_has_no_file")
    if not _point_file_is_editable(file_name):
        return _reject("point", target_id, op, "point_not_editable_in_this_file")
    # Most Points never had a Markdown file. `delta_apply` mints entity and
    # assertion Points straight into `evo_nodes` through `EvoMemory`, which does
    # not consult the configured write authority and does not project Markdown —
    # so every `person-*`, `org-*`, `project-*`, and `tool-*` Point is
    # evo-native. Sending those down the Markdown path raises `FileNotFoundError`
    # from inside the store, which is what turned an ordinary correction into an
    # opaque 500. Correct each Point where it actually lives.
    backing = _point_backing(conn, file_name)
    if backing is None:
        return _reject("point", target_id, op, "point_file_missing")
    if str(row["status"] or "") == "archived":
        return _reject("point", target_id, op, "point_archived")
    # Same three conditions the snapshot uses. `valid_until` alone also marks a
    # relationship that ended while the fact is still current, and those Points
    # are rendered in the model — refusing to correct one would show the owner a
    # claim about themselves that they are not allowed to touch.
    already_retired = (
        bool(row["valid_until"])
        and not int(row["is_latest"] or 0)
        and str(row["superseded_by"] or "[]") == "[]"
    )
    if already_retired:
        return _reject("point", target_id, op, "point_already_retired")
    if not int(row["is_latest"] or 0):
        # Correcting an already-superseded version would fork the chain: two
        # live successors of one fact, each claiming to be the current wording.
        # The owner means the version they can see, which is the chain head.
        return _reject("point", target_id, op, "point_superseded")

    prior_text = str(row["content"] or "")
    tags = _semantic_tags(row["tags"])

    writer = entries if backing == "markdown" else evo_inversion

    if op == "rewrite":
        new_id = writer.supersede_entry(
            conn,
            name=file_name,
            old_entry_id=target_id,
            new_content=replacement,
            reason=reason or "owner edit",
            # Passed explicitly: `supersede_entry` inherits the superseded
            # entry's tags when this argument is omitted, which would leave an
            # owner-authored fact indistinguishable from an observed one.
            tags=[*tags, OWNER_EDIT_TAG],
        )
        return EditResult(
            ok=True,
            kind="point",
            target_id=target_id,
            op=op,
            new_id=str(new_id),
            prior_text=prior_text,
            file_name=file_name,
            applied=[f"superseded {target_id} -> {new_id} in {file_name}"],
        )

    writer.mark_entry_deleted(conn, name=file_name, entry_id=target_id)
    return EditResult(
        ok=True,
        kind="point",
        target_id=target_id,
        op=op,
        new_id=target_id,
        prior_text=prior_text,
        file_name=file_name,
        applied=[f"retired {target_id} in {file_name}"],
    )


def _edit_schema_object(
    conn: sqlite3.Connection, *, kind: str, target_id: str, op: str, replacement: str
) -> EditResult:
    from ..store import schema_faces

    schema_faces.ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT level, status FROM schema_faces WHERE face_id = ? AND valid_to IS NULL LIMIT 1",
        (target_id,),
    ).fetchone()
    if row is None:
        return _reject(kind, target_id, op, "unknown_object")
    if int(row["level"]) != _LEVEL_BY_KIND[kind]:
        # Editing a Volume through the Face endpoint would silently retire the
        # wrong tier of the geometry.
        return _reject(kind, target_id, op, "kind_level_mismatch")
    if str(row["status"] or "") == "archived":
        return _reject(kind, target_id, op, "object_archived")
    if op == "rewrite" and str(row["status"] or "") != "active":
        # A SHADOW object has not been promoted yet, and `maybe_promote` only
        # accepts `both` (or `emergent` at level 2). Marking one `authored`
        # would make it permanently unpromotable — invisible to the model
        # forever, while the save reported success. Rejecting one is still
        # allowed: withdrawing something that never surfaced is coherent.
        return _reject(kind, target_id, op, "object_not_active")

    if op == "rewrite":
        prior = schema_faces.set_authored_signature(conn, face_id=target_id, signature=replacement)
        if prior is None:
            return _reject(kind, target_id, op, "unknown_object")
        return EditResult(
            ok=True,
            kind=kind,
            target_id=target_id,
            op=op,
            new_id=target_id,
            prior_text=prior,
            applied=[f"authored signature for {target_id}"],
        )

    prior = schema_faces.retire_face(conn, face_id=target_id)
    if prior is None:
        return _reject(kind, target_id, op, "unknown_object")
    return EditResult(
        ok=True,
        kind=kind,
        target_id=target_id,
        op=op,
        new_id=target_id,
        prior_text=prior,
        applied=[f"retired {target_id}"],
    )


def apply_model_edit(
    conn: sqlite3.Connection,
    *,
    kind: str,
    target_id: str,
    op: str,
    replacement: str = "",
    reason: str = "",
) -> EditResult:
    """Apply one owner edit to the live model.

    ``kind`` is one of :data:`KINDS`, ``op`` one of :data:`OPS`. ``replacement``
    is required for ``rewrite`` and ignored for ``retire``.

    Never raises for a rejected edit — an invalid target comes back as
    ``ok=False`` with a machine-readable ``reason``. Storage-layer failures do
    propagate: a write that half-succeeded must not be reported as a clean
    refusal.
    """
    from ..evomem import shadow as evo_shadow

    kind = str(kind or "").strip().lower()
    op = str(op or "").strip().lower()
    target_id = str(target_id or "").strip()
    replacement = str(replacement or "").strip()
    reason = str(reason or "").strip()[:MAX_REASON_CHARS]

    if kind not in KINDS:
        return _reject(kind, target_id, op, "unknown_kind")
    if op not in OPS:
        return _reject(kind, target_id, op, "unknown_op")
    if not target_id:
        return _reject(kind, target_id, op, "missing_target")
    if op == "rewrite":
        if not replacement:
            return _reject(kind, target_id, op, "empty_replacement")
        if len(replacement) > MAX_REPLACEMENT_CHARS:
            return _reject(kind, target_id, op, "replacement_too_long")
        if replacement_forges_an_entry(replacement):
            return _reject(kind, target_id, op, "replacement_forges_an_entry")

    misses_before = evo_shadow.miss_count()
    if kind == "point":
        result = _edit_point(
            conn, target_id=target_id, op=op, replacement=replacement, reason=reason
        )
    else:
        result = _edit_schema_object(
            conn, kind=kind, target_id=target_id, op=op, replacement=replacement
        )

    if not result.ok:
        return result

    _audit(
        conn,
        kind=kind,
        target_id=target_id,
        op=op,
        prior_text=result.prior_text,
        new_text=replacement,
        reason=reason,
        new_id=result.new_id,
        file_name=result.file_name,
    )
    _mark_structure_dirty(conn)

    misses = max(0, evo_shadow.miss_count() - misses_before)
    if misses:
        logger.warning(
            "owner edit on %s %s completed with %d shadow-write miss(es);"
            " the markdown layer changed but the Point may not have moved",
            _loggable(kind),
            _loggable(target_id),
            misses,
        )
    logger.info(
        "owner edit applied: %s %s %s", _loggable(kind), _loggable(target_id), _loggable(op)
    )
    return EditResult(
        ok=True,
        kind=result.kind,
        target_id=result.target_id,
        op=result.op,
        new_id=result.new_id,
        prior_text=result.prior_text,
        file_name=result.file_name,
        shadow_misses=misses,
        applied=result.applied,
    )
