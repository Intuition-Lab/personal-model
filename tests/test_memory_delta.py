"""Session memory-delta extraction, gates, persistence, and apply.

Covers the deterministic gates (quote evidence / roster multiple-choice /
closed predicate set / confidence floor), persistence, and safe degradation.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from persome import config as config_mod
from persome import paths
from persome.session import store as session_store
from persome.store import fts
from persome.store import memory_deltas as deltas_store
from persome.timeline import store as timeline_store
from persome.timeline.store import TimelineBlock
from persome.writer import memory_delta as delta_mod


def _block(start: datetime, entries: list[str], apps: list[str]) -> TimelineBlock:
    return TimelineBlock(
        start_time=start,
        end_time=start + timedelta(minutes=1),
        entries=entries,
        apps_used=apps,
        capture_count=len(entries),
    )


def _capture_id(ts: datetime) -> str:
    stem = ts.isoformat().replace(":", "-").replace("+", "p")
    return f"capture:{stem}"


def _write_capture(
    ts: datetime,
    text: str,
    *,
    app: str = "Editor",
    trigger: dict | None = None,
) -> None:
    payload = {
        "timestamp": ts.isoformat(),
        "schema_version": 2,
        "trigger": trigger,
        "window_meta": {
            "app_name": app,
            "title": "Cutoff test",
            "bundle_id": f"test.{app.casefold()}",
        },
        "focused_element": {"role": "AXStaticText", "value": text},
        "visible_text": text,
    }
    stem = ts.isoformat().replace(":", "-").replace("+", "p")
    path = paths.capture_buffer_dir() / f"{stem}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_opaque_capture(ts: datetime, text: str, capture_id: str) -> None:
    _write_capture(ts, text)
    timestamp_stem = ts.isoformat().replace(":", "-").replace("+", "p")
    source = paths.capture_buffer_dir() / f"{timestamp_stem}.json"
    source.rename(paths.capture_buffer_dir() / f"{capture_id}.json")


def _seed_session_blocks(entries: list[str]) -> tuple[datetime, datetime]:
    start = datetime(2026, 7, 2, 9, 0).astimezone()
    with fts.cursor() as conn:
        timeline_store.ensure_schema(conn)
        for i, entry in enumerate(entries):
            timeline_store.insert(conn, _block(start + timedelta(minutes=i), [entry], ["Feishu"]))
    return start, start + timedelta(minutes=len(entries) + 1)


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

    second_start = end + timedelta(minutes=1)
    second_end = second_start + timedelta(minutes=2)
    with fts.cursor() as conn:
        timeline_store.insert(
            conn,
            _block(second_start, [f"[Chrome] {quote}"], ["Chrome"]),
        )
    second = delta_mod.run_after_session(
        cfg,
        session_id="owner-session-2",
        start_time=second_start,
        end_time=second_end,
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


def test_terminal_partial_waits_for_closing_block_without_llm(ac_root, fake_llm) -> None:
    now = datetime(2026, 7, 2, 14, 5, 30).astimezone()
    _write_capture(now - timedelta(seconds=1), "OCCUPIED_CLOSING_SLICE")
    result = delta_mod.run_after_session(
        _cfg(), session_id="s8", start_time=now - timedelta(minutes=5), end_time=now
    )
    assert result.skipped_reason == "awaiting_closing_block" and fake_llm.calls == []


@pytest.mark.parametrize("same_session", [True, False])
def test_off_minute_adjacent_windows_claim_boundary_block_once(
    ac_root,
    fake_llm,
    same_session: bool,
) -> None:
    minute = datetime(2026, 7, 2, 9, 0).astimezone()
    early_text = "evidence before the active-window boundary"
    boundary_text = "evidence after the active-window boundary"
    following_text = "evidence in the following minute"
    boundary_block = _block(minute, [f"[Editor] {early_text}; {boundary_text}"], ["Editor"])
    following_block = _block(
        minute + timedelta(minutes=1),
        [f"[Browser] {following_text}"],
        ["Browser"],
    )
    capture_times = [
        minute + timedelta(seconds=20),
        minute + timedelta(seconds=50),
        minute + timedelta(minutes=1, seconds=10),
    ]
    _write_capture(capture_times[0], early_text)
    _write_capture(capture_times[1], boundary_text)
    _write_capture(capture_times[2], following_text, app="Browser")
    with fts.cursor() as conn:
        timeline_store.ensure_schema(conn)
        timeline_store.insert(conn, boundary_block)
        timeline_store.insert(conn, following_block)

    fake_llm.set_default(delta_mod.STAGE, _payload(entities=[], assertions=[]))
    first_session = "boundary-session"
    second_session = first_session if same_session else "next-session"
    first = delta_mod.ensure_active_window(
        _cfg(),
        session_id=first_session,
        start_time=minute + timedelta(seconds=10),
        end_time=minute + timedelta(seconds=40),
    )
    second = delta_mod.ensure_active_window(
        _cfg(),
        session_id=second_session,
        start_time=minute + timedelta(seconds=40),
        end_time=minute + timedelta(minutes=1, seconds=30),
    )
    assert first.written and first.applied
    assert second.written and second.applied
    assert len(fake_llm.calls) == 2
    first_sent = "".join(block["text"] for block in fake_llm.calls[0]["messages"][1]["content"])
    second_sent = "".join(block["text"] for block in fake_llm.calls[1]["messages"][1]["content"])
    assert early_text in first_sent
    assert boundary_text not in first_sent and following_text not in first_sent
    assert early_text not in second_sent
    assert boundary_text in second_sent and following_text in second_sent
    with fts.cursor() as conn:
        claims = conn.execute(
            "SELECT evidence_id, COUNT(*) AS uses FROM memory_delta_evidence_claims"
            " GROUP BY evidence_id ORDER BY evidence_id"
        ).fetchall()
    assert {row["evidence_id"] for row in claims} == {
        _capture_id(capture_time) for capture_time in capture_times
    }
    assert all(row["uses"] == 1 for row in claims)


@pytest.mark.parametrize("reverse_replay", [False, True])
def test_terminal_boundary_owner_is_stable_across_replay_order(
    ac_root,
    fake_llm,
    reverse_replay: bool,
) -> None:
    minute = datetime(2026, 7, 2, 10, 0).astimezone()
    opening = _block(minute, ["[Editor] previous-session opening"], ["Editor"])
    shared = _block(
        minute + timedelta(minutes=1),
        ["[Browser] minute shared by adjacent sessions"],
        ["Browser"],
    )
    closing = _block(
        minute + timedelta(minutes=2),
        ["[Terminal] final minute with no later session"],
        ["Terminal"],
    )
    first_start = minute + timedelta(seconds=30)
    boundary = minute + timedelta(minutes=1, seconds=30)
    second_end = minute + timedelta(minutes=2, seconds=30)
    first_capture = minute + timedelta(seconds=40)
    before_boundary = minute + timedelta(minutes=1, seconds=10)
    after_boundary = minute + timedelta(minutes=1, seconds=40)
    last_capture = minute + timedelta(minutes=2, seconds=10)
    _write_capture(first_capture, "previous-session opening")
    _write_capture(before_boundary, "shared minute before boundary", app="Browser")
    _write_capture(after_boundary, "shared minute after boundary", app="Browser")
    _write_capture(last_capture, "later-session closing", app="Terminal")
    with fts.cursor() as conn:
        for block in (opening, shared, closing):
            timeline_store.insert(conn, block)
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="previous-session",
                start_time=first_start,
                end_time=boundary,
                status="reduced",
            ),
        )
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="later-session",
                start_time=boundary,
                end_time=second_end,
                status="reduced",
            ),
        )

    fake_llm.set_default(delta_mod.STAGE, _payload(entities=[], assertions=[]))
    windows = [
        ("previous-session", first_start, boundary),
        ("later-session", boundary, second_end),
    ]
    if reverse_replay:
        windows.reverse()
    results = {
        session_id: delta_mod.ensure_after_session(
            _cfg(),
            session_id=session_id,
            start_time=start,
            end_time=end,
        )
        for session_id, start, end in windows
    }

    assert all(result.written and result.applied for result in results.values())
    with fts.cursor() as conn:
        claims = {
            row["session_id"]: deltas_store.evidence_ids_for_delta(conn, int(row["id"]))
            for row in conn.execute(
                "SELECT id, session_id FROM memory_deltas ORDER BY session_id"
            ).fetchall()
        }
    assert claims == {
        "previous-session": [_capture_id(first_capture), _capture_id(before_boundary)],
        "later-session": [_capture_id(after_boundary), _capture_id(last_capture)],
    }


def test_terminal_straddler_never_sends_post_cutoff_heartbeat_text(
    ac_root,
    fake_llm,
) -> None:
    minute = datetime(2026, 7, 2, 11, 0).astimezone()
    safe_capture = minute + timedelta(seconds=20)
    session_start = safe_capture
    terminal_capture = minute + timedelta(seconds=30)
    session_end = terminal_capture + timedelta(microseconds=1)
    heartbeat_capture = minute + timedelta(seconds=50)
    safe_text = "SAFE_PRE_CUTOFF_DECISION"
    terminal_text = "EXACT_TERMINAL_EVENT"
    secret_text = "POST_CUTOFF_HEARTBEAT_SECRET"

    _write_capture(safe_capture, safe_text)
    _write_capture(terminal_capture, terminal_text)
    _write_capture(heartbeat_capture, secret_text, trigger=None)
    closing = _block(
        minute,
        [f"[Editor] normalized whole minute: {safe_text}; {secret_text}"],
        ["Editor"],
    )
    closing.focus_excerpt = f"whole-minute focus also contains {secret_text}"
    with fts.cursor() as conn:
        timeline_store.insert(conn, closing)

    fake_llm.set_default(delta_mod.STAGE, _payload(entities=[], assertions=[]))
    result = delta_mod.ensure_after_session(
        _cfg(),
        session_id="cutoff-safe-terminal",
        start_time=session_start,
        end_time=session_end,
    )

    assert result.written and result.applied
    sent = "".join(block["text"] for block in fake_llm.calls[0]["messages"][1]["content"])
    assert safe_text in sent and terminal_text in sent
    assert secret_text not in sent
    with fts.cursor() as conn:
        claims = deltas_store.evidence_ids_for_delta(conn, result.delta_id)
    assert claims == [_capture_id(safe_capture), _capture_id(terminal_capture)]
    assert closing.id not in claims


@pytest.mark.parametrize("with_manifest", [True, False], ids=["manifest", "legacy"])
def test_full_block_fails_closed_after_partial_claim_and_raw_retention(
    ac_root,
    fake_llm,
    with_manifest: bool,
) -> None:
    minute = datetime(2026, 7, 2, 11, 30).astimezone()
    first_capture = minute + timedelta(seconds=10)
    second_capture = minute + timedelta(seconds=40)
    boundary = minute + timedelta(seconds=20)
    first_text = "PARTIAL_ALREADY_CONSUMED"
    second_text = "FULL_BLOCK_RESIDUAL"
    _write_capture(first_capture, first_text)
    _write_capture(second_capture, second_text)
    block = _block(
        minute,
        [f"[Editor] normalized whole minute: {first_text}; {second_text}"],
        ["Editor"],
    )
    block.capture_count = 2
    with fts.cursor() as conn:
        timeline_store.insert(
            conn,
            block,
            source_capture_ids=(
                [_capture_id(first_capture), _capture_id(second_capture)] if with_manifest else []
            ),
        )

    fake_llm.set_default(delta_mod.STAGE, _payload(entities=[], assertions=[]))
    partial = delta_mod.ensure_active_window(
        _cfg(),
        session_id=f"partial-{with_manifest}",
        start_time=minute,
        end_time=boundary,
    )
    assert partial.written and partial.applied and len(fake_llm.calls) == 1

    for path in paths.capture_buffer_dir().glob("*.json"):
        path.unlink()

    full = delta_mod.ensure_active_window(
        _cfg(),
        session_id=f"full-{with_manifest}",
        start_time=minute,
        end_time=minute + timedelta(minutes=1),
    )

    assert not full.written and full.skipped_reason == "no_cutoff_safe_blocks"
    assert len(fake_llm.calls) == 1
    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_deltas").fetchone()[0] == 1
        assert deltas_store.evidence_ids_for_delta(conn, partial.delta_id) == [
            _capture_id(first_capture)
        ]


def test_partial_manifest_missing_one_expected_source_fails_closed(
    ac_root,
    fake_llm,
) -> None:
    minute = datetime(2026, 7, 2, 11, 40).astimezone()
    first_capture = minute + timedelta(seconds=10)
    missing_capture = minute + timedelta(seconds=15)
    boundary = minute + timedelta(seconds=20)
    _write_capture(first_capture, "SURVIVING_BOUNDARY_SOURCE")
    _write_capture(missing_capture, "REMOVED_BOUNDARY_SOURCE")
    block = _block(minute, ["[Editor] normalized complete minute"], ["Editor"])
    with fts.cursor() as conn:
        timeline_store.insert(
            conn,
            block,
            source_capture_ids=[_capture_id(first_capture), _capture_id(missing_capture)],
        )
    missing_stem = _capture_id(missing_capture).removeprefix("capture:")
    (paths.capture_buffer_dir() / f"{missing_stem}.json").unlink()

    result = delta_mod.ensure_active_window(
        _cfg(),
        session_id="incomplete-partial-manifest",
        start_time=minute,
        end_time=boundary,
    )

    assert not result.written and result.skipped_reason == "no_cutoff_safe_blocks"
    assert fake_llm.calls == []


def test_partial_manifest_never_reintroduces_whole_block_excluded_capture(
    ac_root,
    fake_llm,
) -> None:
    minute = datetime(2026, 7, 2, 11, 45).astimezone()
    capture_ids: list[str] = []
    for index in range(31):
        timestamp = minute + timedelta(seconds=index)
        marker = "EXCLUDED_31ST_CAPTURE" if index == 0 else f"included-{index:02d}"
        _write_capture(timestamp, marker)
        capture_ids.append(_capture_id(timestamp))
    block = _block(minute, ["[Editor] bounded newest-30 minute"], ["Editor"])
    block.capture_count = 30
    with fts.cursor() as conn:
        timeline_store.insert(conn, block, source_capture_ids=capture_ids[1:])

    fake_llm.set_default(delta_mod.STAGE, _payload(entities=[], assertions=[]))
    result = delta_mod.ensure_active_window(
        _cfg(),
        session_id="bounded-partial",
        start_time=minute,
        end_time=minute + timedelta(seconds=10),
    )

    assert result.written and result.applied
    sent = "".join(block["text"] for block in fake_llm.calls[0]["messages"][1]["content"])
    assert "EXCLUDED_31ST_CAPTURE" not in sent
    assert "included-01" in sent and "included-09" in sent
    with fts.cursor() as conn:
        assert deltas_store.evidence_ids_for_delta(conn, result.delta_id) == capture_ids[1:10]


@pytest.mark.parametrize("remove_expected", [False, True], ids=["present", "missing"])
def test_opaque_manifest_timestamp_supports_partial_and_fails_closed_on_retention(
    ac_root,
    fake_llm,
    remove_expected: bool,
) -> None:
    minute = datetime(2026, 7, 2, 11, 48).astimezone()
    sources = [
        ("evt_alpha", minute + timedelta(seconds=10), "OPAQUE_ALPHA"),
        ("evt_beta", minute + timedelta(seconds=15), "OPAQUE_BETA"),
        ("evt_gamma", minute + timedelta(seconds=40), "OPAQUE_GAMMA"),
    ]
    for capture_id, timestamp, text in sources:
        _write_opaque_capture(timestamp, text, capture_id)
    block = _block(minute, ["[Editor] opaque normalized whole minute"], ["Editor"])
    block.capture_count = 3
    with fts.cursor() as conn:
        timeline_store.insert(
            conn,
            block,
            source_captures=[
                timeline_store.TimelineBlockSource(
                    capture_id=f"capture:{capture_id}",
                    captured_at=timestamp,
                )
                for capture_id, timestamp, _ in sources
            ],
        )
    if remove_expected:
        (paths.capture_buffer_dir() / "evt_beta.json").unlink()

    fake_llm.set_default(delta_mod.STAGE, _payload(entities=[], assertions=[]))
    partial = delta_mod.ensure_active_window(
        _cfg(),
        session_id=f"opaque-partial-{remove_expected}",
        start_time=minute,
        end_time=minute + timedelta(seconds=20),
    )

    if remove_expected:
        assert not partial.written and partial.skipped_reason == "no_cutoff_safe_blocks"
        assert fake_llm.calls == []
        return

    assert partial.written and partial.applied
    partial_sent = "".join(
        content["text"] for content in fake_llm.calls[0]["messages"][1]["content"]
    )
    assert "OPAQUE_ALPHA" in partial_sent and "OPAQUE_BETA" in partial_sent
    assert "OPAQUE_GAMMA" not in partial_sent
    with fts.cursor() as conn:
        assert deltas_store.evidence_ids_for_delta(conn, partial.delta_id) == [
            "capture:evt_alpha",
            "capture:evt_beta",
        ]

    full = delta_mod.ensure_active_window(
        _cfg(),
        session_id="opaque-full-replay",
        start_time=minute,
        end_time=minute + timedelta(minutes=1),
    )
    assert full.written and full.applied and len(fake_llm.calls) == 2
    full_sent = "".join(content["text"] for content in fake_llm.calls[1]["messages"][1]["content"])
    assert "OPAQUE_ALPHA" not in full_sent and "OPAQUE_BETA" not in full_sent
    assert "OPAQUE_GAMMA" in full_sent
    with fts.cursor() as conn:
        assert deltas_store.evidence_ids_for_delta(conn, full.delta_id) == [
            block.id,
            "capture:evt_gamma",
        ]


def test_modern_manifest_proves_empty_partial_before_next_session_sources(
    ac_root,
    fake_llm,
) -> None:
    minute = datetime(2026, 7, 2, 11, 49).astimezone()
    next_capture = minute + timedelta(seconds=40)
    _write_opaque_capture(next_capture, "NEXT_SESSION_ONLY", "evt_next_session")
    block = _block(minute, ["[Editor] next-session normalized minute"], ["Editor"])
    with fts.cursor() as conn:
        timeline_store.insert(
            conn,
            block,
            source_captures=[
                timeline_store.TimelineBlockSource(
                    capture_id="capture:evt_next_session",
                    captured_at=next_capture,
                )
            ],
        )

    result = delta_mod.run_after_session(
        _cfg(),
        session_id="empty-modern-partial",
        start_time=minute + timedelta(seconds=10),
        end_time=minute + timedelta(seconds=20),
    )

    assert not result.written and result.skipped_reason == "no_blocks"
    assert fake_llm.calls == []


def test_legacy_full_block_fails_closed_for_opaque_overlapping_capture_claim(
    ac_root,
    fake_llm,
) -> None:
    minute = datetime(2026, 7, 2, 11, 50).astimezone()
    block = _block(minute, ["[Editor] legacy normalized minute"], ["Editor"])
    with fts.cursor() as conn:
        timeline_store.insert(conn, block)
        deltas_store.insert(
            conn,
            session_id="opaque-partial-owner",
            payload={},
            window_start=minute,
            window_end=minute + timedelta(seconds=20),
            evidence_ids=["capture:opaque-legacy-source"],
        )

    result = delta_mod.ensure_active_window(
        _cfg(),
        session_id="legacy-full-replay",
        start_time=minute,
        end_time=minute + timedelta(minutes=1),
    )

    assert not result.written and result.skipped_reason == "no_cutoff_safe_blocks"
    assert fake_llm.calls == []


def test_overlap_cap_keeps_newest_120_blocks_in_chronological_prompt(
    ac_root,
    fake_llm,
) -> None:
    start = datetime(2026, 7, 2, 12, 0).astimezone()
    blocks = [
        _block(
            start + timedelta(minutes=index),
            [f"[Editor] overlap-{index:03d}"],
            ["Editor"],
        )
        for index in range(121)
    ]
    with fts.cursor() as conn:
        timeline_store.ensure_schema(conn)
        for block in blocks:
            timeline_store.insert(conn, block)

    fake_llm.set_default(delta_mod.STAGE, _payload(entities=[], assertions=[]))
    result = delta_mod.ensure_active_window(
        _cfg(),
        session_id="capped-window",
        start_time=start,
        end_time=start + timedelta(minutes=121),
    )

    assert result.written and result.applied
    sent = "".join(block["text"] for block in fake_llm.calls[0]["messages"][1]["content"])
    assert "overlap-000" not in sent
    assert sent.index("overlap-001") < sent.index("overlap-120")
    with fts.cursor() as conn:
        claimed = deltas_store.evidence_ids_for_delta(conn, result.delta_id)
    assert claimed == [block.id for block in blocks[1:]]


def test_llm_failure_leaves_boundary_block_unclaimed_for_retry(ac_root, fake_llm) -> None:
    start = datetime(2026, 7, 2, 15, 0).astimezone()
    block = _block(start, ["[Editor] retryable boundary evidence"], ["Editor"])
    with fts.cursor() as conn:
        timeline_store.insert(conn, block)

    def fail_llm(*_args) -> None:
        raise RuntimeError("temporary model outage")

    failed = delta_mod.ensure_active_window(
        _cfg(),
        session_id="llm-retry",
        start_time=start,
        end_time=start + timedelta(minutes=1),
        llm_call=fail_llm,
    )
    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_delta_evidence_claims").fetchone()[0] == 0

    fake_llm.set_default(delta_mod.STAGE, _payload(entities=[], assertions=[]))
    retried = delta_mod.ensure_active_window(
        _cfg(),
        session_id="llm-retry",
        start_time=start,
        end_time=start + timedelta(minutes=1),
    )

    assert failed.skipped_reason == "llm_failed" and not failed.written
    assert retried.written and retried.applied
    with fts.cursor() as conn:
        assert deltas_store.evidence_ids_for_delta(conn, retried.delta_id) == [block.id]


def test_apply_failure_keeps_claim_and_retry_does_not_reextract(
    ac_root,
    fake_llm,
    monkeypatch,
) -> None:
    from persome.writer import delta_apply

    minute = datetime(2026, 7, 2, 16, 0).astimezone()
    boundary_text = "[Editor] apply-failure boundary evidence"
    following_text = "[Browser] later independent evidence"
    boundary_block = _block(minute, [boundary_text], ["Editor"])
    following_block = _block(
        minute + timedelta(minutes=1),
        [following_text],
        ["Browser"],
    )
    with fts.cursor() as conn:
        timeline_store.insert(conn, boundary_block)
        timeline_store.insert(conn, following_block)

    original_apply = delta_apply.apply_delta
    apply_calls = 0

    def flaky_apply(conn, cfg, payload):
        nonlocal apply_calls
        apply_calls += 1
        if apply_calls == 1:
            raise RuntimeError("apply interrupted")
        return original_apply(conn, cfg, payload)

    monkeypatch.setattr(delta_apply, "apply_delta", flaky_apply)
    fake_llm.set_default(delta_mod.STAGE, _payload(entities=[], assertions=[]))

    failed = delta_mod.ensure_active_window(
        _cfg(),
        session_id="apply-retry",
        start_time=minute,
        end_time=minute + timedelta(minutes=1),
    )
    competing = delta_mod.ensure_active_window(
        _cfg(),
        session_id="competing-replay",
        start_time=minute,
        end_time=minute + timedelta(minutes=1),
    )
    adjacent = delta_mod.ensure_active_window(
        _cfg(),
        session_id="adjacent-session",
        start_time=minute + timedelta(minutes=1),
        end_time=minute + timedelta(minutes=2),
    )
    retried = delta_mod.ensure_active_window(
        _cfg(),
        session_id="apply-retry",
        start_time=minute,
        end_time=minute + timedelta(minutes=1),
    )

    assert failed.written and not failed.applied
    assert competing.skipped_reason == "no_blocks" and not competing.written
    assert adjacent.written and adjacent.applied
    assert retried.applied and retried.skipped_reason == "resumed_apply"
    assert retried.delta_id == failed.delta_id
    assert len(fake_llm.calls) == 2
    adjacent_sent = "".join(block["text"] for block in fake_llm.calls[1]["messages"][1]["content"])
    assert boundary_text not in adjacent_sent and following_text in adjacent_sent
    with fts.cursor() as conn:
        assert deltas_store.evidence_ids_for_delta(conn, failed.delta_id) == [boundary_block.id]
        assert deltas_store.evidence_ids_for_delta(conn, adjacent.delta_id) == [following_block.id]
        statuses = conn.execute(
            "SELECT apply_status FROM memory_deltas WHERE id IN (?, ?) ORDER BY id",
            (failed.delta_id, adjacent.delta_id),
        ).fetchall()
    assert [row["apply_status"] for row in statuses] == ["applied", "applied"]


def test_block_claim_collision_rolls_back_new_delta_row(ac_root) -> None:
    start = datetime(2026, 7, 2, 17, 0).astimezone()
    block = _block(start, ["[Editor] one block"], ["Editor"])
    with fts.cursor() as conn:
        timeline_store.insert(conn, block)
        first_id = deltas_store.insert(
            conn,
            session_id="first-claimant",
            payload={},
            window_start=start,
            window_end=start + timedelta(minutes=1),
            evidence_ids=[block.id],
        )
        with pytest.raises(sqlite3.IntegrityError):
            deltas_store.insert(
                conn,
                session_id="second-claimant",
                payload={},
                window_start=start,
                window_end=start + timedelta(minutes=1),
                evidence_ids=[block.id],
            )
        rows = conn.execute("SELECT id, session_id FROM memory_deltas ORDER BY id").fetchall()
        claims = conn.execute(
            "SELECT delta_id, evidence_id FROM memory_delta_evidence_claims"
        ).fetchall()

    assert [(row["id"], row["session_id"]) for row in rows] == [(first_id, "first-claimant")]
    assert [(row["delta_id"], row["evidence_id"]) for row in claims] == [(first_id, block.id)]


def test_claim_collision_rolls_back_owner_alias_side_effects(
    ac_root,
    fake_llm,
    monkeypatch,
) -> None:
    start = datetime(2026, 7, 2, 17, 30).astimezone()
    end = start + timedelta(minutes=1)
    quote = "Opened my own account Casey-Race"
    with fts.cursor() as conn:
        timeline_store.insert(conn, _block(start, [f"[Chrome] {quote}"], ["Chrome"]))

    fake_llm.set_default(
        delta_mod.STAGE,
        _payload(
            owner_alias_candidates=[
                {
                    "alias": "Casey-Race",
                    "source_kind": "owned_account",
                    "quote": quote,
                    "confidence": 0.94,
                }
            ],
            entities=[],
            assertions=[],
        ),
    )

    def collide(conn, **_kwargs):  # type: ignore[no-untyped-def]
        assert conn.in_transaction
        raise sqlite3.IntegrityError("simulated evidence-claim collision")

    monkeypatch.setattr(deltas_store, "insert", collide)
    result = delta_mod.run_after_session(
        _cfg(),
        session_id="owner-race",
        start_time=start,
        end_time=end,
    )

    assert result.skipped_reason == "persist_failed" and not result.written
    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM owner_aliases").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM owner_alias_evidence").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_deltas").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_delta_evidence_claims").fetchone()[0] == 0


def test_evidence_insert_preserves_caller_transaction_rollback(ac_root) -> None:
    with fts.cursor() as conn:
        deltas_store.ensure_schema(conn)
        conn.execute("CREATE TABLE transaction_marker (value TEXT NOT NULL)")
        conn.execute("BEGIN")
        conn.execute("INSERT INTO transaction_marker (value) VALUES ('rollback-me')")
        deltas_store.insert(
            conn,
            session_id="outer-transaction",
            payload={},
            evidence_ids=["capture:transaction-proof"],
        )
        conn.rollback()

        assert conn.execute("SELECT COUNT(*) FROM transaction_marker").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_deltas").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_delta_evidence_claims").fetchone()[0] == 0


@pytest.mark.parametrize("invalid_evidence_ids", [[""], ["   "], [None]])
def test_evidence_claim_ids_must_be_nonempty_strings(ac_root, invalid_evidence_ids) -> None:
    with fts.cursor() as conn:
        with pytest.raises(ValueError, match="non-empty strings"):
            deltas_store.insert(
                conn,
                session_id="invalid-claim",
                payload={},
                evidence_ids=invalid_evidence_ids,
            )
        assert conn.execute("SELECT COUNT(*) FROM memory_deltas").fetchone()[0] == 0


def test_ensure_schema_adds_claim_table_without_rewriting_legacy_deltas(ac_root) -> None:
    with fts.cursor() as conn:
        conn.execute("DROP TABLE IF EXISTS memory_delta_evidence_claims")
        conn.execute("DROP TABLE IF EXISTS memory_deltas")
        conn.execute(
            """
            CREATE TABLE memory_deltas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'shadow',
                payload TEXT NOT NULL DEFAULT '{}',
                dropped INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "INSERT INTO memory_deltas (session_id, created_at) VALUES (?, ?)",
            ("legacy-session", datetime(2026, 7, 1).astimezone().isoformat()),
        )

        deltas_store.ensure_schema(conn)

        columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(memory_deltas)").fetchall()
        }
        claim_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name='memory_delta_evidence_claims'"
        ).fetchone()
        legacy = conn.execute(
            "SELECT session_id, apply_status, window_start, window_end, is_final FROM memory_deltas"
        ).fetchone()

    assert {"apply_status", "window_start", "window_end", "is_final"} <= columns
    assert claim_table is not None
    assert tuple(legacy) == ("legacy-session", "unknown", "", "", 1)


def test_stats_aggregates_latest_per_session(ac_root, fake_llm) -> None:
    start, end = _seed_session_blocks([SESSION_ENTRY])
    fake_llm.set_default(delta_mod.STAGE, _payload())
    result = delta_mod.run_after_session(_cfg(), session_id="s9", start_time=start, end_time=end)
    with fts.cursor() as conn:
        first = deltas_store.latest_for_session(conn, "s9")
        assert first is not None
        # Simulate a historical second attempt without a block receipt. Stats
        # intentionally count only the latest row for an exact session window.
        deltas_store.insert(
            conn,
            session_id="s9",
            payload=json.loads(first["payload"]),
            window_start=start,
            window_end=end,
        )
        agg = deltas_store.stats(conn)
    assert result.written
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
