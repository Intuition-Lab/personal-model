"""Independent-session evidence gate for durable Point candidates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from persome.session import store as session_store
from persome.store import fts
from persome.store import model_candidates as candidates


def _seed_session(conn, session_id: str, start: datetime) -> None:
    session_store.insert(
        conn,
        session_store.SessionRow(
            id=session_id,
            start_time=start,
            end_time=start + timedelta(minutes=30),
            status="reduced",
        ),
    )


def _record(
    conn,
    *,
    session_id: str,
    start: datetime,
    quote: str = "Alice completed the runtime review.",
):
    return candidates.record_evidence(
        conn,
        candidate_kind="assertion",
        subject="Alice",
        text="Alice completed the Runtime review.",
        session_id=session_id,
        window_start=start,
        window_end=start + timedelta(minutes=5),
        quote=quote,
        confidence=0.9,
    )


def test_candidate_key_is_nfkc_casefolded_and_kind_sensitive() -> None:
    assertion = candidates.make_candidate_key(
        candidate_kind="ASSERTION",
        subject=" \uff21lice ",
        text="Completed   the Runtime Review.",
    )
    equivalent = candidates.make_candidate_key(
        candidate_kind="assertion",
        subject="alice",
        text="completed the runtime review.",
    )
    entity = candidates.make_candidate_key(
        candidate_kind="person",
        subject="alice",
        text="completed the runtime review.",
    )

    assert assertion == equivalent
    assert entity != assertion


def test_receipt_canonicalizes_timezone_equivalent_session_window() -> None:
    key = candidates.make_candidate_key(
        candidate_kind="person",
        subject="Alice",
        text="Alice",
    )
    local = timezone(timedelta(hours=8))
    local_receipt = candidates.make_evidence_receipt(
        candidate_key=key,
        session_id="session-1",
        window_start=datetime(2026, 8, 11, 10, 0, tzinfo=local),
        window_end=datetime(2026, 8, 11, 10, 5, tzinfo=local),
    )
    utc_receipt = candidates.make_evidence_receipt(
        candidate_key=key,
        session_id="session-1",
        window_start=datetime(2026, 8, 11, 2, 0, tzinfo=UTC),
        window_end=datetime(2026, 8, 11, 2, 5, tzinfo=UTC),
    )

    assert local_receipt == utc_receipt
    assert candidates.parse_evidence_receipt(local_receipt) is not None


def test_entity_first_sighting_is_only_a_pending_candidate(ac_root) -> None:
    start = datetime(2026, 8, 11, 2, 0, tzinfo=UTC)
    with fts.cursor() as conn:
        _seed_session(conn, "session-1", start)
        result = candidates.record_evidence(
            conn,
            candidate_kind="person",
            subject="Alice",
            text="Alice",
            session_id="session-1",
            window_start=start,
            window_end=start + timedelta(minutes=5),
            quote="Reviewed the launch plan with Alice.",
            confidence=0.9,
        )

    assert result is not None
    assert result.state.candidate_kind == candidates.KIND_PERSON
    assert result.state.status == candidates.STATUS_PENDING
    assert result.state.independent_sessions == 1
    assert result.promoted_now is False


def test_two_independent_sessions_promote_but_same_session_windows_do_not(ac_root) -> None:
    start = datetime(2026, 8, 11, 2, 0, tzinfo=UTC)
    with fts.cursor() as conn:
        _seed_session(conn, "session-1", start)
        _seed_session(conn, "session-2", start + timedelta(hours=1))

        first = _record(conn, session_id="session-1", start=start)
        retry = _record(conn, session_id="session-1", start=start)
        same_session_later = _record(
            conn,
            session_id="session-1",
            start=start + timedelta(minutes=5),
        )
        second_session = _record(
            conn,
            session_id="session-2",
            start=start + timedelta(hours=1),
        )

        evidence = candidates.evidence_for(conn, second_session.state.candidate_key)
        decisions = candidates.decisions_for(conn, second_session.state.candidate_key)

    assert first is not None
    assert first.state.status == candidates.STATUS_PENDING
    assert first.state.evidence_count == 1
    assert first.state.independent_sessions == 1
    assert first.evidence_created is True

    assert retry is not None
    assert retry.evidence_receipt == first.evidence_receipt
    assert retry.evidence_created is False

    assert same_session_later is not None
    assert same_session_later.state.status == candidates.STATUS_PENDING
    assert same_session_later.state.evidence_count == 2
    assert same_session_later.state.independent_sessions == 1
    assert same_session_later.promoted_now is False

    assert second_session is not None
    assert second_session.state.status == candidates.STATUS_PROMOTED
    assert second_session.state.evidence_count == 3
    assert second_session.state.independent_sessions == 2
    assert second_session.promoted_now is True
    assert len(evidence) == 3
    assert {decision.to_status for decision in decisions} == {
        candidates.STATUS_PENDING,
        candidates.STATUS_PROMOTED,
    }


@pytest.mark.parametrize("session_id", ["", "unknown", "missing-session"])
def test_unknown_or_missing_session_fails_closed(ac_root, session_id: str) -> None:
    start = datetime(2026, 8, 11, 2, 0, tzinfo=UTC)
    with fts.cursor() as conn:
        result = _record(conn, session_id=session_id, start=start)
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_candidates'"
        ).fetchone()

    assert result is None
    assert table is None


def test_same_receipt_with_divergent_evidence_payload_raises_and_rolls_back(ac_root) -> None:
    start = datetime(2026, 8, 11, 2, 0, tzinfo=UTC)
    with fts.cursor() as conn:
        _seed_session(conn, "session-1", start)
        first = _record(conn, session_id="session-1", start=start)
        assert first is not None

        with pytest.raises(candidates.CandidateCollisionError, match="evidence receipt"):
            _record(
                conn,
                session_id="session-1",
                start=start,
                quote="A different quote for the same claimed evidence window.",
            )

        state = candidates.get(conn, first.state.candidate_key)
        evidence = candidates.evidence_for(conn, first.state.candidate_key)

    assert state is not None and state.evidence_count == 1
    assert len(evidence) == 1
    assert evidence[0].quote == "Alice completed the runtime review."


def test_candidate_hash_collision_with_divergent_canonical_payload_raises(
    ac_root, monkeypatch
) -> None:
    start = datetime(2026, 8, 11, 2, 0, tzinfo=UTC)
    forced_key = "a" * 32
    monkeypatch.setattr(candidates, "make_candidate_key", lambda **_kwargs: forced_key)
    with fts.cursor() as conn:
        _seed_session(conn, "session-1", start)
        _seed_session(conn, "session-2", start + timedelta(hours=1))
        first = _record(conn, session_id="session-1", start=start)
        assert first is not None

        with pytest.raises(candidates.CandidateCollisionError, match="candidate key"):
            candidates.record_evidence(
                conn,
                candidate_kind="assertion",
                subject="Bob",
                text="Bob completed a different review.",
                session_id="session-2",
                window_start=start + timedelta(hours=1),
                window_end=start + timedelta(hours=1, minutes=5),
                quote="Bob completed a different review.",
                confidence=0.9,
            )

        state = candidates.get(conn, forced_key)
        evidence = candidates.evidence_for(conn, forced_key)

    assert state is not None and state.subject == "Alice"
    assert len(evidence) == 1


def test_owner_explicit_decision_bypasses_session_gate_and_remains_auditable(ac_root) -> None:
    with fts.cursor() as conn:
        promoted = candidates.record_owner_decision(
            conn,
            candidate_kind="assertion",
            subject="Alice",
            text="Alice completed the Runtime review.",
            decision="promote",
            source_receipt="⟨owner-edit-1:memory_deltas⟩",
            reason="Owner confirmed this Point.",
        )
        assert promoted is not None
        decisions_after_promote = candidates.decisions_for(conn, promoted.candidate_key)

        rejected = candidates.record_owner_decision(
            conn,
            candidate_kind="assertion",
            subject="Alice",
            text="Alice completed the Runtime review.",
            decision="reject",
            source_receipt="⟨owner-edit-2:memory_deltas⟩",
            reason="Owner withdrew this Point.",
        )
        decisions_after_reject = candidates.decisions_for(conn, promoted.candidate_key)

    assert promoted.status == candidates.STATUS_PROMOTED
    assert promoted.evidence_count == 0
    assert promoted.independent_sessions == 0
    assert promoted.decision_source == "owner_explicit"
    assert len(decisions_after_promote) == 1
    assert decisions_after_promote[0].to_status == candidates.STATUS_PROMOTED

    assert rejected is not None and rejected.status == candidates.STATUS_REJECTED
    assert [decision.to_status for decision in decisions_after_reject] == [
        candidates.STATUS_PROMOTED,
        candidates.STATUS_REJECTED,
    ]


def test_owner_decision_receipt_is_idempotent_and_divergence_fails(ac_root) -> None:
    args = {
        "candidate_kind": "assertion",
        "subject": "Alice",
        "text": "Alice completed the Runtime review.",
        "source_receipt": "⟨owner-edit-1:memory_deltas⟩",
        "reason": "Owner confirmed this Point.",
    }
    with fts.cursor() as conn:
        first = candidates.record_owner_decision(conn, decision="promote", **args)
        retry = candidates.record_owner_decision(conn, decision="promote", **args)
        assert first is not None

        with pytest.raises(candidates.CandidateCollisionError, match="owner decision receipt"):
            candidates.record_owner_decision(conn, decision="reject", **args)

        state = candidates.get(conn, first.candidate_key)
        decisions = candidates.decisions_for(conn, first.candidate_key)

    assert retry == first
    assert state is not None and state.status == candidates.STATUS_PROMOTED
    assert len(decisions) == 1


def test_rejected_candidate_does_not_repromote_from_inferred_sessions(ac_root) -> None:
    start = datetime(2026, 8, 11, 2, 0, tzinfo=UTC)
    with fts.cursor() as conn:
        rejected = candidates.record_owner_decision(
            conn,
            candidate_kind="assertion",
            subject="Alice",
            text="Alice completed the Runtime review.",
            decision="reject",
            source_receipt="⟨owner-edit-1:memory_deltas⟩",
        )
        assert rejected is not None
        for offset, session_id in enumerate(("session-1", "session-2")):
            session_start = start + timedelta(hours=offset)
            _seed_session(conn, session_id, session_start)
            observed = _record(conn, session_id=session_id, start=session_start)
            assert observed is not None

        state = candidates.get(conn, rejected.candidate_key)

    assert state is not None
    assert state.status == candidates.STATUS_REJECTED
    assert state.independent_sessions == 2
