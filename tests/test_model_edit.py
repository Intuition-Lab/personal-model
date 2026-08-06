"""Owner corrections to the live personal model.

The regression this whole feature exists to prevent is at the top: derivation
used to overwrite anything a human wrote.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from persome.api import build_api_app
from persome.evidence import OWNER_EDIT_TAG
from persome.evomem import backfill
from persome.model.edit import apply_model_edit
from persome.model.snapshot import build_snapshot, validate_snapshot
from persome.store import entries as entries_mod
from persome.store import fts
from persome.store import schema_faces as sf

# ── fixtures ──────────────────────────────────────────────────────────────


def _seed_point(content: str = "Alex reserves mornings for focused writing.") -> str:
    """Create one Point through the production path and return its node_id."""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="person-alex.md", description="alex", tags=["t"])
        entries_mod.append_entry(conn, name="person-alex.md", content=content, tags=["topic:work"])
    assert backfill.run_backfill().ok
    with fts.cursor() as conn:
        row = conn.execute("SELECT node_id FROM evo_nodes LIMIT 1").fetchone()
    return str(row[0])


def _seed_face(signature: str = "Alex protects deep work.", level: int = 1) -> str:
    with fts.cursor() as conn:
        face_id = sf.record_face(
            conn,
            source=sf.PROVENANCE_MINED,
            signature=signature,
            members=["m1", "m2", "m3"],
            level=level,
        )
        conn.execute("UPDATE schema_faces SET status = 'active' WHERE face_id = ?", (face_id,))
    return face_id


def _live(face_id: str) -> dict:
    with fts.cursor() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM schema_faces WHERE face_id = ?", (face_id,)).fetchone()
    return dict(row)


# ── the core regression ───────────────────────────────────────────────────


def test_re_mine_does_not_overwrite_an_authored_signature(ac_root) -> None:
    """A re-mine used to clobber the owner's wording. That is the whole bug."""
    face_id = _seed_face("Alex works late.")

    with fts.cursor() as conn:
        result = apply_model_edit(
            conn,
            kind="face",
            target_id=face_id,
            op="rewrite",
            replacement="Alex works late by choice, not pressure.",
        )
    assert result.ok
    assert result.prior_text == "Alex works late."

    # The schema miner reaches the same regularity again, with its own wording.
    with fts.cursor() as conn:
        again = sf.record_face(
            conn,
            source=sf.PROVENANCE_EMERGENT,
            signature="Alex works late.",
            members=["m1", "m2", "m3"],
        )
    assert again == face_id, "the re-mine must fold onto the same object"

    row = _live(face_id)
    assert row["signature"] == "Alex works late by choice, not pressure."
    assert row["provenance"] == sf.PROVENANCE_AUTHORED


def test_authored_object_keeps_accruing_evidence(ac_root) -> None:
    """Authoring pins the wording, not the object. Evidence must keep moving."""
    face_id = _seed_face("Alex protects deep work.")
    before = _live(face_id)["observations"]

    with fts.cursor() as conn:
        apply_model_edit(
            conn, kind="face", target_id=face_id, op="rewrite", replacement="I guard my mornings."
        )
        sf.record_face(
            conn,
            source=sf.PROVENANCE_EMERGENT,
            signature="Alex protects deep work.",
            members=["m1", "m2", "m3"],
            confidence=0.9,
        )

    row = _live(face_id)
    assert row["observations"] == before + 1
    assert row["confidence"] == pytest.approx(0.9)
    assert row["signature"] == "I guard my mornings."


def test_authored_root_survives_root_synthesis(ac_root, monkeypatch) -> None:
    """`upsert_root` replaces the apex row wholesale; the gate must stop it."""
    from persome import config as config_mod
    from persome.writer import root_synthesis

    root_id = _seed_face("A person becoming more deliberate.", level=3)
    with fts.cursor() as conn:
        apply_model_edit(
            conn,
            kind="root",
            target_id=root_id,
            op="rewrite",
            replacement="I am learning to say no.",
        )

    cfg = config_mod.load()
    with fts.cursor() as conn:
        result = root_synthesis.synthesize_root(
            cfg,
            conn,
            llm_call=lambda _messages: pytest.fail("an authored root must not spend an LLM call"),
        )

    assert result.reason == "skip_authored"
    assert result.face_id == root_id
    assert _live(root_id)["signature"] == "I am learning to say no."


# ── retire ────────────────────────────────────────────────────────────────


def test_retiring_a_face_removes_it_but_keeps_it_queryable(ac_root) -> None:
    face_id = _seed_face("Alex avoids meetings before noon.")

    with fts.cursor() as conn:
        assert apply_model_edit(conn, kind="face", target_id=face_id, op="retire").ok
        snapshot = build_snapshot(conn, redact=False)

    assert all(face["id"] != face_id for face in snapshot["faces"])
    row = _live(face_id)
    assert row["status"] == "archived"
    # Withdrawal is not deletion: the evidence it stood on is still there.
    assert json.loads(row["members"]) == ["m1", "m2", "m3"]
    assert row["observations"] >= 1


def test_retiring_a_point_removes_it_from_the_live_model(ac_root) -> None:
    point_id = _seed_point()

    with fts.cursor() as conn:
        assert apply_model_edit(conn, kind="point", target_id=point_id, op="retire").ok
        snapshot = build_snapshot(conn, redact=False)
        retired = conn.execute(
            "SELECT content, valid_until FROM evo_nodes WHERE node_id = ?", (point_id,)
        ).fetchone()

    assert all(point["id"] != point_id for point in snapshot["points"])
    assert retired is not None and retired[1], "the retirement must be stamped, not erased"


def test_retiring_the_last_geometry_degrades_rather_than_fabricates(ac_root) -> None:
    """Emptying the model must read as degraded, never as a placeholder claim."""
    face_id = _seed_face("Alex protects deep work.")
    with fts.cursor() as conn:
        apply_model_edit(conn, kind="face", target_id=face_id, op="retire")
        snapshot = build_snapshot(conn, redact=False)

    validate_snapshot(snapshot)
    assert snapshot["faces"] == []
    assert snapshot["root"] is None
    assert snapshot["stats"]["faces"] == 0


# ── Point corrections ─────────────────────────────────────────────────────


def test_point_rewrite_supersedes_and_marks_owner_authorship(ac_root) -> None:
    point_id = _seed_point()

    with fts.cursor() as conn:
        result = apply_model_edit(
            conn,
            kind="point",
            target_id=point_id,
            op="rewrite",
            replacement="I reserve mornings for writing because afternoons get taken.",
            reason="the model guessed at my reason",
        )
    assert result.ok
    assert result.new_id and result.new_id != point_id

    with fts.cursor() as conn:
        conn.row_factory = sqlite3.Row
        new = conn.execute(
            "SELECT content, tags, supersedes FROM evo_nodes WHERE node_id = ?",
            (result.new_id,),
        ).fetchone()

    assert OWNER_EDIT_TAG in str(new["tags"]).split()
    assert "topic:work" in str(new["tags"]).split(), "existing tags must survive"
    assert point_id in json.loads(new["supersedes"])


def test_point_rewrite_survives_a_model_build(ac_root, monkeypatch) -> None:
    """The durability claim, exercised against the real build entrypoint."""
    from persome import config as config_mod
    from persome.model import build as build_mod

    point_id = _seed_point()
    with fts.cursor() as conn:
        result = apply_model_edit(
            conn,
            kind="point",
            target_id=point_id,
            op="rewrite",
            replacement="I reserve mornings for writing.",
        )
    assert result.ok

    build_mod.run_model_build(config_mod.load())

    with fts.cursor() as conn:
        snapshot = build_snapshot(conn, redact=False)
    contents = [point["content"] for point in snapshot["points"]]
    assert "I reserve mornings for writing." in contents


def test_the_predecessor_stays_as_history(ac_root) -> None:
    point_id = _seed_point()
    with fts.cursor() as conn:
        result = apply_model_edit(
            conn, kind="point", target_id=point_id, op="rewrite", replacement="I write at dawn."
        )
        snapshot = build_snapshot(conn, redact=False)

    ids = {point["id"] for point in snapshot["points"]}
    assert point_id in ids, "a superseded Point is history, not a withdrawal"
    assert any(
        line["source"] == point_id and line["target"] == result.new_id for line in snapshot["lines"]
    )


def test_a_face_keeps_its_member_receipts_after_a_point_is_rewritten(ac_root) -> None:
    """H1: member keys are content hashes, so a correction used to break them."""
    original = "Alex reserves mornings for focused writing."
    point_id = _seed_point(original)

    with fts.cursor() as conn:
        face_id = sf.record_face(
            conn,
            source=sf.PROVENANCE_MINED,
            signature="Alex protects deep work.",
            members=[sf.member_key(original)],
        )
        conn.execute("UPDATE schema_faces SET status = 'active' WHERE face_id = ?", (face_id,))
        before = build_snapshot(conn, redact=False)

    face_before = next(face for face in before["faces"] if face["id"] == face_id)
    assert face_before["member_receipts"], "precondition: the Face resolves its member"

    with fts.cursor() as conn:
        result = apply_model_edit(
            conn,
            kind="point",
            target_id=point_id,
            op="rewrite",
            replacement="I reserve mornings for writing, and I protect that.",
        )
        after = build_snapshot(conn, redact=False)

    face_after = next(face for face in after["faces"] if face["id"] == face_id)
    assert face_after["member_receipts"], "the Face must not lose its evidence to a typo fix"
    # And it points at the current wording, not the text the owner replaced.
    assert any(result.new_id in receipt for receipt in face_after["member_receipts"])


# ── evidence honesty ──────────────────────────────────────────────────────


def test_owner_authored_facts_get_no_fabricated_context(ac_root) -> None:
    """H2: a typed assertion must not collect whatever was on screen as proof."""
    from persome.evidence import resolve_evidence

    point_id = _seed_point()
    with fts.cursor() as conn:
        conn.execute(
            "INSERT INTO captures (id, timestamp, app_name, window_title, visible_text)"
            " VALUES ('cap-1', datetime('now'), 'Mail', 'Inbox', 'unrelated')"
        )
        result = apply_model_edit(
            conn, kind="point", target_id=point_id, op="rewrite", replacement="I write at dawn."
        )
        resolved = resolve_evidence(conn, result.new_id)

    assert resolved.get("context") == []


def test_every_edit_records_the_text_it_displaced(ac_root) -> None:
    face_id = _seed_face("Alex works late.")
    with fts.cursor() as conn:
        apply_model_edit(
            conn,
            kind="face",
            target_id=face_id,
            op="rewrite",
            replacement="I work late by choice.",
            reason="tone",
        )
        rows = conn.execute(
            "SELECT payload FROM memory_deltas WHERE session_id = 'owner-edit'"
        ).fetchall()

    assert len(rows) == 1
    edit = json.loads(rows[0][0])["owner_edit"]
    assert edit["prior_text"] == "Alex works late."
    assert edit["new_text"] == "I work late by choice."
    assert edit["reason"] == "tone"


# ── rejections ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "target", "op", "replacement", "expected"),
    [
        ("line", "edge-1", "rewrite", "x", "unknown_kind"),
        ("face", "face-1", "merge", "x", "unknown_op"),
        ("face", "", "rewrite", "x", "missing_target"),
        ("face", "face-1", "rewrite", "", "empty_replacement"),
        ("face", "face-missing", "rewrite", "x", "unknown_object"),
        ("point", "point-missing", "rewrite", "x", "unknown_point"),
    ],
)
def test_invalid_edits_are_refused_by_reason(
    ac_root, kind, target, op, replacement, expected
) -> None:
    _seed_point()  # so `evo_nodes` exists and a missing id is a miss, not an outage
    with fts.cursor() as conn:
        result = apply_model_edit(conn, kind=kind, target_id=target, op=op, replacement=replacement)
    assert not result.ok
    assert result.reason == expected


def test_editing_a_volume_through_the_face_kind_is_refused(ac_root) -> None:
    """A level mismatch would retire the wrong tier of the geometry."""
    volume_id = _seed_face("Alex's work and study rhythms rhyme.", level=2)
    with fts.cursor() as conn:
        result = apply_model_edit(
            conn, kind="face", target_id=volume_id, op="rewrite", replacement="x"
        )
    assert not result.ok
    assert result.reason == "kind_level_mismatch"


def test_a_retired_point_cannot_be_retired_twice(ac_root) -> None:
    point_id = _seed_point()
    with fts.cursor() as conn:
        assert apply_model_edit(conn, kind="point", target_id=point_id, op="retire").ok
        again = apply_model_edit(conn, kind="point", target_id=point_id, op="retire")
    assert not again.ok
    assert again.reason == "point_already_retired"


# ── HTTP surface ──────────────────────────────────────────────────────────


def test_route_applies_an_edit_and_reports_the_new_id(ac_root) -> None:
    point_id = _seed_point()
    client = TestClient(build_api_app(auth_enabled=False))

    response = client.post(
        "/model/edit",
        json={
            "schema_version": 1,
            "kind": "point",
            "id": point_id,
            "op": "rewrite",
            "replacement": "I write at dawn.",
            "reason": "clarity",
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["new_id"] and data["new_id"] != point_id
    assert data["shadow_misses"] == 0


def test_route_rejects_an_unknown_object_with_a_reason(ac_root) -> None:
    client = TestClient(build_api_app(auth_enabled=False))
    response = client.post(
        "/model/edit",
        json={
            "schema_version": 1,
            "kind": "face",
            "id": "face-nope",
            "op": "rewrite",
            "replacement": "x",
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "unknown_object"


@pytest.mark.parametrize(
    "body",
    [
        {"schema_version": 1, "kind": "face", "id": "f", "op": "rewrite", "replacement": ""},
        {"schema_version": 1, "kind": "face", "id": "f", "op": "retire", "replacement": "x"},
        {"schema_version": 1, "kind": "line", "id": "f", "op": "rewrite", "replacement": "x"},
        {"schema_version": 2, "kind": "face", "id": "f", "op": "rewrite", "replacement": "x"},
    ],
)
def test_route_validates_the_body(ac_root, body) -> None:
    client = TestClient(build_api_app(auth_enabled=False))
    assert client.post("/model/edit", json=body).status_code == 422


def test_edit_route_requires_authentication(ac_root, monkeypatch) -> None:
    """The write route must never be reachable without a credential."""
    from persome.env_file import LOCAL_API_TOKEN_ENV

    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, "x" * 43)
    client = TestClient(build_api_app(auth_enabled=True))
    response = client.post(
        "/model/edit",
        json={"schema_version": 1, "kind": "face", "id": "f", "op": "retire"},
    )
    assert response.status_code == 401


# ── the agent guarantee ───────────────────────────────────────────────────


def test_corrections_are_visible_through_the_mcp_snapshot(ac_root) -> None:
    """Edits must reach agents, not only the viewer."""
    from persome.mcp import server as mcp_server

    face_id = _seed_face("Alex works late.")
    with fts.cursor() as conn:
        apply_model_edit(
            conn,
            kind="face",
            target_id=face_id,
            op="rewrite",
            replacement="I work late by choice.",
        )

    with fts.cursor() as conn:
        snapshot = mcp_server._ModelSnapshotCache().get(conn, redact=False)

    face = next(item for item in snapshot["faces"] if item["id"] == face_id)
    assert face["signature"] == "I work late by choice."
    assert face["provenance"] == sf.PROVENANCE_AUTHORED


# ── regressions found by adversarial review ───────────────────────────────


def test_an_ended_fact_is_not_mistaken_for_a_withdrawn_one(ac_root) -> None:
    """`valid_until` also marks a relationship that ended, not just a retirement.

    Those Points stay `is_latest`, and dropping them would delete real history
    from the model the first time someone stopped working somewhere.
    """
    point_id = _seed_point("Alex worked at Acme.")
    with fts.cursor() as conn:
        conn.execute(
            "UPDATE evo_nodes SET valid_until = ? WHERE node_id = ?",
            ("2026-08-01T00:00:00+00:00", point_id),
        )
        snapshot = build_snapshot(conn, redact=False)

    assert any(point["id"] == point_id for point in snapshot["points"])


def test_a_rejected_face_is_not_resurrected_by_the_next_mine(ac_root) -> None:
    """Archiving alone left the row live to `_find_match`, and `maybe_promote`
    handed it straight back to ACTIVE on the next tick."""
    face_id = _seed_face("Alex works late.")
    with fts.cursor() as conn:
        assert apply_model_edit(conn, kind="face", target_id=face_id, op="retire").ok
        sf.record_face(
            conn,
            source=sf.PROVENANCE_EMERGENT,
            signature="Alex works late.",
            members=["m1", "m2", "m3"],
        )
        sf.maybe_promote(conn, face_id)
        snapshot = build_snapshot(conn, redact=False)

    assert all(face["id"] != face_id for face in snapshot["faces"])


def test_a_rejected_root_is_not_regenerated_by_the_next_synthesis(ac_root) -> None:
    from persome import config as config_mod
    from persome.writer import root_synthesis

    root_id = _seed_face("A person becoming more deliberate.", level=3)
    with fts.cursor() as conn:
        assert apply_model_edit(conn, kind="root", target_id=root_id, op="retire").ok
        result = root_synthesis.synthesize_root(
            cfg := config_mod.load(),
            conn,
            llm_call=lambda _m: pytest.fail("a rejected root must not be re-synthesized"),
        )
        assert cfg is not None
        snapshot = build_snapshot(conn, redact=False)

    assert result.reason == "skip_authored"
    assert snapshot["root"] is None


def test_correcting_a_superseded_point_is_refused(ac_root) -> None:
    """Two live corrections of one fact would each claim to be current."""
    point_id = _seed_point()
    with fts.cursor() as conn:
        first = apply_model_edit(
            conn, kind="point", target_id=point_id, op="rewrite", replacement="First correction."
        )
        assert first.ok
        second = apply_model_edit(
            conn, kind="point", target_id=point_id, op="rewrite", replacement="Second correction."
        )
    assert not second.ok
    assert second.reason == "point_superseded"


def test_a_twice_corrected_fact_resolves_to_its_current_wording(ac_root) -> None:
    """A -> B -> C: the Face that mined A must cite C, not the intermediate."""
    original = "Alex reserves mornings for focused writing."
    point_id = _seed_point(original)

    with fts.cursor() as conn:
        face_id = sf.record_face(
            conn,
            source=sf.PROVENANCE_MINED,
            signature="Alex protects deep work.",
            members=[sf.member_key(original)],
        )
        conn.execute("UPDATE schema_faces SET status = 'active' WHERE face_id = ?", (face_id,))
        first = apply_model_edit(
            conn, kind="point", target_id=point_id, op="rewrite", replacement="I write at dawn."
        )
        second = apply_model_edit(
            conn,
            kind="point",
            target_id=first.new_id,
            op="rewrite",
            replacement="I write before anyone else is awake.",
        )
        snapshot = build_snapshot(conn, redact=False)

    assert second.ok
    face = next(item for item in snapshot["faces"] if item["id"] == face_id)
    assert any(second.new_id in receipt for receipt in face["member_receipts"])


def test_a_point_in_an_append_only_log_is_refused(ac_root) -> None:
    """A supersede there rewrites markdown and never moves the Point, so
    reporting success would be a lie."""
    from persome.model.edit import _point_file_is_editable

    assert _point_file_is_editable("person-alex.md")
    assert not _point_file_is_editable("event-2026-08-05.md")
    assert not _point_file_is_editable("skills/writing.md")


def test_a_faces_correction_history_is_replayable(ac_root) -> None:
    """In-place correction keeps `face_id`, so `schema_faces` holds only the
    current wording. The sequence must still be reconstructable end to end."""
    face_id = _seed_face("Alex works late.")
    with fts.cursor() as conn:
        for text in ("I work late by choice.", "I choose my hours."):
            assert apply_model_edit(
                conn, kind="face", target_id=face_id, op="rewrite", replacement=text
            ).ok
        rows = conn.execute(
            "SELECT payload FROM memory_deltas WHERE session_id = 'owner-edit' ORDER BY id"
        ).fetchall()

    chain = [json.loads(row[0])["owner_edit"] for row in rows]
    assert [(edit["prior_text"], edit["new_text"]) for edit in chain] == [
        ("Alex works late.", "I work late by choice."),
        ("I work late by choice.", "I choose my hours."),
    ]
    assert _live(face_id)["signature"] == "I choose my hours."


def test_retiring_a_face_does_not_cascade_to_its_volume(ac_root) -> None:
    """Rejecting one regularity is not a claim about the pattern built over it.

    The Volume survives and the rebuild the retirement schedules re-derives it
    from what is still live.
    """
    child = _seed_face("Alex guards mornings.")
    volume = _seed_face("Alex structures time deliberately.", level=2)
    with fts.cursor() as conn:
        conn.execute(
            "UPDATE schema_faces SET members = ?, parent_face = NULL WHERE face_id = ?",
            (json.dumps([child]), volume),
        )
        conn.execute("UPDATE schema_faces SET parent_face = ? WHERE face_id = ?", (volume, child))
        assert apply_model_edit(conn, kind="face", target_id=child, op="retire").ok
        snapshot = build_snapshot(conn, redact=False)
        dirty = conn.execute(
            "SELECT value FROM system_state WHERE key = 'model_structure_dirty'"
        ).fetchone()

    validate_snapshot(snapshot)
    assert all(face["id"] != child for face in snapshot["faces"])
    assert any(item["id"] == volume for item in snapshot["volumes"])
    assert dirty is not None and int(dirty[0]) >= 1, "the rebuild must be scheduled"


# ── regressions found by the second adversarial pass ──────────────────────


def test_a_correction_cannot_forge_a_memory_entry(ac_root) -> None:
    """Memory files are re-parsed by splitting on entry headings, and a
    correction body is written into one verbatim. A heading-shaped line would
    truncate the correction, overwrite the predecessor's canonical content, and
    break rebuild_index permanently on the duplicate id."""
    point_id = _seed_point()
    forged = (
        "I write in the mornings by choice.\n"
        f"## [2020-01-01T00:00] {{id: {point_id}}} #fact\n"
        "and here is the rest of what I wanted to say."
    )
    with fts.cursor() as conn:
        result = apply_model_edit(
            conn, kind="point", target_id=point_id, op="rewrite", replacement=forged
        )
    assert not result.ok
    assert result.reason == "replacement_forges_an_entry"

    # And the store is untouched: the index still rebuilds.
    with fts.cursor() as conn:
        entries_mod.rebuild_index(conn)


def test_a_correction_may_still_contain_ordinary_markdown(ac_root) -> None:
    """The guard targets one exact shape, not markdown in general."""
    from persome.model.edit import replacement_forges_an_entry

    assert not replacement_forges_an_entry("## A heading I typed")
    assert not replacement_forges_an_entry("I use {braces} and [brackets].")
    assert replacement_forges_an_entry("## [2020-01-01T00:00] {id: abc-123} #fact")


def test_a_rejected_face_is_not_re_created_as_a_twin(ac_root) -> None:
    """Closing the retired row stops it being revived — on its own that only
    means derivation mints an identical row beside it and promotes that.

    Asserting on ids alone (as an earlier test did) misses this entirely,
    because the twin has a different id.
    """
    face_id = _seed_face("Alex works late.")
    with fts.cursor() as conn:
        assert apply_model_edit(conn, kind="face", target_id=face_id, op="retire").ok
        for _ in range(3):
            returned = sf.record_face(
                conn,
                source=sf.PROVENANCE_MINED,
                signature="Alex works late.",
                members=["m1", "m2", "m3"],
            )
            sf.maybe_promote(conn, returned)
        snapshot = build_snapshot(conn, redact=False)

    assert returned == face_id, "the rejection must absorb the re-derivation"
    signatures = [face["signature"] for face in snapshot["faces"]]
    assert "Alex works late." not in signatures


def test_an_ended_but_current_fact_can_still_be_corrected(ac_root) -> None:
    """The snapshot keeps these Points, so the editor must accept them too."""
    point_id = _seed_point("Alex worked at Acme.")
    with fts.cursor() as conn:
        conn.execute(
            "UPDATE evo_nodes SET valid_until = ? WHERE node_id = ?",
            ("2026-08-01T00:00:00+00:00", point_id),
        )
        snapshot = build_snapshot(conn, redact=False)
        assert any(point["id"] == point_id for point in snapshot["points"])
        result = apply_model_edit(
            conn,
            kind="point",
            target_id=point_id,
            op="rewrite",
            replacement="I worked at Acme until this spring.",
        )
    assert result.ok, f"a rendered Point must be correctable, got {result.reason}"


def test_rewriting_an_unpromoted_object_is_refused(ac_root) -> None:
    """Marking a SHADOW object authored would make it permanently unpromotable
    — invisible forever, while the save claimed success."""
    with fts.cursor() as conn:
        face_id = sf.record_face(
            conn, source=sf.PROVENANCE_MINED, signature="A tentative pattern.", members=["m1"]
        )
        result = apply_model_edit(
            conn, kind="face", target_id=face_id, op="rewrite", replacement="My words."
        )
        # Rejecting one is still coherent: withdrawing what never surfaced.
        retired = apply_model_edit(conn, kind="face", target_id=face_id, op="retire")
    assert not result.ok
    assert result.reason == "object_not_active"
    assert retired.ok


def test_ipv6_loopback_is_accepted_by_the_origin_guard(ac_root) -> None:
    """`urlsplit().hostname` strips the brackets, and port-stripping then ate
    the address, so the viewer's own POST was refused over IPv6."""
    from persome.api import _hostname_of, _is_local_host

    assert _hostname_of("::1") == "::1"
    assert _is_local_host("::1")
    assert _is_local_host("[::1]:8742")
    assert not _is_local_host("evil.com")


def test_a_root_correction_made_mid_synthesis_is_not_overwritten(ac_root) -> None:
    """Synthesis spends seconds in an LLM call. An apex the owner settles during
    that window must not be replaced by the answer that was already in flight."""
    from persome import config as config_mod
    from persome.writer import root_synthesis

    _seed_face("Alex structures time deliberately.", level=2)  # so synthesis has input
    root_id = _seed_face("A person becoming more deliberate.", level=3)

    def _llm_that_edits_meanwhile(_messages):
        with fts.cursor() as inner:
            apply_model_edit(
                conn=inner,
                kind="root",
                target_id=root_id,
                op="rewrite",
                replacement="I am not that person.",
            )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content='{"apex": "A machine apex."}'))
            ]
        )

    with fts.cursor() as conn:
        result = root_synthesis.synthesize_root(
            config_mod.load(), conn, llm_call=_llm_that_edits_meanwhile
        )
        snapshot = build_snapshot(conn, redact=False)

    assert result.reason == "skip_authored"
    assert snapshot["root"]["signature"] == "I am not that person."
    assert snapshot["root"]["provenance"] == sf.PROVENANCE_AUTHORED


def test_a_withdrawn_fact_is_not_re_minted_by_the_next_observation(ac_root) -> None:
    """The dedupe guard only sees is_latest rows, so a rejected Point used to
    come back the next time the owner did the thing."""
    from persome import config as config_mod
    from persome.writer import delta_apply

    text = "Alex works at Acme as a staff engineer."
    point_id = _seed_point(text)
    with fts.cursor() as conn:
        assert apply_model_edit(conn, kind="point", target_id=point_id, op="retire").ok

    delta = {
        "entities": [{"canonical": "Alex", "kind": "person"}],
        "assertions": [{"subject": {"canonical": "Alex"}, "text": text}],
    }
    with fts.cursor() as conn:
        result = delta_apply.apply_delta(conn, config_mod.load(), delta)
        snapshot = build_snapshot(conn, redact=False)

    assert result.assertions_minted == 0, "a withdrawn fact must not be re-minted"
    assert all(point["content"] != text for point in snapshot["points"])


def test_a_differently_worded_observation_still_lands(ac_root) -> None:
    """Withdrawal suppresses the exact claim, not the subject."""
    from persome import config as config_mod
    from persome.writer import delta_apply

    point_id = _seed_point("Alex works at Acme as a staff engineer.")
    with fts.cursor() as conn:
        assert apply_model_edit(conn, kind="point", target_id=point_id, op="retire").ok
        result = delta_apply.apply_delta(
            conn,
            config_mod.load(),
            {
                "entities": [{"canonical": "Alex", "kind": "person"}],
                "assertions": [
                    {"subject": {"canonical": "Alex"}, "text": "Alex mentors two new engineers."}
                ],
            },
        )
    assert result.assertions_minted == 1


def test_compaction_refuses_to_strip_owner_authorship(ac_root) -> None:
    """The 95% token gate is blind to this: dropping a tag costs a few tokens
    out of hundreds while reinstating a claim the owner corrected away."""
    from persome.writer.compact import _owner_authorship_lost

    before = (
        "## [t] {id: a} #fact #source:owner-edit\nMy own wording.\n"
        "## [t] {id: b} #fact #superseded-by:a\n~~The old claim.~~\n"
    )
    assert _owner_authorship_lost(before, before) == ""
    stripped_tag = before.replace(" #source:owner-edit", "")
    assert "owner-edit tags" in _owner_authorship_lost(before, stripped_tag)
    reinstated = before.replace(" #superseded-by:a", "").replace("~~", "")
    assert "supersede markers" in _owner_authorship_lost(before, reinstated)
    # Ordinary compression that keeps both markers is unaffected.
    compressed = before.replace("My own wording.", "My wording.")
    assert _owner_authorship_lost(before, compressed) == ""


def test_an_orphan_reaped_fact_is_still_free_to_come_back(ac_root) -> None:
    """Housekeeping is not a decision.

    The orphan reaper retires an unreferenced Point after its TTL, producing the
    same row shape as an owner rejection: shadow, end-dated, no successor.
    Inferring intent from that shape would mean a fact that merely aged out
    could never return, even once the owner starts doing it again.
    """
    from persome import config as config_mod
    from persome.evomem.engine import EvoMemory
    from persome.evomem.store import NodeStore
    from persome.writer import delta_apply

    text = "Alex works at Acme as a staff engineer."
    point_id = _seed_point(text)

    # Exactly what writer/orphan_reaper.py does to a TTL'd orphan.
    EvoMemory().commit_retire(point_id, valid_until="2026-08-01T00:00:00+00:00")
    assert NodeStore() is not None

    with fts.cursor() as conn:
        row = conn.execute(
            "SELECT is_latest, valid_until, superseded_by FROM evo_nodes WHERE node_id = ?",
            (point_id,),
        ).fetchone()
        assert row[0] == 0 and row[1] and row[2] in ("[]", None), "precondition: looks retired"
        result = delta_apply.apply_delta(
            conn,
            config_mod.load(),
            {
                "entities": [{"canonical": "Alex", "kind": "person"}],
                "assertions": [{"subject": {"canonical": "Alex"}, "text": text}],
            },
        )

    assert result.assertions_minted == 1, "an aged-out fact must be free to return"


def test_a_rewritten_wording_is_not_re_minted_either(ac_root) -> None:
    """A rewrite rejects the old wording as surely as a retire does.

    The superseded Point is no longer `is_latest`, so re-observing the text the
    owner replaced would mint it again and stand the discarded version back up
    beside the correction.
    """
    from persome import config as config_mod
    from persome.writer import delta_apply

    original = "Alex works at Acme as a staff engineer."
    point_id = _seed_point(original)
    with fts.cursor() as conn:
        assert apply_model_edit(
            conn,
            kind="point",
            target_id=point_id,
            op="rewrite",
            replacement="I lead the platform team at Acme.",
        ).ok
        result = delta_apply.apply_delta(
            conn,
            config_mod.load(),
            {
                "entities": [{"canonical": "Alex", "kind": "person"}],
                "assertions": [{"subject": {"canonical": "Alex"}, "text": original}],
            },
        )
    assert result.assertions_minted == 0


def test_a_rejection_is_scoped_to_its_subject(ac_root) -> None:
    """The same sentence about someone else is a different claim."""
    from persome import config as config_mod
    from persome.writer import delta_apply

    text = "Prefers written proposals."
    point_id = _seed_point(text)
    with fts.cursor() as conn:
        assert apply_model_edit(conn, kind="point", target_id=point_id, op="retire").ok
        result = delta_apply.apply_delta(
            conn,
            config_mod.load(),
            {
                "entities": [{"canonical": "Sam", "kind": "person"}],
                "assertions": [{"subject": {"canonical": "Sam"}, "text": text}],
            },
        )
    assert result.assertions_minted == 1, "one rejection must not silence every subject"


def test_compaction_refuses_to_unstrike_a_rejected_entry(ac_root) -> None:
    """Keeping `#superseded-by:` while dropping the `~~` scores 100% on the
    token gate and still stands the rejected claim back up as live text."""
    from persome.writer.compact import _owner_authorship_lost

    before = "## [t] {id: b} #fact #superseded-by:a\n~~The old claim.~~\n"
    unstruck = before.replace("~~", "")
    assert "strike markers" in _owner_authorship_lost(before, unstruck)
    assert _owner_authorship_lost(before, before) == ""


def test_a_point_whose_markdown_is_gone_is_refused_not_a_500(ac_root) -> None:
    """`evo_nodes` can outlive the Markdown it projects — a partly restored
    backup leaves rows whose source of truth is missing. Under Markdown
    authority the supersede raises FileNotFoundError from deep in the store,
    which reached the owner as "HTTP 500" on the one surface whose job is
    telling them what happened to their model.
    """
    from persome.store import files as files_mod

    point_id = _seed_point()
    files_mod.memory_path("person-alex.md").unlink()

    with fts.cursor() as conn:
        rewrite = apply_model_edit(
            conn, kind="point", target_id=point_id, op="rewrite", replacement="Mine."
        )
        retire = apply_model_edit(conn, kind="point", target_id=point_id, op="retire")

    assert not rewrite.ok and rewrite.reason == "point_file_missing"
    assert not retire.ok and retire.reason == "point_file_missing"


def test_the_route_reports_a_storage_failure_instead_of_a_bare_500(ac_root, monkeypatch) -> None:
    from persome.api import routes as routes_mod

    def boom(*_args, **_kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(routes_mod, "apply_model_edit", boom, raising=False)
    monkeypatch.setattr("persome.model.edit.apply_model_edit", boom)
    client = TestClient(build_api_app(auth_enabled=False))
    response = client.post(
        "/model/edit",
        json={"schema_version": 1, "kind": "face", "id": "f", "op": "retire"},
    )
    assert response.status_code == 500
    assert response.json()["detail"] == "edit_failed: OSError"
