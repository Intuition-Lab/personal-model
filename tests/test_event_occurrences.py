"""Stable identity and conservative grouping for durable event occurrences."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from persome.store import event_occurrences as occurrences
from persome.store import fts


def test_occurrence_identity_canonicalizes_window_offsets() -> None:
    local = timezone(timedelta(hours=8))
    assert occurrences.make_occurrence_id(
        session_id="session-1",
        window_start=datetime(2026, 8, 11, 10, 0, tzinfo=local),
        window_end=datetime(2026, 8, 11, 10, 5, tzinfo=local),
        item_key="item-1",
    ) == occurrences.make_occurrence_id(
        session_id="session-1",
        window_start=datetime(2026, 8, 11, 2, 0, tzinfo=UTC),
        window_end=datetime(2026, 8, 11, 2, 5, tzinfo=UTC),
        item_key="item-1",
    )


def test_retry_and_series_identity_contract(ac_root) -> None:
    start = datetime(2026, 8, 11, 2, 0, tzinfo=UTC)
    end = start + timedelta(minutes=5)
    item_key = occurrences.make_item_key(
        title=" Weekly   Review ",
        participants=["Alice", "Bob"],
        quote="Reviewed the launch plan.",
    )

    with fts.cursor() as conn:
        first, first_created = occurrences.upsert(
            conn,
            session_id="session-1",
            window_start=start,
            window_end=end,
            item_key=item_key,
            title="Weekly Review",
            participants=["Bob", "Alice", "alice"],
            quote="Reviewed the launch plan.",
            confidence=0.9,
        )
        retry, retry_created = occurrences.upsert(
            conn,
            session_id="session-1",
            window_start=start,
            window_end=end,
            item_key=item_key,
            title="Weekly Review",
            participants=["Alice", "Bob"],
            quote="Reviewed the launch plan.",
            confidence=0.9,
        )
        later, later_created = occurrences.upsert(
            conn,
            session_id="session-1",
            window_start=end,
            window_end=end + timedelta(minutes=5),
            item_key=item_key,
            title=" weekly review ",
            participants=["Alice", "Bob"],
            quote="Reviewed the launch plan.",
            confidence=0.9,
        )
        different_people, _ = occurrences.upsert(
            conn,
            session_id="session-1",
            window_start=end + timedelta(minutes=5),
            window_end=end + timedelta(minutes=10),
            item_key=item_key,
            title="Weekly Review",
            participants=["Alice", "Carol"],
            quote="Reviewed the launch plan.",
            confidence=0.9,
        )

    assert first_created is True
    assert retry_created is False
    assert retry.occurrence_id == first.occurrence_id
    assert later_created is True
    assert later.occurrence_id != first.occurrence_id
    assert later.series_id == first.series_id
    assert different_people.series_id != first.series_id
    assert first.endpoint == f"event:occurrence:{first.occurrence_id}"
    assert occurrences.parse_receipt(first.source_receipt) == first.occurrence_id


def test_explicit_item_key_rejects_divergent_payload(ac_root) -> None:
    start = datetime(2026, 8, 11, 2, 0, tzinfo=UTC)
    end = start + timedelta(minutes=5)
    with fts.cursor() as conn:
        occurrences.upsert(
            conn,
            delta_id=1,
            session_id="session-1",
            window_start=start,
            window_end=end,
            item_key="upstream-item-1",
            title="Weekly Review",
            participants=["Alice"],
            quote="Reviewed the launch plan.",
            confidence=0.9,
        )
        with pytest.raises(RuntimeError, match="divergent payload"):
            occurrences.upsert(
                conn,
                delta_id=1,
                session_id="session-1",
                window_start=start,
                window_end=end,
                item_key="upstream-item-1",
                title="Quarterly Review",
                participants=["Alice"],
                quote="Reviewed the launch plan.",
                confidence=0.9,
            )
