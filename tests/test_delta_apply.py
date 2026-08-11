"Tests for test delta apply."

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from persome.evomem.engine import EvoMemory
from persome.store import event_occurrences as occurrences_store
from persome.store import fts
from persome.store import relation_edges as edges_store
from persome.writer import delta_apply


def _apply(clean: dict, **context) -> delta_apply.ApplyResult:
    with fts.cursor() as conn:
        return delta_apply.apply_delta(conn, None, clean, memory=EvoMemory(), **context)


def _apply_cfg(clean: dict, **flags) -> delta_apply.ApplyResult:
    """apply with a real memory_delta cfg (e.g. apply_assertions=True)."""
    cfg = SimpleNamespace(memory_delta=SimpleNamespace(**flags))
    with fts.cursor() as conn:
        return delta_apply.apply_delta(conn, cfg, clean, memory=EvoMemory())


def _point_files(prefix: str) -> list[str]:
    with fts.cursor() as conn:
        conn.row_factory = None
        return [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT file_name FROM evo_nodes WHERE file_name LIKE ?", (f"{prefix}-%",)
            )
        ]


def test_entities_mint_kind_aware_points(ac_root):
    clean = {
        "entities": [
            {
                "ref": "\u5f20\u4f1f",
                "kind": "person",
                "ended": False,
                "quote": "\u548c\u5f20\u4f1f\u5bf9\u9f50",
            },
            {
                "new_entity": "Acme",
                "kind": "project",
                "ended": False,
                "quote": "Acme \u4e3b\u9879\u76ee",
            },
            {
                "ref": "\u7814\u53d1\u7fa4",
                "kind": "org",
                "ended": False,
                "quote": "\u7814\u53d1\u7fa4\u91cc",
            },
            {"ref": "excalidraw", "kind": "artifact", "ended": False, "quote": "\u7528 excalidraw"},
        ],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    r = _apply(clean)
    assert r.entities_minted == 4

    assert _point_files("person") == ["person-\u5f20\u4f1f.md"]
    assert _point_files("project") == ["project-acme.md"]
    assert _point_files("org") == ["org-\u7814\u53d1\u7fa4.md"]
    assert _point_files("tool") == ["tool-excalidraw.md"]


def test_self_and_bad_kind_skipped(ac_root):
    clean = {
        "entities": [
            {"ref": "self", "kind": "person", "ended": False, "quote": "x"},
            {"ref": "x", "kind": "bogus", "ended": False, "quote": "x"},
            {"kind": "person", "ended": False, "quote": "x"},
        ],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    r = _apply(clean)
    assert r.entities_minted == 0


def test_ended_entity_stamps_valid_until(ac_root):
    clean = {
        "entities": [
            {
                "ref": "\u7814\u53d1\u7fa4",
                "kind": "org",
                "ended": True,
                "quote": "\u9000\u51fa\u7814\u53d1\u7fa4",
            }
        ],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    _apply(clean)
    with fts.cursor() as conn:
        conn.row_factory = None
        rows = conn.execute(
            "SELECT valid_until FROM evo_nodes WHERE file_name = 'org-\u7814\u53d1\u7fa4.md'"
        ).fetchall()
    assert any(row[0] is not None for row in rows)


def test_idempotent_rerun_sees_not_remints(ac_root):
    clean = {
        "entities": [{"ref": "\u5f20\u4f1f", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    r1 = _apply(clean)
    r2 = _apply(clean)
    assert r1.entities_minted == 1 and r1.entities_seen == 0
    assert r2.entities_minted == 0 and r2.entities_seen == 1


def test_relations_mint_edges_with_polarity(ac_root):
    clean = {
        "entities": [{"ref": "\u5f20\u4f1f", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [
            {
                "src": {"ref": "self"},
                "dst": {"ref": "\u5f20\u4f1f"},
                "predicate": "knows",
                "polarity": "+",
                "ended": False,
                "quote": "\u548c\u5f20\u4f1f\u6109\u5feb\u5408\u4f5c",
                "confidence": 0.9,
            }
        ],
        "events": [],
        "assertions": [],
    }
    r = _apply(clean)
    assert r.edges_new == 1
    with fts.cursor() as conn:
        conn.row_factory = None
        row = conn.execute(
            "SELECT src_identity, dst_identity, predicate, polarity FROM relation_edges"
            " WHERE predicate='knows'"
        ).fetchone()
    assert row == ("self", "\u5f20\u4f1f", "knows", "+")

    with fts.cursor() as conn:
        conn.row_factory = None
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM relation_edges WHERE predicate='engaged_with'"
                " AND dst_identity='\u5f20\u4f1f'"
            ).fetchone()[0]
            == 1
        )


def test_illegal_endpoint_relation_dropped(ac_root):

    clean = {
        "entities": [
            {"ref": "\u5f20\u4f1f", "kind": "person", "ended": False, "quote": "x"},
            {"ref": "\u674e\u56db", "kind": "person", "ended": False, "quote": "x"},
        ],
        "relations": [
            {
                "src": {"ref": "\u5f20\u4f1f"},
                "dst": {"ref": "\u674e\u56db"},
                "predicate": "participates_in",
                "polarity": "0",
                "ended": False,
                "quote": "x",
                "confidence": 0.9,
            }
        ],
        "events": [],
        "assertions": [],
    }
    r = _apply(clean)
    assert r.edges_new == 0
    with fts.cursor() as conn:
        conn.row_factory = None

        assert (
            conn.execute(
                "SELECT COUNT(*) FROM relation_edges WHERE predicate='participates_in'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM relation_edges WHERE predicate='engaged_with'"
            ).fetchone()[0]
            == 2
        )


def test_ended_relation_closes_edge(ac_root):
    clean = {
        "entities": [{"ref": "\u5f20\u4f1f", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [
            {
                "src": {"ref": "self"},
                "dst": {"ref": "\u5f20\u4f1f"},
                "predicate": "reports_to",
                "polarity": "0",
                "ended": True,
                "quote": "\u4e0d\u518d\u5411\u5f20\u4f1f\u6c47\u62a5",
                "confidence": 0.9,
            }
        ],
        "events": [],
        "assertions": [],
    }
    r = _apply(clean)
    assert r.edges_closed == 1
    with fts.cursor() as conn:
        conn.row_factory = None
        vt = conn.execute(
            "SELECT valid_to FROM relation_edges WHERE predicate='reports_to'"
        ).fetchone()[0]
    assert vt is not None


def test_itemized_ended_relation_replay_does_not_duplicate_closed_line(ac_root):
    clean = {
        "entities": [{"ref": "Alice", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [
            {
                "src": {"ref": "self"},
                "dst": {"ref": "Alice"},
                "predicate": "reports_to",
                "polarity": "0",
                "ended": True,
                "quote": "no longer reports to Alice",
                "confidence": 0.9,
            }
        ],
        "events": [],
        "assertions": [],
    }
    with fts.cursor() as conn:
        first = delta_apply.ApplyResult()
        delta_apply._apply_relations(
            conn,
            clean,
            {"Alice": "person"},
            first,
            effect_key="memory-delta:1:relation:end-alice",
        )
        replay = delta_apply.ApplyResult()
        delta_apply._apply_relations(
            conn,
            clean,
            {"Alice": "person"},
            replay,
            effect_key="memory-delta:1:relation:end-alice",
        )
        rows = conn.execute(
            "SELECT edge_id, valid_to FROM relation_edges WHERE predicate='reports_to'"
        ).fetchall()

    assert (first.edges_new, first.edges_closed) == (1, 1)
    assert (replay.edges_new, replay.edges_reinforced, replay.edges_closed) == (0, 0, 0)
    assert len(rows) == 1 and rows[0][1] is not None


def test_events_mint_activity_point_and_edge(ac_root):
    clean = {
        "entities": [],
        "relations": [],
        "events": [
            {
                "title": "\u5b8c\u6210\u5b63\u5ea6\u5bf9\u8d26",
                "participants": [{"ref": "self"}],
                "quote": "\u5b8c\u6210\u4e86\u5b63\u5ea6\u5bf9\u8d26",
                "confidence": 0.9,
            }
        ],
        "assertions": [],
    }
    start = datetime(2026, 8, 11, 2, 0, tzinfo=UTC)
    r = _apply(
        clean,
        delta_id=7,
        session_id="event-session",
        window_start=start,
        window_end=start + timedelta(minutes=5),
    )
    assert r.events_minted == 1
    with fts.cursor() as conn:
        conn.row_factory = None
        row = conn.execute(
            "SELECT src_identity, dst_identity, predicate, source_kind, source_id, "
            "source_receipt FROM relation_edges"
        ).fetchone()
        occurrence = conn.execute("SELECT * FROM event_occurrences").fetchone()
    assert row is not None and row[0] == "self" and row[1].startswith("event:occurrence:")
    assert row[2] == "participates_in"
    assert row[3] == "occurrence"
    assert row[4] == row[1].removeprefix("event:occurrence:")
    assert occurrences_store.parse_receipt(row[5]) == row[4]
    assert occurrence is not None


def test_event_retry_is_one_occurrence_but_later_window_is_same_series(ac_root):
    clean = {
        "entities": [],
        "relations": [],
        "events": [
            {
                "title": "Weekly review",
                "participants": [{"ref": "self"}],
                "quote": "Reviewed the launch plan.",
                "confidence": 0.9,
            }
        ],
        "assertions": [],
    }
    start = datetime(2026, 8, 11, 2, 0, tzinfo=UTC)
    context = {
        "session_id": "event-session",
        "window_start": start,
        "window_end": start + timedelta(minutes=5),
    }
    first = _apply(clean, delta_id=1, **context)
    retry = _apply(clean, delta_id=1, **context)
    later = _apply(
        clean,
        delta_id=2,
        session_id="event-session",
        window_start=start + timedelta(minutes=5),
        window_end=start + timedelta(minutes=10),
    )

    with fts.cursor() as conn:
        conn.row_factory = None
        rows = conn.execute(
            "SELECT occurrence_id, series_id FROM event_occurrences ORDER BY window_start"
        ).fetchall()

    assert first.events_minted == 1
    assert retry.events_minted == 0
    assert later.events_minted == 1
    assert len(rows) == 2
    assert rows[0][0] != rows[1][0]
    assert rows[0][1] == rows[1][1]


def test_event_participant_change_splits_series(ac_root):
    start = datetime(2026, 8, 11, 2, 0, tzinfo=UTC)

    def clean(participant: str) -> dict:
        return {
            "entities": [],
            "relations": [],
            "events": [
                {
                    "title": "Weekly review",
                    "participants": [{"ref": participant}],
                    "quote": "Reviewed the launch plan.",
                    "confidence": 0.9,
                }
            ],
            "assertions": [],
        }

    for offset, participant in enumerate(("Alice", "Bob")):
        _apply(
            clean(participant),
            session_id="event-session",
            window_start=start + timedelta(minutes=offset * 5),
            window_end=start + timedelta(minutes=(offset + 1) * 5),
        )

    with fts.cursor() as conn:
        conn.row_factory = None
        series = [row[0] for row in conn.execute("SELECT series_id FROM event_occurrences")]
    assert len(set(series)) == 2


def test_legacy_event_endpoint_remains_title_hash_without_fake_occurrence(ac_root):
    clean = {
        "entities": [],
        "relations": [],
        "events": [
            {
                "title": "Historical review",
                "participants": [{"ref": "self"}],
                "quote": "Reviewed the launch plan.",
                "confidence": 0.9,
            }
        ],
        "assertions": [],
    }
    _apply(clean)
    expected = "event:" + hashlib.sha1(b"Historical review").hexdigest()[:12]

    with fts.cursor() as conn:
        conn.row_factory = None
        endpoint = conn.execute(
            "SELECT dst_identity FROM relation_edges WHERE predicate='participates_in'"
        ).fetchone()[0]
        occurrence_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_occurrences'"
        ).fetchone()
        occurrence_count = (
            conn.execute("SELECT COUNT(*) FROM event_occurrences").fetchone()[0]
            if occurrence_table is not None
            else 0
        )

    assert endpoint == expected
    assert occurrence_count == 0


def test_empty_and_malformed_fail_open(ac_root):
    assert _apply({}).skipped_reason == "empty"

    r = _apply(
        {"entities": [None, {"kind": "person"}, "garbage"], "relations": [42], "events": [None]}
    )
    assert isinstance(r, delta_apply.ApplyResult)


def test_floor_edge_connects_every_entity_no_orphan(ac_root):
    clean = {
        "entities": [
            {
                "new_entity": "\u817e\u8baf\u6821\u62db",
                "kind": "org",
                "ended": False,
                "quote": "\u770b\u6821\u62db\u9875",
            },
            {
                "new_entity": "\u67d0\u5de5\u5177",
                "kind": "artifact",
                "ended": False,
                "quote": "\u7528\u4e86\u67d0\u5de5\u5177",
            },
        ],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    r = _apply(clean)
    assert r.floor_edges == 2
    with fts.cursor() as conn:
        conn.row_factory = None
        connected = {
            (row[0], row[1])
            for row in conn.execute(
                "SELECT dst_identity, status FROM relation_edges WHERE src_identity='self'"
                " AND predicate='engaged_with'"
            )
        }
    assert connected == {
        ("\u817e\u8baf\u6821\u62db", "active"),
        ("\u67d0\u5de5\u5177", "active"),
    }  # observed Lines


def test_floor_obs_accumulates_across_sessions(ac_root):
    clean = {
        "entities": [
            {"ref": "\u5f20\u4e09", "kind": "person", "ended": False, "quote": "\u548c\u5f20\u4e09"}
        ],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    for _ in range(3):
        _apply(clean)
    with fts.cursor() as conn:
        conn.row_factory = None
        row = conn.execute(
            "SELECT observations FROM relation_edges WHERE predicate='engaged_with'"
            " AND dst_identity='\u5f20\u4e09' AND valid_to IS NULL"
        ).fetchone()
    assert row is not None and row[0] == 3


def test_cooccurrence_relation_accumulates_then_promotes(ac_root):
    clean = {
        "entities": [
            {
                "ref": "\u5f20\u4e09",
                "kind": "person",
                "ended": False,
                "quote": "\u5f20\u4e09\u548c\u674e\u56db",
            },
            {
                "ref": "\u674e\u56db",
                "kind": "person",
                "ended": False,
                "quote": "\u5f20\u4e09\u548c\u674e\u56db",
            },
        ],
        "relations": [
            {
                "src": {"ref": "\u5f20\u4e09"},
                "dst": {"ref": "\u674e\u56db"},
                "predicate": "knows",
                "quote": "",
                "confidence": 0.6,
                "cooccurrence": True,
            }
        ],
        "events": [],
        "assertions": [],
    }
    for _ in range(3):
        _apply(clean)
    with fts.cursor() as conn:
        row = conn.execute(
            "SELECT observations, status FROM relation_edges WHERE predicate='knows'"
        ).fetchone()
        promoted = edges_store.promote_edges(conn)
        status = conn.execute(
            "SELECT status FROM relation_edges WHERE predicate='knows'"
        ).fetchone()[0]
    assert tuple(row) == (3, "shadow")
    assert promoted == 1 and status == "active"


def test_org_nesting_part_of(ac_root):
    clean = {
        "entities": [
            {"new_entity": "\u817e\u8baf", "kind": "org", "ended": False, "quote": "x"},
            {"new_entity": "\u6df7\u5143\u90e8", "kind": "org", "ended": False, "quote": "x"},
        ],
        "relations": [
            {
                "src": {"ref": "\u6df7\u5143\u90e8"},
                "dst": {"ref": "\u817e\u8baf"},
                "predicate": "part_of",
                "polarity": "0",
                "ended": False,
                "quote": "\u6df7\u5143\u90e8\u5c5e\u4e8e\u817e\u8baf",
                "confidence": 0.9,
            }
        ],
        "events": [],
        "assertions": [],
    }
    r = _apply(clean)
    assert r.edges_new == 1
    with fts.cursor() as conn:
        conn.row_factory = None
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM relation_edges WHERE predicate='part_of'"
                " AND src_identity='\u6df7\u5143\u90e8' AND dst_identity='\u817e\u8baf'"
            ).fetchone()[0]
            == 1
        )


def test_classifier_retired_when_apply_enabled(ac_root):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from persome.writer import classifier as classifier_mod

    cfg = SimpleNamespace(
        reducer=SimpleNamespace(enabled=True),
        memory_delta=SimpleNamespace(apply_enabled=True),
    )
    res = classifier_mod.classify_after_reduce(
        cfg,
        session_id="s1",
        event_daily_path="event-2026-07-04.md",
        session_start=datetime(2026, 7, 4, tzinfo=UTC),
        session_end=datetime(2026, 7, 4, 1, tzinfo=UTC),
    )
    assert res.skipped_reason == "classifier retired (delta apply)"
    assert not res.committed


def test_assertions_land_as_fact_entries(ac_root):
    clean = {
        "entities": [{"ref": "\u6e29\u5b50\u58a8", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [],
        "events": [],
        "assertions": [
            {
                "subject": {"ref": "\u6e29\u5b50\u58a8"},
                "text": "\u6e29\u5b50\u58a8\u62ff\u4e86\u817e\u8baf offer",
                "quote": "q",
                "confidence": 0.95,
            },
            {
                "subject": {"ref": "\u6e29\u5b50\u58a8"},
                "text": "\u6e29\u5b50\u58a8\u6539\u4e86 inspector.py",
                "quote": "q",
                "confidence": 0.9,
            },
        ],
    }
    r = _apply_cfg(clean, apply_assertions=True)
    assert r.assertions_minted == 2
    with fts.cursor() as conn:
        conn.row_factory = None
        facts = {
            row[0]
            for row in conn.execute(
                "SELECT content FROM evo_nodes WHERE file_name='person-\u6e29\u5b50\u58a8.md'"
                " AND is_latest=1 AND status='active' AND tags LIKE 'fact%'"
            )
        }
    assert (
        "\u6e29\u5b50\u58a8\u62ff\u4e86\u817e\u8baf offer" in facts
        and "\u6e29\u5b50\u58a8\u6539\u4e86 inspector.py" in facts
    )


def test_assertions_gated_off_by_default(ac_root):
    clean = {
        "entities": [{"ref": "\u738b\u4e94", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [],
        "events": [],
        "assertions": [
            {
                "subject": {"ref": "\u738b\u4e94"},
                "text": "\u738b\u4e94\u8d1f\u8d23X",
                "quote": "q",
                "confidence": 0.9,
            }
        ],
    }
    r = _apply(clean)
    assert r.assertions_minted == 0


def test_assertions_unroutable_subject_skipped(ac_root):
    clean = {
        "entities": [],
        "relations": [],
        "events": [],
        "assertions": [
            {
                "subject": {"new_entity": "\u964c\u751f\u4eba"},
                "text": "\u964c\u751f\u4eba\u505a\u4e86X",
                "quote": "q",
                "confidence": 0.9,
            }
        ],
    }
    r = _apply_cfg(clean, apply_assertions=True)
    assert r.assertions_minted == 0


def test_assertions_idempotent_across_sessions(ac_root):
    clean = {
        "entities": [{"ref": "\u8d75\u516d", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [],
        "events": [],
        "assertions": [
            {
                "subject": {"ref": "\u8d75\u516d"},
                "text": "\u8d75\u516d\u662f\u67b6\u6784\u5e08",
                "quote": "q",
                "confidence": 0.9,
            }
        ],
    }
    _apply_cfg(clean, apply_assertions=True)
    r2 = _apply_cfg(clean, apply_assertions=True)
    assert r2.assertions_minted == 0 and r2.assertions_seen == 1


def test_reproject_entries_from_evomem_feeds_retrieval(ac_root):
    from persome.session.tick import _reproject_entries_from_evomem

    clean = {
        "entities": [{"ref": "\u5f20\u4e09", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    _apply(clean)
    with fts.cursor() as conn:
        conn.row_factory = None
        before = conn.execute(
            "SELECT count(*) FROM entries WHERE path='person-\u5f20\u4e09.md' AND superseded=0"
        ).fetchone()[0]
    _reproject_entries_from_evomem()
    with fts.cursor() as conn:
        conn.row_factory = None
        after = conn.execute(
            "SELECT count(*) FROM entries WHERE path='person-\u5f20\u4e09.md' AND superseded=0"
        ).fetchone()[0]
    assert before == 0 and after >= 1
