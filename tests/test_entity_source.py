"""Neutral entity/person source derived from durable classifier memory."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from types import SimpleNamespace

from persome.evomem import owner_identity
from persome.evomem.engine import EvoMemory
from persome.evomem.models import MemoryLayer, MemoryNode
from persome.evomem.person_graph import PersonGraph
from persome.evomem.store import NodeStore
from persome.model.entity_source import EntitySource, MemoryPersonNameSource
from persome.store import entries as entries_store
from persome.store import fts
from persome.store import owner_aliases as owner_alias_store


def _seed_person_memory() -> str:
    node_id = "point-person-alex"
    NodeStore().save(
        MemoryNode(
            node_id=node_id,
            content="Alex reviews architecture decisions with the user.",
            layer=MemoryLayer.L2_FACT,
            file_name="person-alex.md",
            confidence="high",
            occurred_at="2026-07-10T08:00:00+00:00",
            memory_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        )
    )
    return node_id


def test_entity_source_reads_person_fact_and_event_receipts(ac_root) -> None:
    node_id = _seed_person_memory()
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name="event-2026-07-10.md",
            description="Synthetic activity",
            tags=["event"],
        )
        entry_id = entries_store.append_entry(
            conn,
            name="event-2026-07-10.md",
            content="Reviewed the runtime architecture with Alex.",
            tags=["work"],
        )
        events = EntitySource(conn).events()

    assert {event.source_kind for event in events} == {"point", "entry"}
    assert {event.entity_id for event in events} == {"alex"}
    receipts = {event.source_receipt for event in events}
    assert f"⟨{node_id}:person-alex.md⟩" in receipts
    assert f"⟨{entry_id}:event-2026-07-10.md⟩" in receipts


def test_entity_source_processes_points_before_newer_entries_without_starving_either(
    ac_root,
) -> None:
    node_id = _seed_person_memory()
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name="event-2026-07-11.md",
            description="Synthetic newer activity",
            tags=["event"],
        )
        entry_id = entries_store.append_entry(
            conn,
            name="event-2026-07-11.md",
            content="Alex reviewed the newer runtime plan.",
            tags=["work"],
        )
        events = EntitySource(conn, limit=1).events()

    assert [(event.source_kind, event.source_id) for event in events] == [
        ("point", node_id),
        ("entry", entry_id),
    ]


def test_entity_source_applies_limit_after_entry_mention_expansion(ac_root) -> None:
    store = NodeStore()
    for name in ("alex", "blair"):
        store.save(
            MemoryNode(
                node_id=f"point-person-{name}",
                content=f"{name.title()} is a collaborator.",
                layer=MemoryLayer.L2_FACT,
                file_name=f"person-{name}.md",
                memory_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
            )
        )
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name="event-expanded.md",
            description="Synthetic multi-person activity",
            tags=["event"],
        )
        for suffix in ("one", "two"):
            entries_store.append_entry(
                conn,
                name="event-expanded.md",
                content=f"Alex and Blair reviewed plan {suffix}.",
                tags=["work"],
            )
        events = EntitySource(conn, limit=2).events()

    assert sum(event.source_kind == "point" for event in events) == 2
    assert sum(event.source_kind == "entry" for event in events) == 2


def test_entity_source_excludes_derived_person_rows_before_point_limit(ac_root) -> None:
    store = NodeStore()
    for index in range(2):
        store.save(
            MemoryNode(
                node_id=f"derived-event-{index}",
                content=f"Derived event {index}",
                layer=MemoryLayer.L5_KNOWLEDGE,
                file_name=f"person-derived-{index}.md",
                tags="person-event",
                memory_at=datetime(2026, 7, 12, index, 0, tzinfo=UTC),
            )
        )
    for index in range(2):
        store.save(
            MemoryNode(
                node_id=f"point-person-real-{index}",
                content=f"Real person fact {index}",
                layer=MemoryLayer.L2_FACT,
                file_name=f"person-real-{index}.md",
                tags="fact",
                memory_at=datetime(2026, 7, 10, index, 0, tzinfo=UTC),
            )
        )

    with fts.cursor() as conn:
        events = EntitySource(conn, limit=2).events()

    assert [event.source_id for event in events] == [
        "point-person-real-1",
        "point-person-real-0",
    ]


def test_memory_person_source_implements_person_graph_seam(ac_root) -> None:
    _seed_person_memory()
    events = MemoryPersonNameSource().events()
    assert [event.name for event in events] == ["alex"]
    assert events[0].summary == "Alex reviews architecture decisions with the user."
    assert events[0].confidence == 0.95
    assert events[0].source_kind == "point"
    assert events[0].source_receipt == "⟨point-person-alex:person-alex.md⟩"


def test_person_points_use_explicit_entity_body_as_display_identity(ac_root) -> None:
    store = NodeStore()
    store.save(
        MemoryNode(
            node_id="derived-person-alex-roster",
            content="alex-chen",
            layer=MemoryLayer.L5_KNOWLEDGE,
            file_name="person-alex-chen.md",
            tags="person-entity",
            memory_at=datetime(2026, 7, 9, 8, 0, tzinfo=UTC),
            gmt_created=datetime(2026, 7, 9, 8, 0, tzinfo=UTC),
        )
    )
    store.save(
        MemoryNode(
            node_id="point-person-alex-identity",
            content="Alex Chen",
            layer=MemoryLayer.L5_KNOWLEDGE,
            file_name="person-alex-chen.md",
            tags="entity",
            memory_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
            gmt_created=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        )
    )
    store.save(
        MemoryNode(
            node_id="point-person-alex-fact",
            content="Alex reviews architecture decisions.",
            layer=MemoryLayer.L2_FACT,
            file_name="person-alex-chen.md",
            tags="fact",
            memory_at=datetime(2026, 7, 11, 8, 0, tzinfo=UTC),
            gmt_created=datetime(2026, 7, 11, 8, 0, tzinfo=UTC),
        )
    )

    with fts.cursor() as conn:
        direct = EntitySource(conn, limit=1).events()

    assert [event.display_name for event in direct] == ["Alex Chen"]
    assert [event.source_id for event in direct] == ["point-person-alex-fact"]


def test_point_backed_person_source_adopts_identity_once_without_event_copy(ac_root) -> None:
    store = NodeStore()
    store.save(
        MemoryNode(
            node_id="point-person-alex-identity",
            content="Alex Chen",
            layer=MemoryLayer.L5_KNOWLEDGE,
            file_name="person-alex-chen.md",
            tags="entity",
            memory_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        )
    )
    store.save(
        MemoryNode(
            node_id="point-person-alex-fact",
            content="Alex reviews architecture decisions.",
            layer=MemoryLayer.L2_FACT,
            file_name="person-alex-chen.md",
            tags="fact",
            memory_at=datetime(2026, 7, 11, 8, 0, tzinfo=UTC),
        )
    )
    graph = PersonGraph(
        EvoMemory(),
        cfg=SimpleNamespace(person_graph_enabled=True),
        name_source=MemoryPersonNameSource(),
    )

    assert graph.ingest() == ["Alex Chen", "Alex Chen"]
    first = graph.list_persons()[0]
    assert first.canonical == "Alex Chen"
    assert first.sightings == 1
    assert graph.person_timeline("Alex Chen") == []

    assert graph.ingest() == ["Alex Chen"]
    assert graph.list_persons()[0].node_id == first.node_id
    with fts.cursor() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM evo_nodes WHERE tags = 'person-entity'").fetchone()[
                0
            ]
            == 1
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM evo_nodes WHERE tags = 'person-event'").fetchone()[0]
            == 0
        )


def test_person_source_merges_identity_arriving_after_slug_roster(ac_root) -> None:
    store = NodeStore()
    store.save(
        MemoryNode(
            node_id="point-person-alex-fact-first",
            content="Alex reviews architecture decisions.",
            layer=MemoryLayer.L2_FACT,
            file_name="person-alex-chen.md",
            tags="fact",
            memory_at=datetime(2026, 7, 11, 8, 0, tzinfo=UTC),
            gmt_created=datetime(2026, 7, 11, 8, 0, tzinfo=UTC),
        )
    )
    graph = PersonGraph(
        EvoMemory(),
        cfg=SimpleNamespace(person_graph_enabled=True),
        name_source=MemoryPersonNameSource(),
    )

    assert graph.ingest() == ["alex-chen"]
    slug_roster_id = graph.list_persons()[0].node_id

    # Inserted later, but deliberately carries an older evidence timestamp so
    # the fact is emitted first on the next batch. Either ordering must merge.
    store.save(
        MemoryNode(
            node_id="point-person-alex-identity-late",
            content="Alex Chen",
            layer=MemoryLayer.L5_KNOWLEDGE,
            file_name="person-alex-chen.md",
            tags="entity",
            memory_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
            gmt_created=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        )
    )

    assert graph.ingest() == ["Alex Chen", "Alex Chen"]
    people = graph.list_persons()
    assert [(person.canonical, person.sightings) for person in people] == [("Alex Chen", 1)]
    with fts.cursor() as conn:
        active = conn.execute(
            "SELECT node_id, supersedes FROM evo_nodes "
            "WHERE is_latest = 1 AND status = 'active' AND tags = 'person-entity'"
        ).fetchall()
        assert len(active) == 1
        assert set(json.loads(active[0]["supersedes"])) == {
            slug_roster_id,
            "point-person-alex-identity-late",
        }
        assert (
            conn.execute("SELECT COUNT(*) FROM evo_nodes WHERE tags = 'person-event'").fetchone()[0]
            == 0
        )


def test_event_mentions_keep_only_person_specific_lines(ac_root) -> None:
    NodeStore().save(
        MemoryNode(
            node_id="point-person-kevin",
            content="Kevin is a launch collaborator.",
            layer=MemoryLayer.L2_FACT,
            file_name="person-kevin.md",
            confidence="high",
        )
    )
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name="event-2026-07-10.md",
            description="Synthetic mixed activity",
            tags=["event"],
        )
        entries_store.append_entry(
            conn,
            name="event-2026-07-10.md",
            content=(
                "The user adjusted a private investment portfolio.\n"
                "- [Feishu] Kevin reviewed the launch checklist.\n"
                "- [Chrome] The user opened a banking dashboard."
            ),
            tags=["work"],
        )
        mention = next(
            event for event in EntitySource(conn).events() if event.source_kind == "entry"
        )

    assert mention.summary == "- [Feishu] Kevin reviewed the launch checklist."
    assert "investment" not in mention.summary


def test_entity_source_excludes_heuristic_event_mentions(ac_root) -> None:
    _seed_person_memory()
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name="event-2026-07-10.md",
            description="Synthetic reducer fallback",
            tags=["event"],
        )
        entries_store.append_entry(
            conn,
            name="event-2026-07-10.md",
            content="Worked in a window with Alex, involving —",
            tags=["session", "heuristic"],
        )
        events = EntitySource(conn).events()

    assert all(event.source_kind != "entry" for event in events)


def test_memory_person_source_filters_configured_owner_alias(ac_root) -> None:
    node_id = "point-person-owner"
    NodeStore().save(
        MemoryNode(
            node_id=node_id,
            content="Casey-Example opened a pull request.",
            layer=MemoryLayer.L2_FACT,
            file_name="person-casey-example.md",
            confidence="high",
        )
    )
    cfg = SimpleNamespace(memory_delta=SimpleNamespace(owner_aliases=["Casey-Example"]))

    assert MemoryPersonNameSource(cfg=cfg).events() == []


def test_memory_person_source_filters_learned_owner_alias(ac_root) -> None:
    NodeStore().save(
        MemoryNode(
            node_id="point-person-learned-owner",
            content="Casey-Example opened a pull request.",
            layer=MemoryLayer.L2_FACT,
            file_name="person-casey-example.md",
            confidence="high",
        )
    )
    with fts.cursor() as conn:
        for session_id in ("owner-1", "owner-2"):
            owner_identity.record_candidate(
                conn,
                alias="Casey-Example",
                session_id=session_id,
                source_kind=owner_alias_store.SOURCE_OWNED_ACCOUNT,
                quote="own account Casey-Example",
                confidence=0.9,
            )
    cfg = SimpleNamespace(memory_delta=SimpleNamespace(owner_aliases=[]))

    assert MemoryPersonNameSource(cfg=cfg).events() == []


def test_entity_source_limit_uses_actual_instant_across_offsets(ac_root) -> None:
    for node_id, name, timestamp in (
        ("point-person-older", "older", "2025-11-02T10:00:00+08:00"),
        ("point-person-newer", "newer", "2025-11-02T03:00:00+00:00"),
    ):
        NodeStore().save(
            MemoryNode(
                node_id=node_id,
                content=f"{name} person fact",
                layer=MemoryLayer.L2_FACT,
                file_name=f"person-{name}.md",
                confidence="high",
                occurred_at=timestamp,
                memory_at=datetime.fromisoformat(timestamp),
            )
        )

    with fts.cursor() as conn:
        events = EntitySource(conn, limit=1).events()

    assert [(event.entity_id, event.summary) for event in events] == [
        ("newer", "newer person fact")
    ]


def test_entity_source_compares_legacy_naive_entry_to_aware_point(ac_root, monkeypatch) -> None:
    original_timezone = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        NodeStore().save(
            MemoryNode(
                node_id="point-person-alex-newer",
                content="newer aware point",
                layer=MemoryLayer.L2_FACT,
                file_name="person-alex.md",
                confidence="high",
                occurred_at="2026-07-11T03:00:00+00:00",
                memory_at=datetime.fromisoformat("2026-07-11T03:00:00+00:00"),
            )
        )
        with fts.cursor() as conn:
            entries_store.create_file(
                conn,
                name="event-legacy.md",
                description="legacy local event",
                tags=["event"],
            )
            entry_id = entries_store.append_entry(
                conn,
                name="event-legacy.md",
                content="older local event with Alex",
                tags=["work"],
            )
            conn.execute(
                "UPDATE entries SET timestamp='2026-07-11T10:00' WHERE id=?",
                (entry_id,),
            )
            events = EntitySource(conn, limit=1).events()

            assert [(event.source_kind, event.summary) for event in events] == [
                ("point", "newer aware point"),
                ("entry", "older local event with Alex"),
            ]
    finally:
        if original_timezone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_timezone
        time.tzset()
