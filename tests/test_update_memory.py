"""update_memory — the directed memory-update entry point (2026-07-04 spec).

Deterministic, mock-LLM. "Correcting a memory" is an UPDATE: a user statement (supervised
label) → the delta it implies → applied through the SAME executor as observation
(``delta_apply`` ⊖ supersede leg). Covers: retire via the ⊖ leg (markdown strike =
supersede-not-delete), replace (with replacement), entity-op routing to retype, noop, dry-run,
and the feedback log.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from persome.store import entries as E
from persome.store import fts
from persome.store import schema_faces as F
from persome.writer import correct as C


def _cfg():
    return SimpleNamespace(
        memory_delta=SimpleNamespace(apply_assertions=False),
        search=SimpleNamespace(default_top_k=5),
        schema=SimpleNamespace(root_synthesis_enabled=False),
    )


def _llm(payload: dict):
    def _call(_messages):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
                )
            ]
        )

    return _call


def _seed_fact(conn, content: str, name: str = "user-profile.md") -> str:
    if not conn.execute("SELECT 1 FROM files WHERE path = ?", (name,)).fetchone():
        E.create_file(conn, name=name, description="identity", tags=["identity"])
    return E.append_entry(conn, name=name, content=content, tags=["identity"])


def _md_body(root, name: str) -> str:
    p = Path(root) / "memory" / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ── the update is a supersede delta through the shared executor ───────────────


def test_correction_supersedes_via_delta_apply(ac_root):
    with fts.cursor() as conn:
        eid = _seed_fact(conn, "\u6843\u5b50 is the user's Feishu display name")
        res = C.update_memory(
            _cfg(),
            conn,
            "\u6843\u5b50\u4e0d\u662f\u6211\u7684\u540d\u5b57\uff0c\u662f Dev \u7fa4\u540c\u4e8b",
            source="user",
            llm_call=_llm(
                {
                    "supersede": [
                        {
                            "file": "user-profile.md",
                            "entry_id": eid,
                            "reason": "\u6843\u5b50\u662f\u540c\u4e8b",
                        }
                    ],
                    "entity_op": None,
                    "reason": "\u6843\u5b50\u975e\u7528\u6237",
                }
            ),
        )
    assert res.kind == "update" and res.ok
    assert any("superseded" in a for a in res.applied)
    # supersede-not-delete: markdown body is STRUCK (~~...~~), bytes survive as a receipt
    body = _md_body(ac_root, "user-profile.md")
    assert "~~\u6843\u5b50 is the user's Feishu display name~~" in body


def test_replace_writes_corrected_fact(ac_root):
    with fts.cursor() as conn:
        eid = _seed_fact(conn, "User lives in Beijing")
        res = C.update_memory(
            _cfg(),
            conn,
            "\u6211\u4f4f\u5728\u4e0a\u6d77\u4e0d\u662f\u5317\u4eac",
            llm_call=_llm(
                {
                    "supersede": [
                        {
                            "file": "user-profile.md",
                            "entry_id": eid,
                            "replacement": "User lives in Shanghai",
                            "reason": "moved",
                        }
                    ],
                    "entity_op": None,
                    "reason": "moved to Shanghai",
                }
            ),
        )
    assert res.ok
    body = _md_body(ac_root, "user-profile.md")
    assert "~~User lives in Beijing~~" in body  # old struck
    assert "User lives in Shanghai" in body  # new written


def test_entity_op_routes_to_retype(ac_root, monkeypatch):
    calls = {}

    def fake_merge(name, keeper, cfg, *, memory=None):
        calls["merge"] = (name, keeper)
        return SimpleNamespace()

    monkeypatch.setattr("persome.evomem.retype.merge_alias", fake_merge)
    with fts.cursor() as conn:
        res = C.update_memory(
            _cfg(),
            conn,
            "\u5c0f\u5f20\u5c31\u662f\u5f20\u4e09",
            llm_call=_llm(
                {
                    "supersede": [],
                    "entity_op": {
                        "op": "merge",
                        "entity": "\u5c0f\u5f20",
                        "keeper": "\u5f20\u4e09",
                    },
                    "reason": "same person",
                }
            ),
        )
    assert res.ok and calls.get("merge") == ("\u5c0f\u5f20", "\u5f20\u4e09")


def test_entity_op_can_merge_identity_into_reserved_self(ac_root):
    with fts.cursor() as conn:
        res = C.update_memory(
            _cfg(),
            conn,
            "Casey-Example is my GitHub handle",
            llm_call=_llm(
                {
                    "supersede": [],
                    "entity_op": {"op": "merge_into_self", "entity": "Casey-Example"},
                    "reason": "owner handle",
                }
            ),
        )
        row = conn.execute(
            "SELECT status, decision_source FROM owner_aliases WHERE alias_key='casey-example'"
        ).fetchone()

    assert res.ok and "merged Casey-Example → self" in res.applied
    assert tuple(row) == ("active", "user")


def test_entity_op_can_reject_false_owner_alias(ac_root):
    with fts.cursor() as conn:
        res = C.update_memory(
            _cfg(),
            conn,
            "Kevin is my teammate, not me",
            llm_call=_llm(
                {
                    "supersede": [],
                    "entity_op": {"op": "reject_owner_alias", "entity": "Kevin"},
                    "reason": "collaborator",
                }
            ),
        )
        status = conn.execute(
            "SELECT status FROM owner_aliases WHERE alias_key='kevin'"
        ).fetchone()[0]

    assert res.ok and status == "rejected"


def test_owner_alias_correction_preserves_agent_provenance(ac_root):
    with fts.cursor() as conn:
        res = C.update_memory(
            _cfg(),
            conn,
            "Casey-Example is the owner's GitHub handle",
            source="agent",
            llm_call=_llm(
                {
                    "supersede": [],
                    "entity_op": {"op": "merge_into_self", "entity": "Casey-Example"},
                    "reason": "owner handle",
                }
            ),
        )
        source = conn.execute(
            "SELECT decision_source FROM owner_aliases WHERE alias_key='casey-example'"
        ).fetchone()[0]

    assert res.ok and source == "agent"


def test_noop_when_nothing_to_update(ac_root):
    with fts.cursor() as conn:
        res = C.update_memory(
            _cfg(),
            conn,
            "\u968f\u4fbf\u8bf4\u8bf4",
            llm_call=_llm(
                {"supersede": [], "entity_op": None, "reason": "\u65e0\u5bf9\u5e94\u6e90"}
            ),
        )
    assert res.kind == "noop" and not res.ok


def test_dry_run_previews_without_applying(ac_root):
    with fts.cursor() as conn:
        eid = _seed_fact(conn, "\u6843\u5b50 is the user's name")
        res = C.update_memory(
            _cfg(),
            conn,
            "\u6843\u5b50\u4e0d\u662f\u6211",
            dry_run=True,
            llm_call=_llm(
                {
                    "supersede": [{"file": "user-profile.md", "entry_id": eid}],
                    "entity_op": None,
                    "reason": "x",
                }
            ),
        )
        assert not res.ok and any("would retire" in a for a in res.applied)
        # NOT applied — body intact
    assert "~~" not in _md_body(ac_root, "user-profile.md")


def test_update_logged_as_feedback_signal(ac_root):
    with fts.cursor() as conn:
        eid = _seed_fact(conn, "wrong fact about the user")
        C.update_memory(
            _cfg(),
            conn,
            "\u90a3\u6761\u9519\u4e86",
            source="user",
            llm_call=_llm(
                {
                    "supersede": [{"file": "user-profile.md", "entry_id": eid, "reason": "r"}],
                    "entity_op": None,
                    "reason": "r",
                }
            ),
        )
    log = Path(ac_root) / "logs" / "memory-updates.jsonl"
    assert log.exists()
    assert log.stat().st_mode & 0o777 == 0o600
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert row["source"] == "user" and row["kind"] == "update"
    assert row["errors"] == []


def test_correction_rebinds_target_path_from_receipt(ac_root):
    with fts.cursor() as conn:
        face_id = F.record_face(
            conn,
            source="mined",
            signature="Valse is coding-agent infrastructure",
            members=["fact-a", "fact-b"],
        )
        F.record_face(
            conn,
            source="emergent",
            signature="Valse is coding-agent infrastructure",
            members=["fact-a", "fact-b"],
        )
        assert F.maybe_promote(conn, face_id)
        eid = _seed_fact(
            conn,
            "central: Valse is coding-agent infrastructure",
            name="schema-org-valse-ai.md",
        )
        res = C.update_memory(
            _cfg(),
            conn,
            "Valse no longer builds coding-agent infrastructure",
            reforward=False,
            llm_call=_llm(
                {
                    "supersede": [
                        {
                            # A model-provided path must never override the stable receipt.
                            "file": "org-valse-ai.md",
                            "entry_id": eid,
                            "reason": "positioning changed",
                            "replacement": "Valse helps individuals restore continuous state",
                        }
                    ],
                    "entity_op": None,
                    "reason": "positioning changed",
                }
            ),
        )

    assert res.ok and res.errors == []
    assert res.applied == [
        f"superseded schema-org-valse-ai.md#{eid}",
        "updated derived model geometry for schema-org-valse-ai.md (2 nodes)",
    ]
    assert "~~central: Valse is coding-agent infrastructure~~" in _md_body(
        ac_root, "schema-org-valse-ai.md"
    )
    with fts.cursor() as conn:
        old = conn.execute(
            "SELECT status FROM schema_faces WHERE face_id = ?", (face_id,)
        ).fetchone()
        replacement = conn.execute(
            "SELECT signature, status FROM schema_faces"
            " WHERE signature = 'Valse helps individuals restore continuous state'"
        ).fetchone()
    assert old[0] == "superseded"
    assert tuple(replacement) == (
        "Valse helps individuals restore continuous state",
        "active",
    )


def test_correction_does_not_claim_missing_markdown_target_was_applied(ac_root, monkeypatch):
    with fts.cursor() as conn:
        eid = _seed_fact(
            conn,
            "Valse is coding-agent infrastructure",
            name="org-valse-ai.md",
        )
        (Path(ac_root) / "memory" / "org-valse-ai.md").unlink()

        def unexpected_reforward(*_args, **_kwargs):
            raise AssertionError("failed source writes must not regenerate derived memory")

        monkeypatch.setattr(C, "_reforward", unexpected_reforward)
        res = C.update_memory(
            _cfg(),
            conn,
            "Valse no longer builds coding-agent infrastructure",
            llm_call=_llm(
                {
                    "supersede": [
                        {
                            "file": "org-valse-ai.md",
                            "entry_id": eid,
                            "reason": "positioning changed",
                        }
                    ],
                    "entity_op": None,
                    "reason": "positioning changed",
                }
            ),
        )
        superseded = conn.execute("SELECT superseded FROM entries WHERE id = ?", (eid,)).fetchone()[
            0
        ]

    assert res.kind == "error" and not res.ok
    assert res.applied == []
    assert any("authoritative memory file is missing" in error for error in res.errors)
    assert superseded == 0


def test_candidates_skip_stale_rows_without_authoritative_file(ac_root):
    with fts.cursor() as conn:
        stale_id = _seed_fact(
            conn,
            "Valse is coding-agent infrastructure",
            name="org-valse-ai.md",
        )
        live_id = _seed_fact(
            conn,
            "Valse product positioning summary",
            name="schema-org-valse-ai.md",
        )
        (Path(ac_root) / "memory" / "org-valse-ai.md").unlink()

        hits = C._candidates(_cfg(), conn, "Valse product positioning")

    ids = {hit.id for hit in hits}
    assert stale_id not in ids
    assert live_id in ids


def test_forward_refresh_failure_is_not_reported_as_success(ac_root, monkeypatch):
    with fts.cursor() as conn:
        eid = _seed_fact(conn, "User lives in Beijing")
        monkeypatch.setattr(
            C,
            "_reforward",
            lambda *_args, **_kwargs: ([], ["schema refresh produced no replacement"]),
        )
        res = C.update_memory(
            _cfg(),
            conn,
            "User lives in Shanghai",
            llm_call=_llm(
                {
                    "supersede": [
                        {
                            "file": "user-profile.md",
                            "entry_id": eid,
                            "replacement": "User lives in Shanghai",
                            "reason": "moved",
                        }
                    ],
                    "entity_op": None,
                    "reason": "moved",
                }
            ),
        )

    assert res.kind == "error" and not res.ok
    assert res.applied == [f"superseded user-profile.md#{eid}"]
    assert res.errors == ["schema refresh produced no replacement"]


def test_skipped_root_refresh_is_reported_as_incomplete(ac_root, monkeypatch):
    monkeypatch.setattr(
        "persome.writer.root_synthesis.run_root_synthesis",
        lambda *_args, **_kwargs: SimpleNamespace(reason="skip_hallucination"),
    )
    with fts.cursor() as conn:
        applied, errors = C._reforward(
            _cfg(),
            conn,
            {"schema-org-valse-ai.md"},
        )

    assert applied == []
    assert errors == ["root apex refresh did not produce a replacement (skip_hallucination)"]
