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
import hashlib
import hmac
import json
import struct
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

# Cursors must stay small even when a canonical identifier is very large.  The
# token carries the exact ordinal of the item it follows plus a digest of that
# item's ordering key.  The ordinal distinguishes duplicate evolution Lines;
# the digest detects a changed/reordered snapshot without embedding the key.
_CURSOR_SECTION_CODES = {name: index for index, name in enumerate(PAGE_SECTIONS, start=1)}
_CURSOR_STRUCT = struct.Struct(">BBQ32s")
_CURSOR_TEXT_CHARS = 56  # 42 packed bytes encode to exactly 56 unpadded base64 chars.


def dumps(payload: dict[str, Any]) -> str:
    """Serialize one projection using the same representation used for its byte budget."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def result_bytes(payload: dict[str, Any]) -> int:
    return len(dumps(payload).encode("utf-8"))


def _value_bytes(value: Any) -> int:
    """Return the exact UTF-8 size of a value embedded in a projection JSON object."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


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
    validate_cursor_shape(section=section, cursor=cursor)


def validate_cursor_shape(*, section: str, cursor: str | None) -> None:
    """Validate an opaque cursor without opening or projecting the model snapshot."""
    if cursor is None:
        return
    _decode_cursor(section, cursor)


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
    payload.update(
        {
            "root": None,
            "faces": [],
            "volumes": [],
            "coverage": {
                "root": {
                    "returned": 0,
                    "total": 1 if snapshot["root"] is not None else 0,
                },
                "faces": {"returned": 0, "total": len(snapshot["faces"])},
                "volumes": {"returned": 0, "total": len(snapshot["volumes"])},
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
    current_bytes = result_bytes(payload)
    if current_bytes > MAX_RESULT_BYTES:
        return _metadata_too_large(snapshot, redact=redact, section=DEFAULT_SECTION)

    if snapshot["root"] is not None:
        root = _compact_geometry(snapshot["root"])
        candidate_bytes = current_bytes - _value_bytes(None) + _value_bytes(root)
        if candidate_bytes <= MAX_RESULT_BYTES:
            payload["root"] = root
            payload["coverage"]["root"]["returned"] = 1
            current_bytes = candidate_bytes

    indexes = {"faces": 0, "volumes": 0}
    collections = {"faces": snapshot["faces"], "volumes": snapshot["volumes"]}
    while any(indexes[name] < len(collections[name]) for name in collections):
        for name in ("faces", "volumes"):
            index = indexes[name]
            items = collections[name]
            if index >= len(items):
                continue
            indexes[name] += 1
            compact = _compact_geometry(items[index])
            returned = int(payload["coverage"][name]["returned"])
            list_delta = _value_bytes(compact) + (1 if payload[name] else 0)
            count_delta = _value_bytes(returned + 1) - _value_bytes(returned)
            candidate_bytes = current_bytes + list_delta + count_delta
            if candidate_bytes > MAX_RESULT_BYTES:
                continue
            payload[name].append(compact)
            payload["coverage"][name]["returned"] = returned + 1
            current_bytes = candidate_bytes

    if result_bytes(payload) > MAX_RESULT_BYTES:  # pragma: no cover - accounting backstop
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


def _cursor_digest(section: str, key: Any) -> bytes:
    encoded_key = json.dumps(
        [section, key],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded_key).digest()


def _encode_cursor(section: str, index: int, key: Any) -> str:
    section_code = _CURSOR_SECTION_CODES.get(section)
    if section_code is None or not 0 <= index < 2**64:
        raise ValueError("invalid model pagination cursor")
    raw = _CURSOR_STRUCT.pack(
        PROJECTION_SCHEMA_VERSION,
        section_code,
        index,
        _cursor_digest(section, key),
    )
    cursor = base64.urlsafe_b64encode(raw).decode("ascii")
    if len(cursor) != _CURSOR_TEXT_CHARS:  # pragma: no cover - structural invariant
        raise RuntimeError("model pagination cursor encoding changed unexpectedly")
    return cursor


def _decode_cursor(section: str, cursor: str) -> tuple[int, bytes]:
    if not isinstance(cursor, str) or len(cursor) != _CURSOR_TEXT_CHARS:
        raise ValueError("invalid model pagination cursor")
    try:
        raw = base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True)
        if len(raw) != _CURSOR_STRUCT.size:
            raise ValueError("invalid cursor length")
        version, encoded_section, index, digest = _CURSOR_STRUCT.unpack(raw)
    except (binascii.Error, UnicodeEncodeError, ValueError, struct.error) as exc:
        raise ValueError("invalid model pagination cursor") from exc
    if version != PROJECTION_SCHEMA_VERSION or encoded_section != _CURSOR_SECTION_CODES.get(
        section
    ):
        raise ValueError("model pagination cursor belongs to a different response or section")
    return index, digest


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
        cursor_index, cursor_digest = _decode_cursor(section, cursor)
        if cursor_index >= len(all_items) or not hmac.compare_digest(
            _cursor_digest(section, _item_key(section, all_items[cursor_index])),
            cursor_digest,
        ):
            raise ValueError("model pagination cursor is stale; restart from the first page")
        start = cursor_index + 1

    payload = _common(snapshot, redact=redact, section=section)
    base_page = {
        "requested_limit": limit,
        "returned": 0,
        "total": len(all_items),
        "has_more": False,
        "next_cursor": None,
    }
    payload.update(
        {
            "items": [],
            "page": base_page,
            "include_evidence_refs": include_evidence_refs,
        }
    )
    base_payload_bytes = result_bytes(payload)
    if base_payload_bytes > MAX_RESULT_BYTES:
        return _metadata_too_large(snapshot, redact=redact, section=section)
    if start >= len(all_items):
        return payload

    base_page_bytes = _value_bytes(base_page)
    packed_items_bytes = 0
    end = min(start + limit, len(all_items))
    for index in range(start, end):
        raw_item = all_items[index]
        prepared = _prepare_item(
            section,
            raw_item,
            include_evidence_refs=include_evidence_refs,
        )
        has_more = index + 1 < len(all_items)
        next_cursor = (
            _encode_cursor(section, index, _item_key(section, raw_item)) if has_more else None
        )
        candidate_page = {
            "requested_limit": limit,
            "returned": len(payload["items"]) + 1,
            "total": len(all_items),
            "has_more": has_more,
            "next_cursor": next_cursor,
        }
        item_delta = _value_bytes(prepared) + (1 if payload["items"] else 0)
        candidate_bytes = (
            base_payload_bytes
            + packed_items_bytes
            + item_delta
            + _value_bytes(candidate_page)
            - base_page_bytes
        )
        if candidate_bytes > MAX_RESULT_BYTES:
            if not payload["items"]:
                return _oversized_item(
                    snapshot,
                    redact=redact,
                    section=section,
                    item_id=_lookup_id(section, raw_item),
                    resume_cursor=(
                        _encode_cursor(section, index, _item_key(section, raw_item))
                        if has_more
                        else None
                    ),
                    has_more=has_more,
                )
            break
        payload["items"].append(prepared)
        payload["page"] = candidate_page
        packed_items_bytes += item_delta

    if result_bytes(payload) > MAX_RESULT_BYTES:  # pragma: no cover - accounting backstop
        return _metadata_too_large(snapshot, redact=redact, section=section)
    return payload


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
    current_bytes = result_bytes(payload)
    if current_bytes > MAX_RESULT_BYTES:
        return _metadata_too_large(snapshot, redact=redact, section=section)

    single_base = _common(snapshot, redact=redact, section=section)
    single_base.update(
        {
            "items": [],
            "selection": {"requested": 1, "returned": 1},
            "include_evidence_refs": include_evidence_refs,
        }
    )
    single_base_bytes = result_bytes(single_base)

    for raw_item in raw_items:
        prepared = _prepare_item(section, raw_item, include_evidence_refs=include_evidence_refs)
        prepared_bytes = _value_bytes(prepared)
        returned = int(payload["selection"]["returned"])
        item_delta = prepared_bytes + (1 if payload["items"] else 0)
        count_delta = _value_bytes(returned + 1) - _value_bytes(returned)
        if current_bytes + item_delta + count_delta <= MAX_RESULT_BYTES:
            payload["items"].append(prepared)
            payload["selection"]["returned"] = returned + 1
            current_bytes += item_delta + count_delta
            continue

        item_id, _ = _trim(_lookup_id(section, raw_item), 512)
        bucket = (
            "oversized_ids"
            if single_base_bytes + prepared_bytes > MAX_RESULT_BYTES
            else "omitted_ids_due_to_size"
        )
        bucket_values = payload["selection"][bucket]
        bucket_delta = _value_bytes(item_id) + (1 if bucket_values else 0)
        if current_bytes + bucket_delta > MAX_RESULT_BYTES:
            return _metadata_too_large(snapshot, redact=redact, section=section)
        payload["selection"][bucket].append(item_id)
        current_bytes += bucket_delta

    oversized_ids = payload["selection"]["oversized_ids"]
    if not payload["items"] and oversized_ids:
        return _oversized_item(
            snapshot,
            redact=redact,
            section=section,
            item_id=str(oversized_ids[0]),
        )
    if result_bytes(payload) <= MAX_RESULT_BYTES:  # final accounting backstop
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
    if result_bytes(payload) <= MAX_RESULT_BYTES:
        return payload
    return _metadata_too_large(snapshot, redact=redact, section=section)


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
