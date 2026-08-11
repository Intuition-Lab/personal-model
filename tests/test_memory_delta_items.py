"""Item-level receipt ledger for deterministic memory-delta effects."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from persome.store import fts
from persome.store import memory_delta_items as items_store


def _payload() -> dict:
    return {
        "entities": [{"ref": "Alice", "kind": "person", "quote": "Met Alice", "confidence": 0.9}],
        "assertions": [
            {
                "subject": {"ref": "Alice"},
                "text": "Alice leads launch",
                "quote": "Alice leads launch",
                "confidence": 0.9,
            }
        ],
        "relations": [
            {
                "src": {"ref": "self"},
                "dst": {"ref": "Alice"},
                "predicate": "knows",
                "quote": "Met Alice",
                "confidence": 0.9,
            }
        ],
        "events": [
            {
                "title": "Launch review",
                "participants": [{"ref": "self"}, {"ref": "Alice"}],
                "quote": "Launch review finished",
                "confidence": 0.9,
            }
        ],
    }


def test_seed_is_stable_and_rejects_parent_mutation(ac_root) -> None:
    with fts.cursor() as conn:
        first = items_store.seed(conn, delta_id=7, payload=_payload())
        retry = items_store.seed(conn, delta_id=7, payload=_payload())
        mutated = _payload()
        mutated["entities"][0]["quote"] = "Different evidence"
        with pytest.raises(items_store.DeltaItemCollisionError):
            items_store.seed(conn, delta_id=7, payload=mutated)

    assert [(item.kind, item.key) for item in first] == [(item.kind, item.key) for item in retry]
    assert [item.ordinal for item in first] == [0, 1, 2, 3]


def test_duplicate_identity_with_divergent_payload_fails_closed(ac_root) -> None:
    payload = _payload()
    payload["entities"].append(
        {"ref": "Alice", "kind": "person", "quote": "Other quote", "confidence": 0.8}
    )
    with (
        fts.cursor() as conn,
        pytest.raises(items_store.DeltaItemCollisionError, match="divergent candidates"),
    ):
        items_store.seed(conn, delta_id=8, payload=payload)


def test_exact_duplicate_candidate_collapses_to_one_item(ac_root) -> None:
    payload = _payload()
    payload["entities"].append(dict(payload["entities"][0]))
    with fts.cursor() as conn:
        items = items_store.seed(conn, delta_id=81, payload=payload)
    assert [item.kind for item in items].count("entity") == 1


def test_claims_are_ordered_and_stale_token_cannot_complete(ac_root) -> None:
    start = datetime(2026, 8, 11, 2, 0, tzinfo=UTC)
    with fts.cursor() as conn:
        items_store.seed(conn, delta_id=9, payload=_payload())
        first = items_store.claim_next(
            conn,
            delta_id=9,
            now=start,
            lease_seconds=10,
        )
        assert first is not None and first.kind == "entity"
        assert (
            items_store.claim_next(
                conn,
                delta_id=9,
                now=start + timedelta(seconds=5),
                lease_seconds=10,
            )
            is None
        )
        reclaimed = items_store.claim_next(
            conn,
            delta_id=9,
            now=start + timedelta(seconds=11),
            lease_seconds=10,
        )
        assert reclaimed is not None and reclaimed.key == first.key
        assert reclaimed.token != first.token
        assert (
            items_store.mark_applied(
                conn,
                delta_id=9,
                item=first,
                geometry_changed=True,
            )
            is False
        )
        assert items_store.mark_applied(
            conn,
            delta_id=9,
            item=reclaimed,
            geometry_changed=False,
        )
        assert items_store.geometry_changed_for_delta(conn, delta_id=9) is False
        second = items_store.claim_next(conn, delta_id=9)
        assert second is not None and second.kind == "assertion"


def test_failed_item_retries_before_later_items(ac_root) -> None:
    with fts.cursor() as conn:
        items_store.seed(conn, delta_id=10, payload=_payload())
        first = items_store.claim_next(conn, delta_id=10)
        assert first is not None
        assert items_store.mark_failed(conn, delta_id=10, item=first, error="boom")
        retry = items_store.claim_next(conn, delta_id=10)
        assert retry is not None and retry.key == first.key and retry.attempts == 2
        assert items_store.mark_applied(conn, delta_id=10, item=retry)
        counts = items_store.state_counts(conn, delta_id=10)

    assert counts[items_store.STATE_APPLIED] == 1
    assert counts[items_store.STATE_PENDING] == 3


def test_geometry_receipt_aggregates_true_and_preserves_legacy_unknown(ac_root) -> None:
    with fts.cursor() as conn:
        items_store.seed(conn, delta_id=11, payload=_payload())
        first = items_store.claim_next(conn, delta_id=11)
        assert first is not None
        assert items_store.mark_applied(
            conn,
            delta_id=11,
            item=first,
            geometry_changed=True,
        )
        assert items_store.geometry_changed_for_delta(conn, delta_id=11) is True

        items_store.seed(conn, delta_id=12, payload=_payload())
        legacy = items_store.claim_next(conn, delta_id=12)
        assert legacy is not None
        assert items_store.mark_applied(conn, delta_id=12, item=legacy)
        assert items_store.geometry_changed_for_delta(conn, delta_id=12) is None


def test_geometry_receipt_migration_keeps_old_applied_row_unknown(ac_root) -> None:
    with fts.cursor() as conn:
        conn.execute(
            """
            CREATE TABLE memory_delta_items (
                delta_id INTEGER NOT NULL,
                item_kind TEXT NOT NULL,
                item_key TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                payload_hash TEXT NOT NULL,
                payload TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                claim_token TEXT NOT NULL DEFAULT '',
                lease_until TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                effect_kind TEXT NOT NULL DEFAULT '',
                effect_id TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (delta_id, item_kind, item_key)
            )
            """
        )
        conn.execute(
            "INSERT INTO memory_delta_items"
            " (delta_id, item_kind, item_key, ordinal, payload_hash, payload, state,"
            " created_at, updated_at) VALUES (13, 'event', 'old', 0, 'hash', '{}',"
            " 'applied', '2026-08-11', '2026-08-11')"
        )

        items_store.ensure_schema(conn)

        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memory_delta_items)")}
        assert "geometry_changed" in columns
        assert items_store.geometry_changed_for_delta(conn, delta_id=13) is None
