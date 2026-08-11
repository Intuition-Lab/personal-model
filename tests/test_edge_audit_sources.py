"""Edge audit follows Activity source columns instead of assuming intents."""

from __future__ import annotations

from persome.evomem import edge_audit
from persome.store import entries as entries_store
from persome.store import event_occurrences as occurrences
from persome.store import fts
from persome.store import relation_edges as edges


def test_entry_activity_edge_audits_against_durable_entry(ac_root) -> None:
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
            content="Reviewed the Persome runtime architecture.",
            tags=["work"],
        )
        edge_id = edges.add_edge(
            conn,
            src_identity="self",
            dst_identity=f"event:entry:{entry_id}",
            predicate="participates_in",
            src_kind="self",
            dst_kind="event",
            provenance="inferred",
            confidence=0.9,
            quote="Reviewed the Persome runtime architecture.",
            source_kind="entry",
            source_id=entry_id,
            source_receipt=f"⟨{entry_id}:event-2026-07-10.md⟩",
        )
        row = conn.execute("SELECT * FROM relation_edges WHERE edge_id=?", (edge_id,)).fetchone()
        verdict = edge_audit.audit_edge(conn, row)
    assert verdict.verdict == "valid"
    assert verdict.checks["source_exists"] is True
    assert verdict.checks["quote_traceable"] is True


def test_missing_entry_source_is_structural_hallucination(ac_root) -> None:
    with fts.cursor() as conn:
        edge_id = edges.add_edge(
            conn,
            src_identity="self",
            dst_identity="event:entry:missing",
            predicate="participates_in",
            src_kind="self",
            dst_kind="event",
            provenance="inferred",
            confidence=0.9,
            quote="Missing synthetic evidence.",
            source_kind="entry",
            source_id="missing",
            source_receipt="⟨missing:event-2026-07-10.md⟩",
        )
        row = conn.execute("SELECT * FROM relation_edges WHERE edge_id=?", (edge_id,)).fetchone()
        verdict = edge_audit.audit_edge(conn, row)
    assert verdict.verdict == "structural_hallucination"
    assert verdict.checks["source_exists"] is False


def test_occurrence_activity_edge_audits_against_occurrence_receipt(ac_root) -> None:
    from datetime import UTC, datetime, timedelta

    with fts.cursor() as conn:
        start = datetime(2026, 8, 11, 2, 0, tzinfo=UTC)
        occurrence, _ = occurrences.upsert(
            conn,
            session_id="session-1",
            window_start=start,
            window_end=start + timedelta(minutes=5),
            item_key="review-1",
            title="Weekly review",
            participants=["self"],
            quote="Reviewed the Persome runtime architecture.",
            confidence=0.9,
        )
        edge_id = edges.add_edge(
            conn,
            src_identity="self",
            dst_identity=occurrence.endpoint,
            predicate="participates_in",
            src_kind="self",
            dst_kind="event",
            provenance="inferred",
            confidence=0.9,
            quote="Reviewed the Persome runtime architecture.",
            source_kind="occurrence",
            source_id=occurrence.occurrence_id,
            source_receipt=occurrence.source_receipt,
        )
        row = conn.execute("SELECT * FROM relation_edges WHERE edge_id=?", (edge_id,)).fetchone()
        verdict = edge_audit.audit_edge(conn, row)

    assert verdict.verdict == "valid"
    assert verdict.checks["source_exists"] is True
    assert verdict.checks["quote_traceable"] is True


def test_occurrence_activity_edge_rejects_mismatched_receipt(ac_root) -> None:
    from datetime import UTC, datetime, timedelta

    with fts.cursor() as conn:
        start = datetime(2026, 8, 11, 2, 0, tzinfo=UTC)
        occurrence, _ = occurrences.upsert(
            conn,
            session_id="session-1",
            window_start=start,
            window_end=start + timedelta(minutes=5),
            item_key="review-1",
            title="Weekly review",
            participants=["self"],
            quote="Reviewed the Persome runtime architecture.",
            confidence=0.9,
        )
        edge_id = edges.add_edge(
            conn,
            src_identity="self",
            dst_identity=occurrence.endpoint,
            predicate="participates_in",
            src_kind="self",
            dst_kind="event",
            provenance="inferred",
            confidence=0.9,
            quote="Reviewed the Persome runtime architecture.",
            source_kind="occurrence",
            source_id=occurrence.occurrence_id,
            source_receipt="⟨00000000000000000000:event_occurrences⟩",
        )
        row = conn.execute("SELECT * FROM relation_edges WHERE edge_id=?", (edge_id,)).fetchone()
        verdict = edge_audit.audit_edge(conn, row)

    assert verdict.verdict == "structural_hallucination"
    assert verdict.checks["source_exists"] is False


def test_legacy_chinese_synthetic_quotes_still_audit_as_synthetic() -> None:
    # Pre-0.3.0 extractors wrote the same honest constructions in Chinese.
    legacy_interaction = "\u4e0e Alice \u7684\u4ea4\u4e92\u8bb0\u5f55"
    legacy_cooccur = "Alice \u4e0e Bob \u66fe\u5728\u540c\u4e00\u573a\u666f\u51fa\u73b0"
    legacy_completed = "\u5df2\u5b8c\u6210\u7684 review \u4e8b\u9879"
    legacy_project = "core \u9879\u76ee\u8bb0\u5fc6 3 \u6761\u6301\u4e45\u4e8b\u5b9e"
    for quote in (legacy_interaction, legacy_cooccur, legacy_completed, legacy_project):
        assert edge_audit._is_synthetic(quote), quote
    assert edge_audit._is_cooccur_quote(legacy_cooccur)
    assert edge_audit._is_cooccur_quote("Alice and Bob appeared in the same context")
    assert not edge_audit._is_cooccur_quote(legacy_interaction)
    assert not edge_audit._is_synthetic("An ordinary excerpted sentence.")


def test_legacy_chinese_quote_edges_are_not_condemned(ac_root) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from persome.evomem.engine import EvoMemory
    from persome.evomem.person_graph import PersonEvent, PersonGraph

    class _StaticSource:
        def __init__(self, events):
            self._events = events

        def events(self):
            return list(self._events)

    ts = datetime(2026, 6, 20, 10, 0, tzinfo=UTC)
    PersonGraph(
        EvoMemory(),
        cfg=SimpleNamespace(person_graph_enabled=True),
        name_source=_StaticSource(
            [
                PersonEvent(name="Alice", summary="weekly sync", occurred_at=ts, confidence=0.9),
                PersonEvent(name="Bob", summary="weekly sync", occurred_at=ts, confidence=0.9),
            ]
        ),
    ).ingest()

    legacy_interaction = "\u4e0e Alice \u7684\u4ea4\u4e92\u8bb0\u5f55"
    legacy_cooccur = "Alice \u4e0e Bob \u66fe\u5728\u540c\u4e00\u573a\u666f\u51fa\u73b0"
    with fts.cursor() as conn:
        knows_id = edges.add_edge(
            conn,
            src_identity="self",
            dst_identity="Alice",
            predicate="knows",
            src_kind="self",
            dst_kind="person",
            provenance="inferred",
            confidence=0.9,
            quote=legacy_interaction,
        )
        cooccur_id = edges.add_edge(
            conn,
            src_identity="Alice",
            dst_identity="Bob",
            predicate="knows",
            src_kind="person",
            dst_kind="person",
            provenance="inferred",
            confidence=0.6,
            quote=legacy_cooccur,
        )
        rows = {
            edge_id: conn.execute(
                "SELECT * FROM relation_edges WHERE edge_id=?", (edge_id,)
            ).fetchone()
            for edge_id in (knows_id, cooccur_id)
        }
        knows_verdict = edge_audit.audit_edge(conn, rows[knows_id])
        cooccur_verdict = edge_audit.audit_edge(conn, rows[cooccur_id])

    assert knows_verdict.verdict == "valid"
    assert "synthetic_quote" in knows_verdict.notes
    assert cooccur_verdict.verdict == "valid"
    assert "synthetic_quote:cooccur_rederived" in cooccur_verdict.notes
