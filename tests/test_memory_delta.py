"""Session memory-delta extraction, gates, persistence, and apply.

Covers the deterministic gates (quote evidence / roster multiple-choice /
closed predicate set / confidence floor), persistence, and safe degradation.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone

import pytest

from persome import config as config_mod
from persome.session import store as session_store
from persome.store import fts
from persome.store import memory_delta_items as items_store
from persome.store import memory_deltas as deltas_store
from persome.store import model_candidates as candidates_store
from persome.timeline import store as timeline_store
from persome.timeline.store import TimelineBlock
from persome.writer import memory_delta as delta_mod


def _block(
    start: datetime,
    entries: list[str],
    apps: list[str],
    *,
    normalization_status: str = "legacy",
) -> TimelineBlock:
    return TimelineBlock(
        start_time=start,
        end_time=start + timedelta(minutes=1),
        entries=entries,
        apps_used=apps,
        capture_count=len(entries),
        normalization_status=normalization_status,
    )


def _seed_session_blocks(entries: list[str]) -> tuple[datetime, datetime]:
    start = datetime(2026, 7, 2, 9, 0).astimezone()
    with fts.cursor() as conn:
        timeline_store.ensure_schema(conn)
        for i, entry in enumerate(entries):
            timeline_store.insert(conn, _block(start + timedelta(minutes=i), [entry], ["Feishu"]))
    return start, start + timedelta(minutes=len(entries) + 1)


def _seed_session(session_id: str, start: datetime, end: datetime) -> None:
    with fts.cursor() as conn:
        session_store.ensure_schema(conn)
        session_store.insert(
            conn,
            session_store.SessionRow(
                id=session_id,
                start_time=start,
                end_time=end,
                status="reduced",
            ),
        )


def _cfg(enabled: bool = True) -> config_mod.Config:
    cfg = config_mod.Config()
    cfg.memory_delta.enabled = enabled
    return cfg


def _ref(name: str) -> dict:
    return {"new_entity": name}


def _payload(**overrides) -> str:
    base = {
        "owner_alias_candidates": [],
        "entities": [
            {
                "new_entity": "\u5f20\u4e09",
                "kind": "person",
                "quote": "\u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba",
                "confidence": 0.9,
            }
        ],
        "assertions": [
            {
                "subject": _ref("\u5f20\u4e09"),
                "text": "\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba",
                "quote": "\u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba",
                "confidence": 0.8,
            }
        ],
        "relations": [],
        "events": [],
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


SESSION_ENTRY = '[Feishu] \u804a\u5929: \u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba\u3002"\u5468\u4e94\u7248\u672c\u53ef\u4ee5\u53d1"'


def test_flag_off_is_a_strict_noop(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    result = delta_mod.run_after_session(
        _cfg(enabled=False), session_id="s1", start_time=start, end_time=end
    )
    assert result.skipped_reason == "disabled" and not result.written
    assert fake_llm.calls == []  # no LLM call, no row
    with fts.cursor() as conn:
        assert deltas_store.recent(conn) == []


def test_direct_oversized_window_fails_closed_instead_of_truncating(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY, SESSION_ENTRY, SESSION_ENTRY])
    cfg = _cfg()
    cfg.memory_delta.max_blocks = 2

    result = delta_mod.run_after_session(
        cfg,
        session_id="s-too-large",
        start_time=start,
        end_time=end,
    )

    assert result.skipped_reason == "window_too_large"
    assert result.written is False
    assert fake_llm.calls == []


def test_delta_persisted_shadow_with_counts(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(delta_mod.STAGE, _payload())
    result = delta_mod.run_after_session(_cfg(), session_id="s2", start_time=start, end_time=end)
    assert result.written and result.counts["entities"] == 1 and result.counts["assertions"] == 1
    with fts.cursor() as conn:
        row = deltas_store.latest_for_session(conn, "s2")
    assert row is not None and row["status"] == "shadow"
    delta = json.loads(row["payload"])
    assert delta["entities"][0]["new_entity"] == "\u5f20\u4e09"
    # the LLM actually received roster + session_events sections (now cache-control blocks)
    blocks = fake_llm.calls[0]["messages"][1]["content"]
    sent = "".join(b["text"] for b in blocks)
    assert "<roster>" in sent and "<session_events>" in sent and "\u5f20\u4e09" in sent

    sys_blocks = fake_llm.calls[0]["messages"][0]["content"]
    assert sys_blocks[0].get("cache_control") == {"type": "ephemeral"}
    assert blocks[0].get("cache_control") == {"type": "ephemeral"}


def test_windowed_apply_mints_occurrence_with_explicit_delta_context(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            entities=[],
            assertions=[],
            events=[
                {
                    "title": "Friday release review",
                    "participants": [{"ref": "self"}],
                    "quote": "\u5468\u4e94\u7248\u672c\u53ef\u4ee5\u53d1",
                    "confidence": 0.9,
                }
            ],
        ),
    )

    result = delta_mod.run_after_session(
        _cfg(),
        session_id="s-event-context",
        start_time=start,
        end_time=end,
    )

    assert result.written and result.applied
    with fts.cursor() as conn:
        occurrence = conn.execute(
            "SELECT delta_id, session_id, window_start, window_end FROM event_occurrences"
        ).fetchone()
        edge = conn.execute(
            "SELECT dst_identity, source_kind FROM relation_edges WHERE predicate='participates_in'"
        ).fetchone()
    assert occurrence is not None
    assert occurrence["delta_id"] == result.delta_id
    assert occurrence["session_id"] == "s-event-context"
    assert edge is not None and edge["dst_identity"].startswith("event:occurrence:")
    assert edge["source_kind"] == "occurrence"


def test_pending_parent_recovers_geometry_from_fully_applied_item_ledger(
    ac_root, fake_llm, monkeypatch
) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            entities=[],
            assertions=[],
            events=[
                {
                    "title": "Friday release review",
                    "participants": [{"ref": "self"}],
                    "quote": "\u5468\u4e94\u7248\u672c\u53ef\u4ee5\u53d1",
                    "confidence": 0.9,
                }
            ],
        ),
    )

    real_set_apply_status = deltas_store.set_apply_status

    def die_before_parent_publish(*_args, **_kwargs):
        raise SystemExit("synthetic process death after item acknowledgement")

    monkeypatch.setattr(deltas_store, "set_apply_status", die_before_parent_publish)
    with pytest.raises(SystemExit, match="after item acknowledgement"):
        delta_mod.run_after_session(
            _cfg(),
            session_id="s-parent-pending-crash",
            start_time=start,
            end_time=end,
        )
    monkeypatch.setattr(deltas_store, "set_apply_status", real_set_apply_status)

    with fts.cursor() as conn:
        parent = deltas_store.latest_for_session(conn, "s-parent-pending-crash")
        assert parent is not None
        delta_id = int(parent["id"])
        item = conn.execute(
            "SELECT state, geometry_changed FROM memory_delta_items WHERE delta_id=?",
            (delta_id,),
        ).fetchone()
        occurrence_count = conn.execute("SELECT COUNT(*) FROM event_occurrences").fetchone()[0]
    assert parent["apply_status"] == "pending"
    assert tuple(item) == (items_store.STATE_APPLIED, 1)

    retry = delta_mod.ensure_active_window(
        _cfg(),
        session_id="s-parent-pending-crash",
        start_time=start,
        end_time=end,
    )

    assert retry.applied is True
    assert retry.skipped_reason == "resumed_apply"
    assert retry.geometry_changed is True
    with fts.cursor() as conn:
        parent_after = deltas_store.latest_for_session(conn, "s-parent-pending-crash")
        occurrence_count_after = conn.execute("SELECT COUNT(*) FROM event_occurrences").fetchone()[
            0
        ]
    assert parent_after is not None and parent_after["apply_status"] == "applied"
    assert occurrence_count_after == occurrence_count == 1


def test_lease_retry_keeps_geometry_unknown_after_effect_commits_before_ack(
    ac_root, fake_llm, monkeypatch
) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            entities=[],
            assertions=[],
            events=[
                {
                    "title": "Friday release review",
                    "participants": [{"ref": "self"}],
                    "quote": "\u5468\u4e94\u7248\u672c\u53ef\u4ee5\u53d1",
                    "confidence": 0.9,
                }
            ],
        ),
    )

    real_mark_applied = items_store.mark_applied

    def die_before_item_ack(*_args, **_kwargs):
        raise SystemExit("synthetic process death before item acknowledgement")

    monkeypatch.setattr(items_store, "mark_applied", die_before_item_ack)
    with pytest.raises(SystemExit, match="before item acknowledgement"):
        delta_mod.run_after_session(
            _cfg(),
            session_id="s-effect-before-ack",
            start_time=start,
            end_time=end,
        )
    monkeypatch.setattr(items_store, "mark_applied", real_mark_applied)

    with fts.cursor() as conn:
        parent = deltas_store.latest_for_session(conn, "s-effect-before-ack")
        assert parent is not None
        delta_id = int(parent["id"])
        before = conn.execute(
            "SELECT state, attempts, geometry_changed FROM memory_delta_items WHERE delta_id=?",
            (delta_id,),
        ).fetchone()
        occurrence_count = conn.execute("SELECT COUNT(*) FROM event_occurrences").fetchone()[0]
        conn.execute(
            "UPDATE memory_delta_items SET lease_until='2000-01-01T00:00:00+00:00'"
            " WHERE delta_id=?",
            (delta_id,),
        )
    assert parent["apply_status"] == "pending"
    assert tuple(before) == (items_store.STATE_APPLYING, 1, None)

    retry = delta_mod.ensure_active_window(
        _cfg(),
        session_id="s-effect-before-ack",
        start_time=start,
        end_time=end,
    )

    assert retry.applied is True
    assert retry.skipped_reason == "resumed_apply"
    assert retry.geometry_changed is True
    with fts.cursor() as conn:
        after = conn.execute(
            "SELECT state, attempts, geometry_changed FROM memory_delta_items WHERE delta_id=?",
            (delta_id,),
        ).fetchone()
        parent_after = deltas_store.latest_for_session(conn, "s-effect-before-ack")
        occurrence_count_after = conn.execute("SELECT COUNT(*) FROM event_occurrences").fetchone()[
            0
        ]
    assert tuple(after) == (items_store.STATE_APPLIED, 2, None)
    assert parent_after is not None and parent_after["apply_status"] == "applied"
    assert occurrence_count_after == occurrence_count == 1


def test_apply_item_errors_leave_parent_delta_retryable(ac_root, fake_llm, monkeypatch) -> None:
    from persome.writer import delta_apply

    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(delta_mod.STAGE, _payload())

    def apply_with_error(*_args, **_kwargs):
        return delta_apply.ApplyResult(errors=["entity: synthetic write failure"])

    monkeypatch.setattr(delta_apply, "apply_delta_item", apply_with_error)
    result = delta_mod.run_after_session(
        _cfg(),
        session_id="s-apply-item-error",
        start_time=start,
        end_time=end,
    )

    assert result.written is True
    assert result.applied is False
    assert result.skipped_reason == "apply_failed"
    with fts.cursor() as conn:
        row = deltas_store.latest_for_session(conn, "s-apply-item-error")
    assert row is not None and row["apply_status"] == "failed"


def test_retry_after_effect_commit_does_not_repeat_entity_floor_line(
    ac_root, fake_llm, monkeypatch
) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    prior_start = start - timedelta(hours=1)
    prior_end = prior_start + timedelta(minutes=5)
    _seed_session("s-effect-prior", prior_start, prior_end)
    _seed_session("s-effect-crash", start, end)
    with fts.cursor() as conn:
        prior = candidates_store.record_evidence(
            conn,
            candidate_kind=candidates_store.KIND_PERSON,
            subject="\u5f20\u4e09",
            text="\u5f20\u4e09",
            session_id="s-effect-prior",
            window_start=prior_start,
            window_end=prior_end,
            quote="\u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba",
            confidence=0.9,
        )
    assert prior is not None
    assert prior.state.status == candidates_store.STATUS_PENDING
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(assertions=[], relations=[], events=[]),
    )

    real_mark_applied = items_store.mark_applied
    lost_once = True

    def lose_first_ledger_ack(*args, **kwargs):
        nonlocal lost_once
        if lost_once:
            lost_once = False
            return False
        return real_mark_applied(*args, **kwargs)

    monkeypatch.setattr(items_store, "mark_applied", lose_first_ledger_ack)
    first = delta_mod.run_after_session(
        _cfg(),
        session_id="s-effect-crash",
        start_time=start,
        end_time=end,
    )

    assert first.written is True
    assert first.applied is False
    assert first.skipped_reason == "apply_failed"
    assert first.delta_id is not None
    with fts.cursor() as conn:
        edge_before = conn.execute(
            "SELECT edge_id, observations FROM relation_edges "
            "WHERE predicate='engaged_with' AND valid_to IS NULL"
        ).fetchone()
        assert edge_before is not None and edge_before["observations"] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM relation_edge_effects WHERE edge_id=?",
                (edge_before["edge_id"],),
            ).fetchone()[0]
            == 1
        )
        conn.execute(
            "UPDATE memory_delta_items SET lease_until='2000-01-01T00:00:00+00:00' "
            "WHERE delta_id=? AND state='applying'",
            (first.delta_id,),
        )

    monkeypatch.setattr(items_store, "mark_applied", real_mark_applied)
    retry = delta_mod.ensure_active_window(
        _cfg(),
        session_id="s-effect-crash",
        start_time=start,
        end_time=end,
    )

    assert retry.applied is True
    assert retry.skipped_reason == "resumed_apply"
    assert retry.geometry_changed is True
    with fts.cursor() as conn:
        edge_after = conn.execute(
            "SELECT edge_id, observations FROM relation_edges "
            "WHERE predicate='engaged_with' AND valid_to IS NULL"
        ).fetchone()
        item_state = conn.execute(
            "SELECT state, attempts FROM memory_delta_items WHERE delta_id=?",
            (first.delta_id,),
        ).fetchone()
        point_count = conn.execute(
            "SELECT COUNT(*) FROM evo_nodes WHERE file_name='person-\u5f20\u4e09.md' "
            "AND is_latest=1 AND status='active'"
        ).fetchone()[0]
    assert edge_after is not None
    assert tuple(edge_after) == tuple(edge_before)
    assert tuple(item_state) == (items_store.STATE_APPLIED, 2)
    assert point_count == 1


def test_windowed_points_require_two_independent_sessions_before_promotion(
    ac_root, fake_llm
) -> None:
    first_start = datetime(2026, 7, 2, 9, 0).astimezone()
    second_start = first_start + timedelta(hours=1)
    first_end = first_start + timedelta(minutes=2)
    second_end = second_start + timedelta(minutes=2)
    with fts.cursor() as conn:
        timeline_store.ensure_schema(conn)
        timeline_store.insert(
            conn,
            _block(first_start, [SESSION_ENTRY], ["Feishu"], normalization_status="llm"),
        )
        timeline_store.insert(
            conn,
            _block(second_start, [SESSION_ENTRY], ["Feishu"], normalization_status="llm"),
        )
    _seed_session("s-candidate-1", first_start, first_end)
    _seed_session("s-candidate-2", second_start, second_end)
    fake_llm.set_default(delta_mod.STAGE, _payload())

    first = delta_mod.run_after_session(
        _cfg(),
        session_id="s-candidate-1",
        start_time=first_start,
        end_time=first_end,
    )
    duplicate = delta_mod.ensure_active_window(
        _cfg(),
        session_id="s-candidate-1",
        start_time=first_start,
        end_time=first_end,
    )

    assert first.written and first.applied
    assert duplicate.skipped_reason == "already_processed"
    with fts.cursor() as conn:
        pending = conn.execute(
            "SELECT candidate_kind, status, evidence_count, independent_sessions "
            "FROM model_candidates ORDER BY candidate_kind"
        ).fetchall()
        assert conn.execute("SELECT COUNT(*) FROM evo_nodes").fetchone()[0] == 0
        relation_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='relation_edges'"
        ).fetchone()
        assert relation_table is None
    assert [tuple(row) for row in pending] == [
        ("assertion", "pending", 1, 1),
        ("person", "pending", 1, 1),
    ]

    second = delta_mod.run_after_session(
        _cfg(),
        session_id="s-candidate-2",
        start_time=second_start,
        end_time=second_end,
    )

    assert second.written and second.applied
    assert len(fake_llm.calls) == 2
    with fts.cursor() as conn:
        promoted = conn.execute(
            "SELECT candidate_kind, status, evidence_count, independent_sessions "
            "FROM model_candidates ORDER BY candidate_kind"
        ).fetchall()
        entity_points = conn.execute(
            "SELECT COUNT(*) FROM evo_nodes WHERE file_name='person-\u5f20\u4e09.md' "
            "AND is_latest=1 AND status='active' AND instr(tags, 'entity') > 0"
        ).fetchone()[0]
        assertion_points = conn.execute(
            "SELECT COUNT(*) FROM evo_nodes WHERE file_name='person-\u5f20\u4e09.md' "
            "AND is_latest=1 AND status='active' AND instr(tags, 'fact') > 0"
        ).fetchone()[0]
        floor = conn.execute(
            "SELECT observations FROM relation_edges WHERE predicate='engaged_with' "
            "AND valid_to IS NULL"
        ).fetchone()
    assert [tuple(row) for row in promoted] == [
        ("assertion", "promoted", 2, 2),
        ("person", "promoted", 2, 2),
    ]
    assert entity_points == 1
    assert assertion_points == 1
    assert floor is not None and floor["observations"] == 1


def test_promoted_assertion_materializes_when_subject_point_arrives_later(
    ac_root, fake_llm
) -> None:
    base = datetime(2026, 7, 2, 9, 0).astimezone()
    windows: list[tuple[str, datetime, datetime]] = []
    with fts.cursor() as conn:
        timeline_store.ensure_schema(conn)
        for index in range(4):
            start = base + timedelta(hours=index)
            end = start + timedelta(minutes=2)
            session_id = f"s-late-subject-{index + 1}"
            timeline_store.insert(
                conn,
                _block(start, [SESSION_ENTRY], ["Feishu"], normalization_status="llm"),
            )
            windows.append((session_id, start, end))
    for session_id, start, end in windows:
        _seed_session(session_id, start, end)

    assertion_only = _payload(entities=[], relations=[], events=[])
    fake_llm.set_default(delta_mod.STAGE, assertion_only)
    for session_id, start, end in windows[:2]:
        result = delta_mod.run_after_session(
            _cfg(), session_id=session_id, start_time=start, end_time=end
        )
        assert result.applied

    with fts.cursor() as conn:
        assertion_candidate = conn.execute(
            "SELECT status FROM model_candidates WHERE candidate_kind='assertion'"
        ).fetchone()
        assert assertion_candidate is not None and assertion_candidate["status"] == "promoted"

    entity_only = _payload(assertions=[], relations=[], events=[])
    fake_llm.set_default(delta_mod.STAGE, entity_only)
    for session_id, start, end in windows[2:]:
        result = delta_mod.run_after_session(
            _cfg(), session_id=session_id, start_time=start, end_time=end
        )
        assert result.applied

    with fts.cursor() as conn:
        points = conn.execute(
            "SELECT content, tags FROM evo_nodes WHERE file_name='person-\u5f20\u4e09.md' "
            "AND is_latest=1 AND status='active' ORDER BY content"
        ).fetchall()
    assert any("entity" in str(row["tags"]) and row["content"] == "\u5f20\u4e09" for row in points)
    assert any(
        "fact" in str(row["tags"])
        and row["content"] == "\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba"
        for row in points
    )


def test_late_subject_respects_disabled_assertion_materialization(ac_root, fake_llm) -> None:
    base = datetime(2026, 7, 2, 9, 0).astimezone()
    windows: list[tuple[str, datetime, datetime]] = []
    with fts.cursor() as conn:
        timeline_store.ensure_schema(conn)
        for index in range(4):
            start = base + timedelta(hours=index)
            end = start + timedelta(minutes=2)
            session_id = f"s-late-disabled-{index + 1}"
            timeline_store.insert(
                conn,
                _block(start, [SESSION_ENTRY], ["Feishu"], normalization_status="llm"),
            )
            windows.append((session_id, start, end))
    for session_id, start, end in windows:
        _seed_session(session_id, start, end)

    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(entities=[], relations=[], events=[]),
    )
    for session_id, start, end in windows[:2]:
        result = delta_mod.run_after_session(
            _cfg(),
            session_id=session_id,
            start_time=start,
            end_time=end,
        )
        assert result.applied

    entity_cfg = _cfg()
    entity_cfg.memory_delta.apply_assertions = False
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(assertions=[], relations=[], events=[]),
    )
    for session_id, start, end in windows[2:]:
        result = delta_mod.run_after_session(
            entity_cfg,
            session_id=session_id,
            start_time=start,
            end_time=end,
        )
        assert result.applied

    with fts.cursor() as conn:
        assertion_candidate = conn.execute(
            "SELECT status FROM model_candidates WHERE candidate_kind='assertion'"
        ).fetchone()
        points = conn.execute(
            "SELECT content, tags FROM evo_nodes WHERE file_name='person-\u5f20\u4e09.md' "
            "AND is_latest=1 AND status='active' ORDER BY content"
        ).fetchall()
    assert assertion_candidate is not None and assertion_candidate["status"] == "promoted"
    assert any("entity" in str(row["tags"]) and row["content"] == "\u5f20\u4e09" for row in points)
    assert not any("fact" in str(row["tags"]) for row in points)


def test_unwindowed_legacy_delta_keeps_context_free_event_and_point_apply(
    ac_root, fake_llm
) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    payload = {
        "owner_alias_candidates": [],
        "entities": [
            {
                "new_entity": "Legacy Person",
                "kind": "person",
                "quote": "legacy evidence",
                "confidence": 0.9,
                "ended": False,
            }
        ],
        "assertions": [],
        "relations": [],
        "events": [
            {
                "title": "Legacy Review",
                "participants": [{"ref": "self"}],
                "quote": "legacy evidence",
                "confidence": 0.9,
            }
        ],
    }
    with fts.cursor() as conn:
        legacy_id = deltas_store.insert(
            conn,
            session_id="s-legacy-unwindowed",
            payload=payload,
            apply_status="not_requested",
        )

    result = delta_mod.ensure_after_session(
        _cfg(),
        session_id="s-legacy-unwindowed",
        start_time=start,
        end_time=end,
    )

    assert result.delta_id == legacy_id
    assert result.applied and result.skipped_reason == "resumed_apply"
    assert fake_llm.calls == []
    with fts.cursor() as conn:
        legacy = conn.execute(
            "SELECT window_start, window_end FROM memory_deltas WHERE id=?", (legacy_id,)
        ).fetchone()
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        candidate_count = (
            conn.execute("SELECT COUNT(*) FROM model_candidates").fetchone()[0]
            if "model_candidates" in tables
            else 0
        )
        occurrence_count = (
            conn.execute("SELECT COUNT(*) FROM event_occurrences").fetchone()[0]
            if "event_occurrences" in tables
            else 0
        )
        edge = conn.execute(
            "SELECT dst_identity, source_kind FROM relation_edges WHERE predicate='participates_in'"
        ).fetchone()
    assert tuple(legacy) == ("", "")
    assert candidate_count == 0
    assert occurrence_count == 0
    assert edge is not None and edge["dst_identity"].startswith("event:")
    assert not edge["dst_identity"].startswith("event:occurrence:")
    assert edge["source_kind"] is None


def test_pre_ledger_failed_delta_with_possible_partial_effects_fails_closed(
    ac_root,
) -> None:
    from persome.writer import delta_apply

    start, end = _seed_session_blocks([SESSION_ENTRY])
    payload = json.loads(_payload(assertions=[], relations=[], events=[]))
    cfg = _cfg()

    # Model the failure mode of the pre-ledger implementation: an effect was
    # committed, but the parent row never reached applied. There is no receipt
    # capable of proving which effects completed.
    with fts.cursor() as conn:
        applied = delta_apply.apply_delta(conn, cfg, payload)
        assert applied.floor_edges == 1
        legacy_id = deltas_store.insert(
            conn,
            session_id="s-legacy-partial",
            payload=payload,
            apply_status="failed",
            window_start=start,
            window_end=end,
            is_final=False,
        )
        before = conn.execute(
            "SELECT observations FROM relation_edges "
            "WHERE predicate='engaged_with' AND valid_to IS NULL"
        ).fetchone()[0]

    result = delta_mod.ensure_active_window(
        cfg,
        session_id="s-legacy-partial",
        start_time=start,
        end_time=end,
    )

    assert result.delta_id == legacy_id
    assert result.applied is False
    assert result.skipped_reason == "legacy_apply_ambiguous"
    with fts.cursor() as conn:
        parent = conn.execute(
            "SELECT apply_status, item_ledger_version FROM memory_deltas WHERE id=?",
            (legacy_id,),
        ).fetchone()
        after = conn.execute(
            "SELECT observations FROM relation_edges "
            "WHERE predicate='engaged_with' AND valid_to IS NULL"
        ).fetchone()[0]
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        item_count = (
            conn.execute(
                "SELECT COUNT(*) FROM memory_delta_items WHERE delta_id=?", (legacy_id,)
            ).fetchone()[0]
            if "memory_delta_items" in tables
            else 0
        )
        effect_count = (
            conn.execute("SELECT COUNT(*) FROM relation_edge_effects").fetchone()[0]
            if "relation_edge_effects" in tables
            else 0
        )
    assert tuple(parent) == ("failed", 0)
    assert before == after == 1
    assert item_count == 0
    assert effect_count == 0


def test_roster_reserves_self_and_owner_aliases(ac_root) -> None:
    cfg = _cfg()
    cfg.memory_delta.owner_aliases = [
        "Casey Example",
        "\u793a\u4f8b\u7532",
        "Casey-Example",
    ]

    roster = delta_mod._load_roster(cfg)

    assert roster[0] == (
        "self",
        ["Casey Example", "\u793a\u4f8b\u7532", "Casey-Example"],
    )
    rendered = delta_mod._render_roster(roster)
    assert "self" in rendered and "memory owner" in rendered


def test_owner_alias_canonicalizes_to_self_but_never_mints_person(ac_root) -> None:
    from persome.evomem import identity as identity_mod

    roster = identity_mod.Roster.build([("self", ["Casey-Example"]), ("Kevin", [])])
    quote = "Casey-Example reviewed the launch plan with Kevin"
    raw = {
        "entities": [
            {"ref": "Casey-Example", "kind": "person", "quote": quote, "confidence": 0.9},
            {"ref": "Kevin", "kind": "person", "quote": quote, "confidence": 0.9},
        ],
        "assertions": [],
        "relations": [
            {
                "src": {"ref": "Casey-Example"},
                "dst": {"ref": "Kevin"},
                "predicate": "knows",
                "label": "teammates",
                "quote": quote,
                "confidence": 0.9,
            }
        ],
        "events": [],
    }

    clean, dropped = delta_mod.gate_delta(
        raw, roster=roster, session_text=quote, min_confidence=0.5
    )

    assert dropped == 1
    assert [entity["ref"] for entity in clean["entities"]] == ["Kevin"]
    assert clean["relations"][0]["src"] == {"ref": "self"}
    assert clean["relations"][0]["dst"] == {"ref": "Kevin"}


def test_ai_owner_alias_evidence_promotes_without_user_config(ac_root, fake_llm) -> None:
    quote = "Opened the user's own GitHub account Casey-Example with Kevin"
    start, end = _seed_session_blocks([f"[Chrome] {quote}"])
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            owner_alias_candidates=[
                {
                    "alias": "Casey-Example",
                    "source_kind": "owned_account",
                    "quote": quote,
                    "confidence": 0.94,
                }
            ],
            entities=[
                {
                    "new_entity": "Casey-Example",
                    "kind": "person",
                    "quote": quote,
                    "confidence": 0.94,
                },
                {
                    "new_entity": "Kevin",
                    "kind": "person",
                    "quote": quote,
                    "confidence": 0.9,
                },
            ],
            assertions=[],
            relations=[
                {
                    "src": {"new_entity": "Casey-Example"},
                    "dst": {"new_entity": "Kevin"},
                    "predicate": "knows",
                    "label": "collaborators",
                    "quote": quote,
                    "confidence": 0.9,
                }
            ],
        ),
    )
    cfg = _cfg()

    first = delta_mod.run_after_session(
        cfg, session_id="owner-session-1", start_time=start, end_time=end
    )
    assert first.counts["owner_alias_candidates"] == 1
    with fts.cursor() as conn:
        assert (
            conn.execute(
                "SELECT status FROM owner_aliases WHERE alias_key='casey-example'"
            ).fetchone()[0]
            == "pending"
        )
        payload = json.loads(deltas_store.latest_for_session(conn, "owner-session-1")["payload"])
    assert all(entity.get("new_entity") != "Casey-Example" for entity in payload["entities"])
    assert payload["relations"] == []

    second = delta_mod.run_after_session(
        cfg, session_id="owner-session-2", start_time=start, end_time=end
    )
    assert second.counts["owner_alias_candidates"] == 1
    with fts.cursor() as conn:
        row = conn.execute(
            "SELECT status, evidence_count FROM owner_aliases WHERE alias_key='casey-example'"
        ).fetchone()
        payload = json.loads(deltas_store.latest_for_session(conn, "owner-session-2")["payload"])
        owner_points = conn.execute(
            "SELECT COUNT(*) FROM evo_nodes WHERE file_name='person-casey-example.md'"
            " AND is_latest=1 AND status='active'"
        ).fetchone()[0]
    assert tuple(row) == ("active", 2)
    assert payload["relations"][0]["src"] == {"ref": "self"}
    assert owner_points == 0
    assert delta_mod._load_roster(cfg)[0] == ("self", ["Casey-Example"])


def test_render_blocks_excludes_local_model_output_and_mixed_focus(ac_root) -> None:
    start = datetime(2026, 7, 12, 9, 0).astimezone()
    block = TimelineBlock(
        start_time=start,
        end_time=start + timedelta(minutes=1),
        entries=[
            "[Google Chrome] Persome Personal Model (http://127.0.0.1:8742/model): "
            "read Root claiming Kevin is the owner.",
            "[Feishu] Project chat: Kevin said the release is ready.",
        ],
        apps_used=["Google Chrome", "Feishu"],
        capture_count=2,
        focus_excerpt="Root: Kevin is the owner",
        normalization_status="legacy",
    )

    rendered = delta_mod._render_blocks([block])

    assert "release is ready" in rendered
    assert "127.0.0.1:8742/model" not in rendered
    assert "Root: Kevin is the owner" not in rendered


def test_quote_evidence_gate_drops_unquoted_items(ac_root, fake_llm) -> None:
    """No verbatim quote from the session text → the item never lands (§4.1)."""
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            entities=[
                {
                    "new_entity": "\u5f20\u4e09",
                    "kind": "person",
                    "quote": "\u8fd9\u53e5\u8bdd\u4e0d\u5728\u4f1a\u8bdd\u91cc",
                    "confidence": 0.9,
                }
            ],
            assertions=[],
        ),
    )
    result = delta_mod.run_after_session(_cfg(), session_id="s3", start_time=start, end_time=end)
    assert result.written and result.counts["entities"] == 0 and result.dropped == 1


def test_identity_gate_rejects_bare_store_probing_strings(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            entities=[
                {
                    "new_entity": "\u51ed\u7a7a\u634f\u9020\u7684\u4eba",
                    "kind": "person",
                    "quote": "\u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba",
                    "confidence": 0.9,
                }
            ],
            assertions=[],
        ),
    )
    result = delta_mod.run_after_session(_cfg(), session_id="s4", start_time=start, end_time=end)
    assert result.counts["entities"] == 0 and result.dropped == 1


def test_relation_gate_enforces_closed_predicate_set(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            entities=[],
            assertions=[],
            relations=[
                {
                    "src": _ref("\u5f20\u4e09"),
                    "dst": _ref("\u5f20\u4e09"),
                    "predicate": "loves",  # not in the 6-predicate closed set
                    "label": "",
                    "quote": "\u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba",
                    "confidence": 0.9,
                },
                {
                    "src": _ref("\u5f20\u4e09"),
                    "dst": _ref("\u5f20\u4e09"),
                    "predicate": "knows",
                    "label": "\u540c\u4e8b",
                    "quote": "\u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba",
                    "confidence": 0.9,
                },
            ],
        ),
    )
    result = delta_mod.run_after_session(_cfg(), session_id="s5", start_time=start, end_time=end)
    assert result.counts["relations"] == 1 and result.dropped == 1


def test_confidence_floor_drops_hedges(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            entities=[
                {
                    "new_entity": "\u5f20\u4e09",
                    "kind": "person",
                    "quote": "\u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba",
                    "confidence": 0.2,
                }
            ],
            assertions=[],
        ),
    )
    result = delta_mod.run_after_session(_cfg(), session_id="s6", start_time=start, end_time=end)
    assert result.counts["entities"] == 0 and result.dropped == 1


def test_malformed_llm_output_fails_open(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(delta_mod.STAGE, "not json at all {{{")
    result = delta_mod.run_after_session(_cfg(), session_id="s7", start_time=start, end_time=end)
    assert not result.written and result.skipped_reason == "unparseable"
    with fts.cursor() as conn:
        assert deltas_store.latest_for_session(conn, "s7") is None


def test_no_blocks_skips_without_llm(ac_root, fake_llm) -> None:
    now = datetime.now().astimezone()
    result = delta_mod.run_after_session(
        _cfg(), session_id="s8", start_time=now - timedelta(minutes=5), end_time=now
    )
    assert result.skipped_reason == "no_blocks" and fake_llm.calls == []


def test_no_eligible_evidence_skips_without_llm(ac_root, fake_llm) -> None:
    start = datetime(2026, 7, 2, 10, 0).astimezone()
    end = start + timedelta(minutes=1)
    with fts.cursor() as conn:
        timeline_store.insert(
            conn,
            _block(
                start,
                ["[Cursor] active, involving —"],
                ["Cursor"],
                normalization_status="llm_failed",
            ),
        )

    result = delta_mod.run_after_session(
        _cfg(), session_id="s-ineligible", start_time=start, end_time=end
    )

    assert result.skipped_reason == "no_eligible_evidence"
    assert result.written is False
    assert fake_llm.calls == []
    with fts.cursor() as conn:
        assert deltas_store.latest_for_session(conn, "s-ineligible") is None


def test_mixed_quality_prompt_and_quote_gate_exclude_ineligible_text(ac_root, fake_llm) -> None:
    start = datetime(2026, 7, 2, 10, 10).astimezone()
    middle = start + timedelta(minutes=1)
    end = middle + timedelta(minutes=1)
    with fts.cursor() as conn:
        timeline_store.insert(
            conn,
            _block(start, [SESSION_ENTRY], ["Feishu"], normalization_status="llm"),
        )
        timeline_store.insert(
            conn,
            _block(
                middle,
                ["[Mail] Secret Phantom approved a fabricated launch"],
                ["Mail"],
                normalization_status="llm_malformed",
            ),
        )
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            entities=[
                {
                    "new_entity": "Secret Phantom",
                    "kind": "person",
                    "quote": "Secret Phantom approved a fabricated launch",
                    "confidence": 0.9,
                }
            ],
            assertions=[],
        ),
    )

    result = delta_mod.run_after_session(
        _cfg(), session_id="s-mixed-quality", start_time=start, end_time=end
    )

    prompt = json.dumps(fake_llm.calls[0]["messages"], ensure_ascii=False)
    assert "Secret Phantom" not in prompt
    assert result.counts["entities"] == 0
    assert result.dropped == 1


def test_stats_preserves_legacy_append_rows_but_aggregates_latest(ac_root) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    with fts.cursor() as conn:
        for _ in range(2):
            deltas_store.insert(
                conn,
                session_id="s9",
                payload=json.loads(_payload()),
                window_start=start,
                window_end=end,
            )
        agg = deltas_store.stats(conn)
    assert agg["rows"] == 2 and agg["sessions"] == 1  # latest-per-session, not double-counted
    assert agg["heads"]["entities"] == 1


def test_active_windows_are_incremental_and_idempotent(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY, SESSION_ENTRY])
    middle = start + timedelta(minutes=1)
    fake_llm.set_default(delta_mod.STAGE, _payload())
    cfg = _cfg()

    first = delta_mod.ensure_active_window(
        cfg,
        session_id="s-live",
        start_time=start,
        end_time=middle,
    )
    duplicate = delta_mod.ensure_active_window(
        cfg,
        session_id="s-live",
        start_time=start,
        end_time=middle,
    )
    second = delta_mod.ensure_active_window(
        cfg,
        session_id="s-live",
        start_time=middle,
        end_time=end,
    )

    assert first.written and first.applied
    assert duplicate.skipped_reason == "already_processed"
    assert second.written and second.applied
    assert len(fake_llm.calls) == 2
    with fts.cursor() as conn:
        rows = conn.execute(
            "SELECT window_start, window_end, is_final FROM memory_deltas"
            " WHERE session_id=? ORDER BY id",
            ("s-live",),
        ).fetchall()
    assert [(row["window_start"], row["window_end"], row["is_final"]) for row in rows] == [
        (start.isoformat(), middle.isoformat(), 0),
        (middle.isoformat(), end.isoformat(), 0),
    ]


def test_owner_edit_empty_window_does_not_create_a_claim(ac_root) -> None:
    with fts.cursor() as conn:
        deltas_store.insert(conn, session_id="owner-edit", payload={})
        count = conn.execute("SELECT COUNT(*) FROM memory_delta_window_claims").fetchone()[0]
    assert count == 0


def test_canonical_window_reuses_timezone_equivalent_persisted_delta(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    start_plus_eight = start_utc.astimezone(timezone(timedelta(hours=8)))
    end_plus_eight = end_utc.astimezone(timezone(timedelta(hours=8)))
    fake_llm.set_default(delta_mod.STAGE, _payload())
    cfg = _cfg()

    first = delta_mod.ensure_active_window(
        cfg,
        session_id="s-canonical",
        start_time=start_utc,
        end_time=end_utc,
    )
    duplicate = delta_mod.ensure_active_window(
        cfg,
        session_id="s-canonical",
        start_time=start_plus_eight,
        end_time=end_plus_eight,
    )

    assert first.written
    assert duplicate.skipped_reason == "already_processed"
    assert duplicate.delta_id == first.delta_id
    assert len(fake_llm.calls) == 1
    assert deltas_store.canonical_window_key(
        datetime.fromisoformat("2026-07-02T01:00:00Z"),
        datetime.fromisoformat("2026-07-02T01:02:00+00:00"),
    ) == deltas_store.canonical_window_key(
        datetime.fromisoformat("2026-07-02T09:00:00+08:00"),
        datetime.fromisoformat("2026-07-02T09:02:00+08:00"),
    )


def test_legacy_timezone_window_is_reused_and_backfilled(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    legacy_start = start.astimezone(timezone(timedelta(hours=8)))
    legacy_end = end.astimezone(timezone(timedelta(hours=8)))
    with fts.cursor() as conn:
        legacy_id = deltas_store.insert(
            conn,
            session_id="s-legacy-window",
            payload=json.loads(_payload()),
            window_start=legacy_start,
            window_end=legacy_end,
        )
    fake_llm.set_default(delta_mod.STAGE, _payload())

    result = delta_mod.ensure_active_window(
        _cfg(),
        session_id="s-legacy-window",
        start_time=start.astimezone(UTC),
        end_time=end.astimezone(UTC),
    )

    assert result.skipped_reason in {"already_processed", "resumed_apply"}
    assert result.delta_id == legacy_id
    assert fake_llm.calls == []
    with fts.cursor() as conn:
        row = conn.execute(
            "SELECT state, delta_id FROM memory_delta_window_claims"
            " WHERE session_id='s-legacy-window'"
        ).fetchone()
    assert tuple(row) == (deltas_store.CLAIM_PERSISTED, legacy_id)


def test_two_connections_claim_one_llm_call_for_the_same_window(ac_root) -> None:
    from persome.writer.llm import _build_response

    start, end = _seed_session_blocks([SESSION_ENTRY])
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def blocking_llm(_cfg, _stage, _messages):
        nonlocal calls
        with calls_lock:
            calls += 1
        entered.set()
        assert release.wait(timeout=5)
        return _build_response(_payload())

    cfg = _cfg()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(
            delta_mod.run_after_session,
            cfg,
            session_id="s-concurrent",
            start_time=start,
            end_time=end,
            llm_call=blocking_llm,
        )
        assert entered.wait(timeout=5)
        second_future = pool.submit(
            delta_mod.run_after_session,
            cfg,
            session_id="s-concurrent",
            start_time=start,
            end_time=end,
            llm_call=blocking_llm,
        )
        second = second_future.result(timeout=5)
        release.set()
        first = first_future.result(timeout=5)

    assert calls == 1
    assert first.written
    assert second.skipped_reason == "claim_in_progress"
    with fts.cursor() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM memory_deltas WHERE session_id='s-concurrent'"
            ).fetchone()[0]
            == 1
        )


def test_expired_claim_uses_token_cas_and_rejects_stale_payload(ac_root) -> None:
    start = datetime(2026, 7, 2, 1, 0, tzinfo=UTC)
    end = start + timedelta(minutes=2)
    first_now = datetime(2026, 7, 2, 2, 0, tzinfo=UTC)
    with fts.cursor() as conn:
        first = deltas_store.claim_window(
            conn,
            session_id="s-lease",
            window_start=start,
            window_end=end,
            now=first_now,
            lease_seconds=10,
        )
    with fts.cursor() as conn:
        live = deltas_store.claim_window(
            conn,
            session_id="s-lease",
            window_start=start,
            window_end=end,
            now=first_now + timedelta(seconds=5),
            lease_seconds=10,
        )
        reclaimed = deltas_store.claim_window(
            conn,
            session_id="s-lease",
            window_start=start,
            window_end=end,
            now=first_now + timedelta(seconds=11),
            lease_seconds=10,
        )

    assert first.acquired
    assert not live.acquired
    assert reclaimed.acquired and reclaimed.token != first.token
    with fts.cursor() as conn:
        with pytest.raises(deltas_store.ClaimLostError):
            deltas_store.insert_for_claim(
                conn,
                session_id="s-lease",
                payload={},
                token=first.token,
                window_start=start,
                window_end=end,
            )
        delta_id = deltas_store.insert_for_claim(
            conn,
            session_id="s-lease",
            payload={},
            token=reclaimed.token,
            window_start=start,
            window_end=end,
        )
        persisted = deltas_store.persisted_for_window(
            conn,
            "s-lease",
            window_start=start.astimezone(timezone(timedelta(hours=8))),
            window_end=end.astimezone(timezone(timedelta(hours=8))),
        )
    assert persisted is not None and persisted["id"] == delta_id


def test_gate_canonicalizes_honorific_ref_through_the_funnel(ac_root) -> None:
    from persome.evomem import identity as identity_mod

    roster = identity_mod.Roster.build([("\u5f20\u4f1f", ["\u4f1f\u54e5"])])
    session_text = "[Feishu] \u804a\u5929: \u5f20\u603b\u786e\u8ba4\u4e86\u5bf9\u8d26\u65b9\u6848"
    raw = {
        "entities": [
            {
                "ref": "\u5f20\u603b",
                "kind": "person",
                "quote": "\u5f20\u603b\u786e\u8ba4\u4e86\u5bf9\u8d26\u65b9\u6848",
                "confidence": 0.9,
            }
        ],
        "assertions": [],
        "relations": [],
        "events": [],
    }
    clean, dropped = delta_mod.gate_delta(
        raw, roster=roster, session_text=session_text, min_confidence=0.5
    )
    assert dropped == 0
    assert clean["entities"][0]["ref"] == "\u5f20\u4f1f"  # canonicalized, not the raw mention
    assert "new_entity" not in clean["entities"][0]


def test_gate_adds_deterministic_cooccurrence_knows(ac_root) -> None:
    from persome.evomem import identity as identity_mod

    roster = identity_mod.Roster.build(
        [("\u5f20\u4f1f", []), ("\u674e\u56db", []), ("\u738b\u4e94", [])]
    )
    session_text = "[Feishu] \u7fa4\u804a: \u5f20\u4f1f\u3001\u674e\u56db\u3001\u738b\u4e94 \u4e09\u4eba\u4e00\u8d77\u8fc7\u4e86\u65b9\u6848"
    q = "\u5f20\u4f1f\u3001\u674e\u56db\u3001\u738b\u4e94 \u4e09\u4eba\u4e00\u8d77\u8fc7\u4e86\u65b9\u6848"
    raw = {
        "entities": [
            {"ref": "\u5f20\u4f1f", "kind": "person", "quote": q, "confidence": 0.9},
            {"ref": "\u674e\u56db", "kind": "person", "quote": q, "confidence": 0.9},
            {"ref": "\u738b\u4e94", "kind": "person", "quote": q, "confidence": 0.9},
        ],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    clean, _ = delta_mod.gate_delta(
        raw, roster=roster, session_text=session_text, min_confidence=0.5
    )
    knows = {
        frozenset((r["src"]["ref"], r["dst"]["ref"]))
        for r in clean["relations"]
        if r["predicate"] == "knows"
    }
    assert knows == {
        frozenset(("\u5f20\u4f1f", "\u674e\u56db")),
        frozenset(("\u5f20\u4f1f", "\u738b\u4e94")),
        frozenset(("\u674e\u56db", "\u738b\u4e94")),
    }


def test_gate_cooccurrence_off_is_noop(ac_root) -> None:
    from persome.evomem import identity as identity_mod

    roster = identity_mod.Roster.build([("\u5f20\u4f1f", []), ("\u674e\u56db", [])])
    raw = {
        "entities": [
            {
                "ref": "\u5f20\u4f1f",
                "kind": "person",
                "quote": "\u5f20\u4f1f \u548c \u674e\u56db",
                "confidence": 0.9,
            },
            {
                "ref": "\u674e\u56db",
                "kind": "person",
                "quote": "\u5f20\u4f1f \u548c \u674e\u56db",
                "confidence": 0.9,
            },
        ],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    clean, _ = delta_mod.gate_delta(
        raw,
        roster=roster,
        session_text="[Feishu] \u5f20\u4f1f \u548c \u674e\u56db",
        min_confidence=0.5,
        cooccurrence=False,
    )
    assert clean["relations"] == []


def test_gate_folds_known_name_posing_as_new_entity(ac_root) -> None:
    """A new_entity whose name resolves to a known identity folds to its ref —
    the LLM cannot re-mint an existing person as a fresh node."""
    from persome.evomem import identity as identity_mod

    roster = identity_mod.Roster.build([("\u5f20\u4f1f", ["\u4f1f\u54e5"])])
    session_text = "[Feishu] \u804a\u5929: \u4f1f\u54e5\u786e\u8ba4\u4e86\u5bf9\u8d26\u65b9\u6848"
    raw = {
        "entities": [
            {
                "new_entity": "\u4f1f\u54e5",
                "kind": "person",
                "quote": "\u4f1f\u54e5\u786e\u8ba4\u4e86\u5bf9\u8d26\u65b9\u6848",
                "confidence": 0.9,
            }
        ],
        "assertions": [],
        "relations": [],
        "events": [],
    }
    clean, dropped = delta_mod.gate_delta(
        raw, roster=roster, session_text=session_text, min_confidence=0.5
    )
    assert dropped == 0
    assert (
        clean["entities"][0].get("ref") == "\u5f20\u4f1f"
        and "new_entity" not in clean["entities"][0]
    )


def test_gate_rejects_unknown_ref_but_keeps_genuine_new_entity(ac_root) -> None:
    from persome.evomem import identity as identity_mod

    roster = identity_mod.Roster.build([("\u5f20\u4f1f", [])])
    session_text = (
        "[Feishu] \u804a\u5929: \u738b\u4e94\u63d0\u4ea4\u4e86\u65b0\u7684\u63a5\u53e3\u6587\u6863"
    )
    raw = {
        "entities": [
            {
                "ref": "\u738b\u4e94",
                "kind": "person",
                "quote": "\u738b\u4e94\u63d0\u4ea4\u4e86\u65b0\u7684\u63a5\u53e3\u6587\u6863",
                "confidence": 0.9,
            },
            {
                "new_entity": "\u738b\u4e94",
                "kind": "person",
                "quote": "\u738b\u4e94\u63d0\u4ea4\u4e86\u65b0\u7684\u63a5\u53e3\u6587\u6863",
                "confidence": 0.9,
            },
        ],
        "assertions": [],
        "relations": [],
        "events": [],
    }
    clean, dropped = delta_mod.gate_delta(
        raw, roster=roster, session_text=session_text, min_confidence=0.5
    )
    assert dropped == 1  # the bare ref probing the store
    assert clean["entities"] == [
        {
            "kind": "person",
            "quote": "\u738b\u4e94\u63d0\u4ea4\u4e86\u65b0\u7684\u63a5\u53e3\u6587\u6863",
            "confidence": 0.9,
            "new_entity": "\u738b\u4e94",
            "ended": False,
        }
    ]


def test_relation_polarity_and_ended_normalize(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            entities=[],
            assertions=[],
            relations=[
                {
                    "src": _ref("\u5f20\u4e09"),
                    "dst": _ref("\u5f20\u4e09"),
                    "predicate": "knows",
                    "polarity": "positive",  # off-set → coerced to "0"
                    "ended": "yes",  # non-bool → False
                    "quote": "\u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba",
                    "confidence": 0.9,
                },
                {
                    "src": _ref("\u5f20\u4e09"),
                    "dst": _ref("\u5f20\u4e09"),
                    "predicate": "knows",
                    "polarity": "-",
                    "ended": True,
                    "quote": "\u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba",
                    "confidence": 0.9,
                },
            ],
        ),
    )
    result = delta_mod.run_after_session(_cfg(), session_id="s9", start_time=start, end_time=end)
    assert result.counts["relations"] == 2
    import json as _json

    from persome.store import fts as fts_store
    from persome.store import memory_deltas as deltas_store

    with fts_store.cursor() as conn:
        row = deltas_store.latest_for_session(conn, "s9")
    rels = _json.loads(row["payload"])["relations"]
    assert (rels[0]["polarity"], rels[0]["ended"]) == ("0", False)
    assert (rels[1]["polarity"], rels[1]["ended"]) == ("-", True)


def test_entity_ended_defaults_false(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            entities=[
                {
                    "new_entity": "\u5f20\u4e09",
                    "kind": "person",
                    "quote": "\u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba",
                    "confidence": 0.9,
                }
            ],
            assertions=[],
            relations=[],
        ),
    )
    result = delta_mod.run_after_session(_cfg(), session_id="sa", start_time=start, end_time=end)
    assert result.counts["entities"] == 1
    import json as _json

    from persome.store import fts as fts_store
    from persome.store import memory_deltas as deltas_store

    with fts_store.cursor() as conn:
        row = deltas_store.latest_for_session(conn, "sa")
    assert _json.loads(row["payload"])["entities"][0]["ended"] is False
