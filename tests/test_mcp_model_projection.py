"""Adversarial and traversal tests for the bounded MCP model projection."""

from __future__ import annotations

import json

import pytest

from persome.mcp import model_projection


def _snapshot(
    *,
    points: list[dict] | None = None,
    lines: list[dict] | None = None,
    faces: list[dict] | None = None,
    volumes: list[dict] | None = None,
    root: dict | None = None,
    receipts: list[dict] | None = None,
    build: dict | None = None,
) -> dict:
    points = [] if points is None else points
    lines = [] if lines is None else lines
    faces = [] if faces is None else faces
    volumes = [] if volumes is None else volumes
    receipts = [] if receipts is None else receipts
    return {
        "schema_version": 1,
        "generated_at": "2026-08-03T00:00:00+00:00",
        "build": {"status": "complete", "build_id": "fixture"} if build is None else build,
        "points": points,
        "lines": lines,
        "faces": faces,
        "volumes": volumes,
        "root": root,
        "receipts": receipts,
        "stats": {
            "points": len(points),
            "active_points": len(points),
            "evolution_lines": sum(line.get("kind") == "evolution" for line in lines),
            "relation_lines": sum(line.get("kind") == "relation" for line in lines),
            "faces": len(faces),
            "volumes": len(volumes),
            "roots": int(root is not None),
            "receipts": len(receipts),
            "redactions": {},
        },
    }


def _receipt(value: str) -> dict:
    return {"receipt": value, "source_kind": "point", "source_id": "point-fixture"}


def _geometry(identifier: str, *, large: bool, level: int = 1) -> dict:
    return {
        "id": identifier,
        "level": level,
        "parent_id": None,
        "signature": "s" * (4_000 if large else 8),
        "members": [],
        "member_receipts": [],
        "source_receipts": [],
        "anchors": ["a" * 512] * 20 if large else [],
        "provenance": "mined",
        "observations": 2,
        "confidence": 0.8,
        "status": "active",
        "valid_from": "2026-08-03T00:00:00+00:00",
        "created_at": "2026-08-03T00:00:00+00:00",
    }


def _assert_bounded(payload: dict) -> None:
    assert model_projection.result_bytes(payload) <= model_projection.MAX_RESULT_BYTES
    json.loads(model_projection.dumps(payload))


def test_thirteen_mib_receipt_key_keeps_error_and_resume_cursor_bounded() -> None:
    huge_key = "r" * (13 * 1024 * 1024)
    snapshot = _snapshot(receipts=[_receipt(huge_key), _receipt("receipt-later")])

    blocked = model_projection.project_snapshot(
        snapshot,
        redact=True,
        section="receipts",
        limit=2,
    )
    cursor = blocked["pagination"]["resume_cursor"]

    assert blocked["error"]["code"] == "model_item_exceeds_mcp_budget"
    assert len(cursor) == 56
    model_projection.validate_cursor_shape(section="receipts", cursor=cursor)
    _assert_bounded(blocked)

    resumed = model_projection.project_snapshot(
        snapshot,
        redact=True,
        section="receipts",
        cursor=cursor,
        limit=2,
    )
    assert [item["receipt"] for item in resumed["items"]] == ["receipt-later"]
    _assert_bounded(resumed)


@pytest.mark.parametrize("huge_key", ["k" * 100_000, "🙂" * 100_000])
def test_large_ascii_and_unicode_keys_return_fixed_size_cursors(huge_key: str) -> None:
    snapshot = _snapshot(receipts=[_receipt(huge_key), _receipt("receipt-later")])

    blocked = model_projection.project_snapshot(
        snapshot,
        redact=True,
        section="receipts",
        limit=1,
    )
    cursor = blocked["pagination"]["resume_cursor"]

    assert blocked["error"]["code"] == "model_item_exceeds_mcp_budget"
    assert len(cursor) == 56
    model_projection.validate_cursor_shape(section="receipts", cursor=cursor)
    _assert_bounded(blocked)


def test_fitting_unicode_key_returns_reusable_fixed_size_next_cursor() -> None:
    unicode_key = "🙂" * 1_024
    snapshot = _snapshot(receipts=[_receipt(unicode_key), _receipt("receipt-later")])

    first = model_projection.project_snapshot(
        snapshot,
        redact=True,
        section="receipts",
        limit=1,
    )
    cursor = first["page"]["next_cursor"]

    assert [item["receipt"] for item in first["items"]] == [unicode_key]
    assert len(cursor) == 56
    model_projection.validate_cursor_shape(section="receipts", cursor=cursor)
    _assert_bounded(first)

    second = model_projection.project_snapshot(
        snapshot,
        redact=True,
        section="receipts",
        cursor=cursor,
        limit=1,
    )
    assert [item["receipt"] for item in second["items"]] == ["receipt-later"]
    _assert_bounded(second)


def test_duplicate_line_keys_page_by_exact_occurrence_and_terminate() -> None:
    duplicate_lines = [
        {
            "id": "evolution:old:new",
            "kind": "evolution",
            "source": "old",
            "target": "new",
            "predicate": "supersedes",
            "marker": marker,
        }
        for marker in ("first", "second", "third")
    ]
    lines = [
        *duplicate_lines,
        {
            "id": "relation-later",
            "kind": "relation",
            "source": "self",
            "target": "project",
            "predicate": "works_on",
            "marker": "later",
        },
    ]
    snapshot = _snapshot(lines=lines)
    cursor = None
    markers: list[str] = []
    cursors: list[str] = []

    for _ in range(10):
        page = model_projection.project_snapshot(
            snapshot,
            redact=True,
            section="lines",
            cursor=cursor,
            limit=1,
        )
        _assert_bounded(page)
        markers.extend(item["marker"] for item in page["items"])
        if not page["page"]["has_more"]:
            break
        cursor = page["page"]["next_cursor"]
        cursors.append(cursor)
    else:  # pragma: no cover - explicit infinite-loop guard
        pytest.fail("duplicate-key pagination did not terminate")

    assert markers == ["first", "second", "third", "later"]
    assert len(set(cursors)) == len(cursors)


def test_multiple_oversized_items_can_each_be_skipped_before_small_item() -> None:
    points = [{"id": f"point-huge-{index}", "content": "x" * (2 * 64 * 1024)} for index in range(3)]
    points.append({"id": "point-small", "content": "reachable"})
    snapshot = _snapshot(points=points)
    cursor = None

    for index in range(3):
        blocked = model_projection.project_snapshot(
            snapshot,
            redact=True,
            section="points",
            cursor=cursor,
            limit=100,
        )
        assert blocked["error"]["item_id"] == f"point-huge-{index}"
        cursor = blocked["pagination"]["resume_cursor"]
        assert len(cursor) == 56
        _assert_bounded(blocked)

    page = model_projection.project_snapshot(
        snapshot,
        redact=True,
        section="points",
        cursor=cursor,
        limit=100,
    )
    assert [item["id"] for item in page["items"]] == ["point-small"]
    assert page["page"]["has_more"] is False
    _assert_bounded(page)


def test_exact_id_selection_accounts_for_multiple_oversized_items_incrementally() -> None:
    points = [{"id": f"point-huge-{index}", "content": "x" * (2 * 64 * 1024)} for index in range(3)]
    points.append({"id": "point-small", "content": "reachable"})
    snapshot = _snapshot(points=points)

    selected = model_projection.project_snapshot(
        snapshot,
        redact=True,
        section="points",
        ids=[item["id"] for item in points],
    )

    assert [item["id"] for item in selected["items"]] == ["point-small"]
    assert selected["selection"]["oversized_ids"] == [
        "point-huge-0",
        "point-huge-1",
        "point-huge-2",
    ]
    _assert_bounded(selected)


def test_page_reports_metadata_overflow_before_examining_small_item() -> None:
    snapshot = _snapshot(
        points=[{"id": "point-small", "content": "small"}],
        build={"status": "complete", "build_id": "fixture", "future": "z" * (70 * 1024)},
    )

    page = model_projection.project_snapshot(
        snapshot,
        redact=True,
        section="points",
        limit=1,
    )

    assert page["error"]["code"] == "model_projection_metadata_exceeds_mcp_budget"
    assert "item_id" not in page["error"]
    _assert_bounded(page)


def test_overview_skips_nonfitting_geometry_and_keeps_later_small_item() -> None:
    faces = [_geometry(f"face-large-{index}", large=True) for index in range(5)]
    faces.append(_geometry("face-small-later", large=False))
    snapshot = _snapshot(faces=faces)

    overview = model_projection.project_snapshot(snapshot, redact=True)
    returned_ids = [item["id"] for item in overview["faces"]]

    assert "face-small-later" in returned_ids
    assert len(returned_ids) < len(faces)
    assert overview["coverage"]["faces"] == {
        "returned": len(returned_ids),
        "total": len(faces),
    }
    _assert_bounded(overview)


def test_page_stops_after_first_oversized_item_without_serializing_later_items() -> None:
    snapshot = _snapshot(
        points=[
            {"id": "point-huge", "content": "x" * (2 * 64 * 1024)},
            {"id": "point-not-visited", "content": object()},
        ]
    )

    page = model_projection.project_snapshot(
        snapshot,
        redact=True,
        section="points",
        limit=100,
    )

    assert page["error"]["item_id"] == "point-huge"
    _assert_bounded(page)


def test_cursor_shape_validation_rejects_malformed_and_wrong_section_tokens() -> None:
    changed_snapshot = _snapshot(points=[{"id": "point-changed", "content": "zero"}])
    page = model_projection.project_snapshot(
        _snapshot(
            points=[
                {"id": "point-0", "content": "zero"},
                {"id": "point-1", "content": "one"},
            ]
        ),
        redact=True,
        section="points",
        limit=1,
    )
    cursor = page["page"]["next_cursor"]

    model_projection.validate_cursor_shape(section="points", cursor=None)
    model_projection.validate_cursor_shape(section="points", cursor=cursor)
    with pytest.raises(ValueError, match="invalid model pagination cursor"):
        model_projection.validate_cursor_shape(section="points", cursor="!")
    with pytest.raises(ValueError, match="different response or section"):
        model_projection.validate_cursor_shape(section="lines", cursor=cursor)
    with pytest.raises(ValueError, match="stale"):
        model_projection.project_snapshot(
            changed_snapshot,
            redact=True,
            section="points",
            cursor=cursor,
            limit=1,
        )


def test_every_section_pages_to_completion_without_loss() -> None:
    points = [
        {"id": f"point-{index:03}", "content": "x" * ((index % 7) * 700)} for index in range(123)
    ]
    lines = [
        {
            "id": f"line-{index:03}",
            "kind": "relation",
            "source": "self",
            "target": f"project-{index}",
            "predicate": "works_on",
        }
        for index in range(111)
    ]
    faces = [_geometry(f"face-{index:03}", large=index % 9 == 0) for index in range(33)]
    volumes = [
        _geometry(f"volume-{index:03}", large=index % 7 == 0, level=2) for index in range(29)
    ]
    root = _geometry("root-000", large=False, level=3)
    receipts = [_receipt(f"receipt-{index:03}") for index in range(127)]
    snapshot = _snapshot(
        points=points,
        lines=lines,
        faces=faces,
        volumes=volumes,
        root=root,
        receipts=receipts,
    )

    for section in model_projection.PAGE_SECTIONS:
        expected = [root] if section == "root" else snapshot[section]
        cursor = None
        returned: list[dict] = []
        for _ in range(100):
            page = model_projection.project_snapshot(
                snapshot,
                redact=True,
                section=section,
                cursor=cursor,
                limit=17,
            )
            assert "error" not in page
            _assert_bounded(page)
            returned.extend(page["items"])
            if not page["page"]["has_more"]:
                break
            cursor = page["page"]["next_cursor"]
            assert len(cursor) == 56
        else:  # pragma: no cover - explicit infinite-loop guard
            pytest.fail(f"{section} pagination did not terminate")

        if section in {"faces", "volumes", "root"}:
            assert [item["id"] for item in returned] == [item["id"] for item in expected]
        else:
            assert returned == expected


def test_projection_envelope_key_sets_are_stable() -> None:
    common_keys = {
        "projection_schema_version",
        "model_schema_version",
        "section",
        "canonical_snapshot_complete",
        "redacted",
        "generated_at",
        "build",
        "model_stats",
        "consistency",
        "full_export",
    }
    points = [
        {"id": "point-0", "content": "zero"},
        {"id": "point-1", "content": "one"},
    ]
    snapshot = _snapshot(points=points, faces=[_geometry("face-0", large=False)])

    overview = model_projection.project_snapshot(snapshot, redact=True)
    page = model_projection.project_snapshot(
        snapshot,
        redact=True,
        section="points",
        limit=1,
    )
    selection = model_projection.project_snapshot(
        snapshot,
        redact=True,
        section="points",
        ids=["point-1", "missing"],
    )
    oversized = model_projection.project_snapshot(
        _snapshot(
            points=[
                {"id": "point-huge", "content": "x" * (2 * 64 * 1024)},
                {"id": "point-later", "content": "later"},
            ]
        ),
        redact=True,
        section="points",
        limit=1,
    )
    full = model_projection.full_export_response(redact=True)

    assert set(overview) == common_keys | {
        "root",
        "faces",
        "volumes",
        "coverage",
        "omitted_model_sections",
        "paging",
    }
    assert set(page) == common_keys | {"items", "page", "include_evidence_refs"}
    assert set(page["page"]) == {
        "requested_limit",
        "returned",
        "total",
        "has_more",
        "next_cursor",
    }
    assert set(selection) == common_keys | {"items", "selection", "include_evidence_refs"}
    assert set(selection["selection"]) == {
        "requested",
        "returned",
        "missing_ids",
        "omitted_ids_due_to_size",
        "oversized_ids",
    }
    assert set(oversized) == {
        "projection_schema_version",
        "model_schema_version",
        "section",
        "canonical_snapshot_complete",
        "redacted",
        "error",
        "full_export",
        "pagination",
    }
    assert set(oversized["error"]) == {"code", "item_id", "max_result_bytes"}
    assert set(oversized["pagination"]) == {
        "skipped_oversized_item",
        "has_more",
        "resume_cursor",
    }
    assert set(full) == {
        "projection_schema_version",
        "model_schema_version",
        "section",
        "canonical_snapshot_complete",
        "redacted",
        "error",
        "full_export",
    }
    assert set(full["error"]) == {"code", "message"}
