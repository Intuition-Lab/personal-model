"""update_memory — the ONE directed memory-update entry point (2026-07-04 spec).

**First principles (model mindset — manage Memory like weights):** "correcting a memory" is
not a special operation — it is an UPDATE. Memory = weights; there is one update mechanism
(input → delta → apply). Observation is the *self-supervised* update (pre-training: the pipeline
watches the screen → memory-delta modeling → ``delta_apply``). A user saying "this is
wrong" is the *supervised* update (post-training / SFT: the user's statement is the ground-truth
label — authoritative, no quote-gate). So a "correction" is just a **directed update**, reusing
the same executor (``delta_apply``, now with a ⊖ supersede leg → a delta is a complete update =
add ∧ retract).

Backprop framing: the error shows up at the OUTPUT (recall/apex said X, user says X is wrong);
retrieval traces it back to the SOURCE facts (credit assignment); we supersede the source
(update the weight), NOT the apex (the activation). Forward re-derivation (Face to Volume to Root) then
propagates the fix. supersede-not-delete = a negative signal (shift recall away), receipts survive.

The update is logged as a feedback signal — a user's "this is wrong" is the most valuable label.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..evomem import identity
from ..evomem._json import parse_json_object
from ..logger import get
from ..store import fts
from ..writer import delta_apply
from ..writer import llm as llm_mod

logger = get("persome.writer.correct")

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "update_memory.md"
_ENTITY_OPS = {"retype", "shadow", "merge", "merge_into_self", "reject_owner_alias"}


@dataclass
class UpdateResult:
    kind: str  # update | entity_update | noop | error
    applied: list[str] = field(default_factory=list)  # human-readable of what changed
    reason: str = ""
    ok: bool = False
    errors: list[str] = field(default_factory=list)


def _content_of(resp: Any) -> str:
    try:
        return resp.choices[0].message.content or ""
    except (AttributeError, IndexError):
        return ""


def _build_llm_call(cfg: Any) -> Callable[[list[dict]], Any]:
    def _call(messages: list[dict]) -> Any:
        return llm_mod.call_llm(cfg, "update_memory", messages=messages, json_mode=True)

    return _call


# Identity-ish files hold the durable "who is X" beliefs a correction most often targets;
# surface their entity hits first so the update LLM sees the source weight before event noise.
_IDENTITY_PREFIXES = ("user-", "person-", "org-", "project-", "tool-", "topic-", "schema-")


def _target_is_writable(name: str) -> bool:
    """Return whether a correction target has an authoritative write surface.

    Under Markdown authority, SQLite is only a derived mirror. A live-looking row
    whose Markdown file has disappeared is stale and cannot be corrected through
    the normal supersede choke point. Under evomem authority, the canonical node
    store remains writable even when its Markdown projection is temporarily absent.
    """
    try:
        from ..evomem import inversion
        from ..store import files as files_mod

        return inversion.routes_to_engine(name) or files_mod.memory_path(name).exists()
    except (OSError, ValueError):
        return False


def _candidates(cfg: Any, conn: sqlite3.Connection, query: str, cap: int = 16) -> list[Any]:
    """Credit assignment (backprop): trace the wrong output back to the SOURCE fact entries.

    A raw-sentence BM25 search can miss them because correction terms rank
    unrelated entries). So drive retrieval by the ENTITY the correction is about: ``scan_mentions``
    pulls roster entities out of the signal, then a substring scan over live entries finds
    every source fact naming it (identity files first). fts.search is a lexical fallback. Reusing
    the recall path's entity head for the backward pass — same machinery, both directions."""
    out: dict[str, Any] = {}

    def _add(eid: str, path: str, content: str) -> None:
        if eid and eid not in out and _target_is_writable(path):
            out[eid] = SimpleNamespace(id=eid, path=path, content=content)

    try:
        roster = identity.load_roster(cfg)
        ents = [e for e in identity.scan_mentions(query, roster) if len(e) >= 2]
        for ent in ents:
            rows = conn.execute(
                "SELECT id, path, content FROM entries WHERE superseded = 0 AND content LIKE ?"
                " ORDER BY (CASE WHEN "
                + " OR ".join(f"path LIKE '{p}%'" for p in _IDENTITY_PREFIXES)
                + " THEN 0 ELSE 1 END), timestamp DESC LIMIT ?",
                (f"%{ent}%", max(12, cap * 4)),
            ).fetchall()
            for r in rows:
                _add(str(r[0]), str(r[1]), str(r[2]))
    except Exception:  # noqa: BLE001 — entity credit-assignment is best-effort
        logger.debug("entity credit-assignment failed", exc_info=True)

    try:  # lexical fallback (also covers corrections that name no roster entity)
        for h in fts.search(conn, query=query, top_k=max(8, cap * 4)):
            _add(h.id, h.path, h.content)
    except Exception:  # noqa: BLE001
        pass
    return list(out.values())[:cap]


def _normalize_supersedes(
    conn: sqlite3.Connection, planned: list[dict]
) -> tuple[list[dict], list[str]]:
    """Bind LLM-planned targets to the authoritative live SQLite receipt.

    The model is allowed to select only supplied ``file#entry_id`` candidates,
    but the entry id is the stable handle. Resolve its path again at apply time
    so a mistyped file cannot redirect a correction, and reject stale DB rows
    whose Markdown authority has disappeared.
    """
    normalized: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()
    for item in planned:
        eid = str(item.get("entry_id", "")).strip()
        if not eid or eid in seen:
            continue
        seen.add(eid)
        row = conn.execute(
            "SELECT path, superseded, content FROM entries WHERE id = ? LIMIT 1",
            (eid,),
        ).fetchone()
        if row is None:
            errors.append(f"supersede {eid}: receipt is missing from the index")
            continue
        name = str(row[0])
        if bool(row[1]):
            errors.append(f"supersede {name}#{eid}: receipt is already superseded")
            continue
        if not _target_is_writable(name):
            errors.append(
                f"supersede {name}#{eid}: authoritative memory file is missing; "
                "rebuild the index before retrying"
            )
            continue
        target = dict(item)
        target["file"] = name
        target["_previous_content"] = str(row[2] or "")
        normalized.append(target)
    return normalized, errors


def _schema_signature(content: str) -> str:
    """Extract the modeled proposition from a schema body or direct correction."""
    lines = [line.strip() for line in (content or "").splitlines() if line.strip()]
    for line in lines:
        if line.startswith("central:"):
            return line.split(":", 1)[1].strip()
    for line in lines:
        if not line.startswith(("summary:", "inferences:", "-", "<!--")):
            return line
    return ""


def _update_direct_schema_geometry(
    conn: sqlite3.Connection,
    *,
    name: str,
    previous_content: str,
    replacement: str,
) -> list[str]:
    """Keep schema geometry aligned when a derived schema is corrected directly."""
    if not name.startswith("schema-"):
        return []
    from ..store import schema_faces

    changed = schema_faces.supersede_signature(
        conn,
        old_signature=_schema_signature(previous_content),
        new_signature=_schema_signature(replacement),
    )
    if not changed:
        return []
    return [f"updated derived model geometry for {name} ({len(changed)} nodes)"]


def _log_update(
    signal: str,
    kind: str,
    applied: list[str],
    reason: str,
    source: str,
    errors: list[str] | None = None,
) -> None:
    """Append to ``logs/memory-updates.jsonl`` — the RLHF reward signal (a user 'this is wrong'
    is the most valuable label). Fail-open."""
    try:
        from .. import paths

        log_dir = paths.ensure_private_dir(paths.logs_dir())
        row = {
            "ts": datetime.now(UTC).isoformat(),
            "signal": signal[:500],
            "kind": kind,
            "applied": applied,
            "errors": list(errors or []),
            "reason": reason[:300],
            "source": source,  # user (supervised) | agent | observation
        }
        target = log_dir / "memory-updates.jsonl"
        paths.append_private_text(target, json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — logging never blocks the update
        logger.debug("update log write failed", exc_info=True)


def _plan(supersede: list[dict], entity_op: dict | None) -> list[str]:
    """Dry-run preview (the apply writes markdown, so no clean rollback → classify-only preview)."""
    out: list[str] = []
    for s in supersede:
        if isinstance(s, dict) and s.get("file") and s.get("entry_id"):
            verb = "would replace" if str(s.get("replacement", "")).strip() else "would retire"
            out.append(f"{verb} {s.get('file')}#{s.get('entry_id')}")
    if entity_op and entity_op.get("op") in _ENTITY_OPS:
        op, ent = entity_op["op"], entity_op.get("entity")
        tgt = entity_op.get("kind") or entity_op.get("keeper") or ""
        out.append(f"would {op} {ent}{(' → ' + tgt) if tgt else ''}")
    return out


def _apply_entity_op(
    entity_op: dict,
    cfg: Any,
    conn: sqlite3.Connection,
    *,
    signal: str,
    source: str,
) -> list[str]:
    from ..evomem import retype as retype_mod

    op = entity_op.get("op")
    ent = str(entity_op.get("entity", "")).strip()
    if not ent or op not in _ENTITY_OPS:
        return []
    try:
        if op in {"merge_into_self", "reject_owner_alias"}:
            from ..evomem import owner_identity

            if op == "reject_owner_alias":
                state = owner_identity.reject_alias(conn, ent, decision_source=source)
                return [f"rejected owner alias {ent}"] if state is not None else []
            source_id = "correction:" + hashlib.sha1(signal.encode()).hexdigest()[:16]  # noqa: S324
            state = owner_identity.accept_alias(
                conn,
                ent,
                source_id=source_id,
                quote=signal or ent,
                decision_source=source,
            )
            return [f"merged {ent} → self"] if state is not None else []
        if op == "retype":
            retype_mod.retype_entity(ent, str(entity_op.get("kind", "")).strip())
            return [f"retyped {ent} → {entity_op.get('kind')}"]
        if op == "shadow":
            retype_mod.shadow_entity(ent)
            return [f"shadowed {ent}"]
        keeper = str(entity_op.get("keeper", "")).strip()
        if not keeper:
            return []
        retype_mod.merge_alias(ent, keeper, cfg)
        return [f"merged {ent} → {keeper}"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("update_memory: entity op %s on %s failed: %s", op, ent, exc)
        return []


def _reforward(cfg: Any, conn: sqlite3.Connection, files: set[str]) -> tuple[list[str], list[str]]:
    """Re-run the FORWARD pass on the affected path after a weight (fact) update — the closed
    loop: update the weight → re-run forward so the change propagates to the resident apex NOW,
    not on the next daily tick. For each file whose facts changed, re-derive its schema (targeted
    re-mine, reading the CORRECTED ``entries`` projection so it sees the supersede), then
    re-synthesize the level-3 root apex (top of the forward pass). A failed propagation is
    returned to the caller instead of being reported as a successful real-time correction."""
    done: list[str] = []
    errors: list[str] = []
    try:
        from ..writer import root_synthesis
        from ..writer import schema_miner_stage as sm

        # from_evomem=False: read the entries projection, which the choke-point supersede just
        # updated (superseded=1) — evo_nodes may not reflect a markdown-authority correction.
        fact_files = {name for name in files if not name.startswith("schema-")}
        bundles = [
            b
            for b in sm.collect_fact_bundles(conn, from_evomem=False)
            if b.source_path in fact_files
        ]
        if bundles:
            run = sm.mine_bundles_and_write(cfg, conn, bundles)
            written = {w.path for w in run.written}
            expected = {sm.schema_name_for(b.source_path) for b in bundles}
            missing = sorted(expected - written)
            if missing:
                errors.append(f"schema refresh produced no replacement for {missing}")
            else:
                done.append(f"re-derived schema for {sorted(b.source_path for b in bundles)}")
        rr = root_synthesis.run_root_synthesis(cfg, conn)  # re-synth the apex (top of forward)
        if rr.reason in {"written", "disabled"}:
            done.append(f"re-synth root apex ({rr.reason})")
        else:
            errors.append(f"root apex refresh did not produce a replacement ({rr.reason})")
    except Exception as exc:  # noqa: BLE001 — source correction remains durable
        logger.exception("reforward failed")
        errors.append(f"forward refresh failed: {exc}")
    return done, errors


def update_memory(
    cfg: Any,
    conn: sqlite3.Connection,
    signal: str,
    *,
    source: str = "user",
    dry_run: bool = False,
    reforward: bool = True,
    llm_call: Callable[[list[dict]], Any] | None = None,
) -> UpdateResult:
    """Directed memory update: an authoritative statement (``signal``) → the memory delta it
    implies (which beliefs to supersede/replace at the source, or an entity-level op) → applied
    through the SAME executor as observation (``delta_apply``, ⊖ supersede leg). ``source`` marks
    the update's authority (user = supervised label). Injectable ``llm_call`` for tests; ``dry_run``
    previews. Never raises. Downstream Face, Volume, and Root structures re-derive from the updated truth."""
    signal = (signal or "").strip()
    if not signal:
        return UpdateResult("noop", reason="empty", ok=False)
    try:
        hits = _candidates(cfg, conn, signal)  # credit assignment: output error → source weights
        cand_text = (
            "\n".join(f"- [{h.path}#{h.id}] {h.content[:200]}" for h in hits) or "(no candidates)"
        )
        call = llm_call or _build_llm_call(cfg)
        messages = [
            {"role": "system", "content": _PROMPT_PATH.read_text(encoding="utf-8")},
            {
                "role": "user",
                "content": f"## Authoritative information ({source})\n{signal}\n\n"
                f"## Candidate source memories ([file#id] body)\n{cand_text}\n\n"
                "Compute the memory update and return the JSON defined by the system prompt.",
            },
        ]
        parsed = parse_json_object(_content_of(call(messages))) or {}
        planned_supersede = [s for s in (parsed.get("supersede") or []) if isinstance(s, dict)]
        entity_op = parsed.get("entity_op") if isinstance(parsed.get("entity_op"), dict) else None
        reason = str(parsed.get("reason", signal))[:300]

        if not planned_supersede and not (entity_op and entity_op.get("op") in _ENTITY_OPS):
            return UpdateResult("noop", reason=reason, ok=False)
        supersede, errors = _normalize_supersedes(conn, planned_supersede)
        if dry_run:
            return UpdateResult(
                "error" if errors else "update",
                applied=_plan(supersede, entity_op),
                reason=reason,
                ok=False,
                errors=errors,
            )

        applied: list[str] = []
        kind = "update"
        touched_files: set[str] = set()
        if supersede:  # fact/point layer → reuse the shared update executor (delta_apply ⊖ leg)
            for s in supersede:
                delta_item = {k: v for k, v in s.items() if not k.startswith("_")}
                r = delta_apply.apply_delta(conn, cfg, {"supersede": [delta_item]})
                name = str(s["file"])
                eid = str(s["entry_id"])
                if r.supersedes_applied:
                    applied.append(f"superseded {name}#{eid}")
                    touched_files.add(name)
                    try:
                        applied += _update_direct_schema_geometry(
                            conn,
                            name=name,
                            previous_content=str(s.get("_previous_content", "")),
                            replacement=str(s.get("replacement", "")),
                        )
                    except Exception as exc:  # noqa: BLE001 — source correction remains durable
                        logger.exception("direct schema geometry refresh failed")
                        errors.append(f"derived geometry refresh failed for {name}: {exc}")
                else:
                    errors.extend(r.errors or [f"supersede {name}#{eid}: no change applied"])
        if entity_op and entity_op.get("op") in _ENTITY_OPS:  # entity layer → retype verbs
            applied += _apply_entity_op(entity_op, cfg, conn, signal=signal, source=source)
            kind = "entity_update" if not supersede else "update"

        # Closed loop: weight update (backward) → re-run the forward pass on the affected path so
        # the correction reaches the resident apex immediately (not on the next daily tick).
        if reforward and touched_files:
            forward_applied, forward_errors = _reforward(cfg, conn, touched_files)
            applied += forward_applied
            errors += forward_errors

        if errors:
            kind = "error"
        _log_update(signal, kind, applied, reason, source, errors)
        return UpdateResult(
            kind,
            applied=applied,
            reason=reason,
            ok=bool(applied) and not errors,
            errors=errors,
        )
    except Exception:  # noqa: BLE001 — a bad update never crashes the caller
        logger.exception("update_memory failed")
        return UpdateResult("error", ok=False)
