"""Durable, bounded success receipts for S0 content dedup."""

from __future__ import annotations

import sqlite3

import pytest

from persome.store import capture_content_receipts as receipts


def _connection() -> sqlite3.Connection:
    return sqlite3.connect(":memory:", isolation_level=None)


def test_record_success_loads_newest_first() -> None:
    conn = _connection()
    try:
        first = receipts.record_success(
            conn,
            fingerprint="v2:first",
            capture_id="capture-1",
            committed_at="2026-08-11T00:00:00.000000+00:00",
        )
        second = receipts.record_success(
            conn,
            fingerprint="v2:second",
            capture_id="capture-2",
            committed_at="2026-08-11T00:00:01.000000+00:00",
        )

        loaded = receipts.load_recent(conn)

        assert first.sequence < second.sequence
        assert [receipt.fingerprint for receipt in loaded] == ["v2:second", "v2:first"]
    finally:
        conn.close()


def test_record_success_prunes_to_128_newest_rows() -> None:
    conn = _connection()
    try:
        for index in range(receipts.RECEIPT_LIMIT + 2):
            receipts.record_success(
                conn,
                fingerprint=f"v2:{index}",
                capture_id=f"capture-{index}",
                committed_at=f"2026-08-11T00:00:00.{index:06d}+00:00",
            )

        loaded = receipts.load_recent(conn, limit=receipts.RECEIPT_LIMIT + 10)

        assert len(loaded) == receipts.RECEIPT_LIMIT
        assert loaded[0].fingerprint == f"v2:{receipts.RECEIPT_LIMIT + 1}"
        assert loaded[-1].fingerprint == "v2:2"
    finally:
        conn.close()


def test_record_success_is_composable_with_caller_transaction() -> None:
    conn = _connection()
    try:
        receipts.ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        receipts.record_success(
            conn,
            fingerprint="v2:rollback",
            capture_id="capture-rollback",
            committed_at="2026-08-11T00:00:00.000000+00:00",
        )
        conn.rollback()

        assert receipts.load_recent(conn) == []
    finally:
        conn.close()


def test_record_success_rejects_zero_retention() -> None:
    conn = _connection()
    try:
        with pytest.raises(ValueError, match="keep must be positive"):
            receipts.record_success(
                conn,
                fingerprint="v2:test",
                capture_id="capture-test",
                committed_at="2026-08-11T00:00:00.000000+00:00",
                keep=0,
            )
    finally:
        conn.close()


def test_delete_for_captures_is_composable_with_capture_transaction() -> None:
    conn = _connection()
    try:
        receipts.record_success(
            conn,
            fingerprint="v2:delete",
            capture_id="capture-delete",
            committed_at="2026-08-11T00:00:00.000000+00:00",
        )
        receipts.record_success(
            conn,
            fingerprint="v2:keep",
            capture_id="capture-keep",
            committed_at="2026-08-11T00:00:01.000000+00:00",
        )

        conn.execute("BEGIN IMMEDIATE")
        assert (
            receipts.delete_for_captures(
                conn,
                ["missing"] * 1_000 + ["capture-delete"],
            )
            == 1
        )
        conn.commit()

        assert [row.capture_id for row in receipts.load_recent(conn)] == ["capture-keep"]
    finally:
        conn.close()
