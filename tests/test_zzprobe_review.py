"""Throwaway probe for a code review. Delete after use."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from persome.evomem import backfill
from persome.model.snapshot import build_snapshot
from persome.store import entries as entries_mod
from persome.store import fts
from persome.store import schema_faces as sf

CONTENT = "Alex reserves mornings for focused writing."


def _seed_point() -> str:
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="person-alex.md", description="alex", tags=["t"])
        entries_mod.append_entry(conn, name="person-alex.md", content=CONTENT, tags=["topic:work"])
    assert backfill.run_backfill().ok
    with fts.cursor() as conn:
        row = conn.execute("SELECT node_id FROM evo_nodes LIMIT 1").fetchone()
    return str(row[0])


def test_probe_ended_entity_points_vanish(ac_root) -> None:
    node_id = _seed_point()
    key = sf.member_key(CONTENT)
    with fts.cursor() as conn:
        face_id = sf.record_face(
            conn, source=sf.PROVENANCE_MINED, signature="Alex writes early.", members=[key]
        )
        conn.execute("UPDATE schema_faces SET status='active' WHERE face_id=?", (face_id,))

    with fts.cursor() as conn:
        conn.row_factory = sqlite3.Row
        snap = build_snapshot(conn, redact=False)
    print("BEFORE  points:", [p["id"] for p in snap["points"]])
    print("BEFORE  face receipts:", snap["faces"][0]["member_receipts"])

    # Exactly what writer/delta_apply._stamp_entities_valid_until does when a
    # memory_delta reports the entity has ended. superseded_by stays '[]'.
    with fts.cursor() as conn:
        conn.execute(
            "UPDATE evo_nodes SET valid_until=? WHERE file_name=? AND is_latest=1"
            " AND valid_until IS NULL",
            (datetime.now(UTC).isoformat(), "person-alex.md"),
        )
        row = conn.execute(
            "SELECT status, valid_until, superseded_by FROM evo_nodes WHERE node_id=?", (node_id,)
        ).fetchone()
        print("NODE STATE:", tuple(row))

    with fts.cursor() as conn:
        conn.row_factory = sqlite3.Row
        snap = build_snapshot(conn, redact=False)
    print("AFTER   points:", [p["id"] for p in snap["points"]])
    print("AFTER   face receipts:", snap["faces"][0]["member_receipts"] if snap["faces"] else None)
    print("AFTER   stats:", snap["stats"])
    assert snap["points"], "an ended-entity Point disappeared from the live model"
