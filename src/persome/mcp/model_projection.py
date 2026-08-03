"""Bounded MCP projections of the canonical Personal Model snapshot.

The canonical model snapshot is intentionally complete: it retains historical
Points, evolution Lines, and every evidence receipt.  That object can grow far
beyond the message and context limits of stdio MCP clients.  This module keeps
the canonical schema unchanged and publishes an explicitly partial, byte-bounded
MCP envelope instead.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from ..model.snapshot import SCHEMA_VERSION, validate_snapshot

PROJECTION_SCHEMA_VERSION = 1
DEFAULT_SECTION = "overview"
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100
MAX_RESULT_BYTES = 64 * 1024

PAGE_SECTIONS = ("points", "lines", "faces", "volumes", "root", "receipts")
_COMPACT_TEXT_CHARS = 4_000
_COMPACT_ANCHOR_CHARS = 512
_COMPACT_ANCHORS = 20


def dumps(payload: dict[str, Any]) -> str:
    """Serialize one projection using the same representation used for its byte budget."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def result_bytes(payload: dict[str, Any]) -> int:
    return len(dumps(payload).encode("utf-8"))


def full_export_response(*, redact: bool) -> dict[str, Any]:
    """Return a small response for callers that request an unbounded full section."""
    return {
        "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        "model_schema_version": SCHEMA_VERSION,
        "section": "full",
        "canonical_snapshot_complete": False,
        "redacted": redact,
        "error": {
            "code": "full_snapshot_not_available_over_mcp",
            "message": "Full model snapshots are exported to a local file, not returned in one MCP result.",
        },
        "full_export": _full_export_hint(redact),
    }


def project_snapshot(
    snapshot: dict[str, Any],
    *,
    redact: bool,
    section: str = DEFAULT_SECTION,
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    ids: list[str] | None = None,
    include_evidence_refs: bool = False,
) -> dict[str, Any]:
    """Project a validated canonical snapshot into one bounded MCP response."""
    validate_snapshot(snapshot)
    validate_request(section=section, cursor=cursor, ids=ids)
    if section == "full":
        return full_export_response(redact=redact)
    if section == DEFAULT_SECTION:
        return _overview(snapshot, redact=redact)
    return _page(
        snapshot,
        redact=redact,
        section=section,
        cursor=cursor,
        limit=limit,
        ids=ids,
        include_evidence_refs=include_evidence_refs,
    )


def validate_request(*, section: str, cursor: str | None, ids: list[str] | None) -> None:
    """Reject invalid projection combinations before the expensive live snapshot build."""
    if section not in {DEFAULT_SECTION, *PAGE_SECTIONS, "full"}:
        allowed = ", ".join((DEFAULT_SECTION, *PAGE_SECTIONS, "full"))
        raise ValueError(f"section must be one of: {allowed}")
    if section in {DEFAULT_SECTION, "full"} and (cursor is not None or ids is not None):
        raise ValueError("cursor and ids require a paged model section")
    if cursor is not None and ids is not None:
        raise ValueError("cursor and ids are mutually exclusive")


def _full_export_hint(redact: bool) -> dict[str, Any]:
    command = "persome model export --out ./model-snapshot.json"
    if not redact:
        command += " --raw"
    return {
        "command": command,
        "redacted": redact,
        "note": "The file is owner-local and must be reviewed before sharing.",
    }


def _common(snapshot: dict[str, Any], *, redact: bool, section: str) -> dict[str, Any]:
    return {
        "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        "model_schema_version": snapshot["schema_version"],
        "section": section,
        "canonical_snapshot_complete": False,
        "redacted": redact,
        "generated_at": snapshot["generated_at"],
        "build": snapshot["build"],
        "model_stats": snapshot["stats"],
        "consistency": "transactionally_stable_per_call",
        "full_export": _full_export_hint(redact),
    }


def _trim(value: Any, maximum: int) -> tuple[Any, bool]:
    if value is None:
        return None, False
    text = str(value)
    if len(text) <= maximum:
        return text, False
    return text[: maximum - 1] + "…", True


def _compact_geometry(item: dict[str, Any]) -> dict[str, Any]:
    compact = {
        key: item.get(key)
        for key in (
            "id",
            "level",
            "parent_id",
            "provenance",
            "observations",
            "confidence",
            "status",
            "valid_from",
            "created_at",
        )
    }
    truncated: list[str] = []
    compact["signature"], signature_truncated = _trim(item.get("signature"), _COMPACT_TEXT_CHARS)
    if signature_truncated:
        truncated.append("signature")

    anchors: list[str] = []
    raw_anchors = item.get("anchors") if isinstance(item.get("anchors"), list) else []
    for value in raw_anchors[:_COMPACT_ANCHORS]:
        anchor, anchor_truncated = _trim(value, _COMPACT_ANCHOR_CHARS)
        anchors.append(str(anchor or ""))
        if anchor_truncated:
            truncated.append("anchors")
    if len(raw_anchors) > _COMPACT_ANCHORS:
        truncated.append("anchors")
    compact["anchors"] = anchors

    compact["member_count"] = len(item.get("members") or [])
    compact["member_receipt_count"] = len(item.get("member_receipts") or [])
    compact["source_receipt_count"] = len(item.get("source_receipts") or [])
    if truncated:
        compact["truncated_fields"] = sorted(set(truncated))
    return compact


def _overview(snapshot: dict[str, Any], *, redact: bool) -> dict[str, Any]:
    payload = _common(snapshot, redact=redact, section=DEFAULT_SECTION)
    faces = [_compact_geometry(item) for item in snapshot["faces"]]
    volumes = [_compact_geometry(item) for item in snapshot["volumes"]]
    root = _compact_geometry(snapshot["root"]) if snapshot["root"] is not None else None
    payload.update(
        {
            "root": root,
            "faces": [],
            "volumes": [],
            "coverage": {
                "root": {"returned": 1 if root else 0, "total": 1 if root else 0},
                "faces": {"returned": 0, "total": len(faces)},
                "volumes": {"returned": 0, "total": len(volumes)},
                "points": {"returned": 0, "total": len(snapshot["points"])},
                "lines": {"returned": 0, "total": len(snapshot["lines"])},
                "receipts": {"returned": 0, "total": len(snapshot["receipts"])},
            },
            "omitted_model_sections": ["points", "lines", "receipts"],
            "paging": {
                "sections": list(PAGE_SECTIONS),
                "default_limit": DEFAULT_PAGE_LIMIT,
                "max_limit": MAX_PAGE_LIMIT,
            },
        }
    )
    if result_bytes(payload) > MAX_RESULT_BYTES:
        return _metadata_too_large(snapshot, redact=redact, section=DEFAULT_SECTION)

    indexes = {"faces": 0, "volumes": 0}
    collections = {"faces": faces, "volumes": volumes}
    blocked: set[str] = set()
    while len(blocked) < len(collections):
        progressed = False
        for name in ("faces", "volumes"):
            if name in blocked:
                continue
            index = indexes[name]
            items = collections[name]
            if index >= len(items):
                blocked.add(name)
                continue
            payload[name].append(items[index])
            payload["coverage"][name]["returned"] = index + 1
            if result_bytes(payload) <= MAX_RESULT_BYTES:
                indexes[name] += 1
                progressed = True
            else:
                payload[name].pop()
                payload["coverage"][name]["returned"] = index
                blocked.add(name)
        if not progressed and all(name in blocked for name in collections):
            break

    if result_bytes(payload) > MAX_RESULT_BYTES:
        return _metadata_too_large(snapshot, redact=redact, section=DEFAULT_SECTION)
    return payload


def _item_key(section: str, item: dict[str, Any]) -> Any:
    if section == "lines":
        return [str(item.get("kind") or ""), str(item.get("id") or "")]
    if section == "receipts":
        return str(item.get("receipt") or "")
    return str(item.get("id") or "")


def _lookup_id(section: str, item: dict[str, Any]) -> str:
    if section == "receipts":
        return str(item.get("receipt") or "")
    return str(item.get("id") or "")


def _encode_cursor(section: str, key: Any) -> str:
    raw = json.dumps([PROJECTION_SCHEMA_VERSION, section, key], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(section: str, cursor: str) -> Any:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode())
        version, encoded_section, key = json.loads(raw)
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise ValueError("invalid model pagination cursor") from exc
    if version != PROJECTION_SCHEMA_VERSION or encoded_section != section:
        raise ValueError("model pagination cursor belongs to a different response or section")
    return key


def _prepare_item(
    section: str, item: dict[str, Any], *, include_evidence_refs: bool
) -> dict[str, Any]:
    if section in {"faces", "volumes", "root"} and not include_evidence_refs:
        return _compact_geometry(item)
    return item


def _page(
    snapshot: dict[str, Any],
    *,
    redact: bool,
    section: str,
    cursor: str | None,
    limit: int,
    ids: list[str] | None,
    include_evidence_refs: bool,
) -> dict[str, Any]:
    all_items = (
        [snapshot["root"]]
        if section == "root" and snapshot["root"] is not None
        else ([] if section == "root" else snapshot[section])
    )
    if ids is not None:
        by_id = {_lookup_id(section, item): item for item in all_items}
        raw_items = [by_id[value] for value in ids if value in by_id]
        missing_ids = [value for value in ids if value not in by_id]
        return _bounded_selected_items(
            snapshot,
            redact=redact,
            section=section,
            raw_items=raw_items,
            requested_ids=ids,
            missing_ids=missing_ids,
            include_evidence_refs=include_evidence_refs,
        )

    start = 0
    if cursor is not None:
        cursor_key = _decode_cursor(section, cursor)
        for index, item in enumerate(all_items):
            if _item_key(section, item) == cursor_key:
                start = index + 1
                break
        else:
            raise ValueError("model pagination cursor is stale; restart from the first page")

    raw_items = all_items[start : start + limit]
    payload = _common(snapshot, redact=redact, section=section)
    payload.update(
        {
            "items": [
                _prepare_item(section, item, include_evidence_refs=include_evidence_refs)
                for item in raw_items
            ],
            "page": {
                "requested_limit": limit,
                "returned": len(raw_items),
                "total": len(all_items),
                "has_more": start + len(raw_items) < len(all_items),
                "next_cursor": None,
            },
            "include_evidence_refs": include_evidence_refs,
        }
    )
    _set_next_cursor(payload, section=section, raw_items=raw_items)
    while payload["items"] and result_bytes(payload) > MAX_RESULT_BYTES:
        payload["items"].pop()
        raw_items.pop()
        payload["page"]["returned"] = len(raw_items)
        payload["page"]["has_more"] = start + len(raw_items) < len(all_items)
        _set_next_cursor(payload, section=section, raw_items=raw_items)
    if raw_items or start >= len(all_items):
        if result_bytes(payload) <= MAX_RESULT_BYTES:
            return payload
        return _metadata_too_large(snapshot, redact=redact, section=section)
    return _oversized_item(
        snapshot,
        redact=redact,
        section=section,
        item_id=_lookup_id(section, all_items[start]),
        resume_cursor=(
            _encode_cursor(section, _item_key(section, all_items[start]))
            if start + 1 < len(all_items)
            else None
        ),
        has_more=start + 1 < len(all_items),
    )


def _set_next_cursor(
    payload: dict[str, Any], *, section: str, raw_items: list[dict[str, Any]]
) -> None:
    has_more = bool(payload["page"]["has_more"])
    if has_more and raw_items:
        payload["page"]["next_cursor"] = _encode_cursor(section, _item_key(section, raw_items[-1]))
    else:
        payload["page"]["next_cursor"] = None


def _bounded_selected_items(
    snapshot: dict[str, Any],
    *,
    redact: bool,
    section: str,
    raw_items: list[dict[str, Any]],
    requested_ids: list[str],
    missing_ids: list[str],
    include_evidence_refs: bool,
) -> dict[str, Any]:
    payload = _common(snapshot, redact=redact, section=section)
    payload.update(
        {
            "items": [],
            "selection": {
                "requested": len(requested_ids),
                "returned": 0,
                "missing_ids": missing_ids,
                "omitted_ids_due_to_size": [],
                "oversized_ids": [],
            },
            "include_evidence_refs": include_evidence_refs,
        }
    )
    if result_bytes(payload) > MAX_RESULT_BYTES:
        return _metadata_too_large(snapshot, redact=redact, section=section)

    for raw_item in raw_items:
        prepared = _prepare_item(section, raw_item, include_evidence_refs=include_evidence_refs)
        payload["items"].append(prepared)
        payload["selection"]["returned"] = len(payload["items"])
        if result_bytes(payload) <= MAX_RESULT_BYTES:
            continue
        payload["items"].pop()
        payload["selection"]["returned"] = len(payload["items"])
        item_id, _ = _trim(_lookup_id(section, raw_item), 512)
        single = _common(snapshot, redact=redact, section=section)
        single.update(
            {
                "items": [prepared],
                "selection": {"requested": 1, "returned": 1},
                "include_evidence_refs": include_evidence_refs,
            }
        )
        bucket = (
            "oversized_ids"
            if result_bytes(single) > MAX_RESULT_BYTES
            else "omitted_ids_due_to_size"
        )
        payload["selection"][bucket].append(item_id)

    oversized_ids = payload["selection"]["oversized_ids"]
    if not payload["items"] and oversized_ids:
        return _oversized_item(
            snapshot,
            redact=redact,
            section=section,
            item_id=str(oversized_ids[0]),
        )
    if result_bytes(payload) <= MAX_RESULT_BYTES:
        return payload
    return _metadata_too_large(snapshot, redact=redact, section=section)


def _oversized_item(
    snapshot: dict[str, Any],
    *,
    redact: bool,
    section: str,
    item_id: str,
    resume_cursor: str | None = None,
    has_more: bool | None = None,
) -> dict[str, Any]:
    safe_id, _ = _trim(item_id, 512)
    payload = {
        "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        "model_schema_version": snapshot["schema_version"],
        "section": section,
        "canonical_snapshot_complete": False,
        "redacted": redact,
        "error": {
            "code": "model_item_exceeds_mcp_budget",
            "item_id": safe_id,
            "max_result_bytes": MAX_RESULT_BYTES,
        },
        "full_export": _full_export_hint(redact),
    }
    if has_more is not None:
        payload["pagination"] = {
            "skipped_oversized_item": True,
            "has_more": has_more,
            "resume_cursor": resume_cursor,
        }
    return payload


def _metadata_too_large(snapshot: dict[str, Any], *, redact: bool, section: str) -> dict[str, Any]:
    return {
        "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        "model_schema_version": snapshot.get("schema_version", SCHEMA_VERSION),
        "section": section,
        "canonical_snapshot_complete": False,
        "redacted": redact,
        "error": {
            "code": "model_projection_metadata_exceeds_mcp_budget",
            "max_result_bytes": MAX_RESULT_BYTES,
        },
        "full_export": _full_export_hint(redact),
    }
