"""Build one TimelineBlock from a short (default 1-minute) window of captures.

Reads capture-buffer JSON files whose ``timestamp`` falls inside the
window, renders them into a prompt, and asks the LLM to produce a
small list of self-contained ``[App] …`` lines. Idempotent: skips
windows that already have a block.

The prompt reads the structured S1 fields (``focused_element``,
``visible_text``, ``url``) written by ``capture/s1_parser.py`` rather
than re-rendering the raw AX tree. Pre-v2 captures without those
fields are back-rendered via ``ax_tree_to_markdown`` as a fallback.
"""

from __future__ import annotations

import json
import re
import sqlite3
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path

from .. import paths
from ..capture import s1_parser
from ..capture.ax_models import ax_tree_to_markdown
from ..capture.timestamps import parse_capture_path_timestamp, parse_capture_timestamp
from ..config import Config
from ..logger import get
from ..parsers import parser_for_capture
from ..prompts import load as load_prompt
from ..store import entries as entries_mod
from ..store import fts as fts_store
from ..store import parser_ticks
from ..writer import llm as llm_mod
from .attention_locus import AttentionLocus, resolve_locus
from .attention_locus import click_anchor as _click_anchor
from .attention_locus import focus_pane as _focus_pane

# Re-exported for the flag-off path + existing unit tests (test_click_anchor /
# test_focus_pane import these names from this module). The implementations now
# live in attention_locus.py so the resolver and the legacy feed share one copy.
__all__ = ["_click_anchor", "_focus_pane"]
from . import store

logger = get("persome.timeline")

# Per-capture slice that goes into the timeline prompt. S1 parser
# already caps visible_text at 10k; the timeline prompt is now a
# verbatim-preserving normalizer, so we want to keep as much as the
# context budget allows. 1-min windows rarely carry more than ~6
# captures in practice.
_PER_CAPTURE_TEXT_LIMIT = 4000
# Defensive ceiling: if something goes haywire and a 1-min window has
# 30+ captures, keep the newest ones. Later events are more recent and
# tend to be more informative.
_MAX_EVENTS_PER_WINDOW = 30

# Terminal emulators that scroll oldest-to-newest: the most recent content
# is at the bottom, so tail-truncation gives better intent signal than
# head-truncation.
#
# cmux (com.cmuxterm.app) is GPU-rendered and exposes ~no AX text, so the
# capture worker injects the real terminal surface (read over cmux's RPC)
# at the *end* of visible_text, after the AX chrome (workspace/tab sidebar,
# split buttons, update banner). Tail-truncation therefore keeps the injected
# terminal content — the actual attention target — and drops the leading
# chrome. Without this, the default head-slice keeps the chrome and cuts the
# terminal content exactly when a session is busy enough to overflow the cap.
_TERMINAL_BUNDLES: frozenset[str] = frozenset(
    {
        "com.googlecode.iterm2",
        "com.apple.Terminal",
        "io.alacritty",
        "net.kovidgoyal.kitty",
        "com.cmuxterm.app",
    }
)

# Chat/IM apps where message threads scroll downward (newest at bottom).
# The AX tree exposes full message content for these Electron-based apps,
# so tail-truncation surfaces the most recent messages instead of the oldest.
_CHAT_BUNDLES: frozenset[str] = frozenset(
    {
        "com.electron.lark",  # Feishu / Lark
    }
)


def _slice_visible_text(
    visible_text: str,
    focused_value: str,
    bundle: str,
    limit: int,
) -> str:
    """Return at most *limit* chars of *visible_text*, prioritising the region
    around the focused element's typed content.

    Strategy (tried in order):

    1. **Focused-value anchor** — if the focused element has a meaningful
       value (>20 chars), locate its opening in *visible_text* and return a
       context window centred slightly behind the match (25 % pre, 75 % post).
       This works for any app: text editors, chat composers, terminal inputs.
    2. **Terminal / chat tail** — for terminal emulators and Electron chat apps
       (e.g. Feishu), return the trailing *limit* chars.  Both scroll
       oldest-to-newest, so leading content is stale scrollback / old messages.
    3. **Default head** — for everything else, return the leading *limit*
       chars (page title + main body for browsers / document viewers).
    """
    if len(visible_text) <= limit:
        return visible_text

    search = (focused_value or "").strip()
    if len(search) > 20:
        idx = visible_text.find(search[:80])
        if idx >= 0:
            pre = limit // 2
            start = max(0, idx - pre)
            end = min(len(visible_text), start + limit)
            prefix = "…\n" if start > 0 else ""
            suffix = "\n…" if end < len(visible_text) else ""
            return prefix + visible_text[start:end] + suffix

    if bundle in _TERMINAL_BUNDLES | _CHAT_BUNDLES:
        return "…\n" + visible_text[-limit:]

    return visible_text[:limit] + "\n…"


# Budget for the raw focus excerpt stored on each block (a lossless backstop for
# session modeling; see TimelineBlock.focus_excerpt). Generous head slice so a
# chat message that sits just after the sidebar (the common AX layout) is always
# included — the chat-"tail" heuristic in _slice_visible_text would miss it.
_FOCUS_EXCERPT_CHARS = 8000


def _focus_excerpt(parsed: list[tuple[Path, dict]]) -> str:
    for _p, data in reversed(parsed):
        vt = data.get("visible_text")
        if vt is None:
            ax = data.get("ax_tree")
            vt = ax_tree_to_markdown(ax) if ax else ""
        vt = str(vt).strip()
        if vt:
            return vt[:_FOCUS_EXCERPT_CHARS]
    return ""


def _focus_structured_with_outcome(
    parsed: list[tuple[Path, dict]],
) -> tuple[str, str | None, str | None, str | None]:
    """Per-app structured conversation + telemetry outcome for the window.

    Walks newest→oldest. For the first capture whose app (``window_meta.bundle_id``)
    has a registered parser, that parser decides the outcome:

    - parser raised → ``("", bundle, "miss", "exception")``
    - ``parse`` → ``None`` → ``("", bundle, "miss", "decline")``
    - ``render()`` empty → ``("", bundle, "miss", "empty_render")``
    - ``render()`` non-empty → ``(rendered, bundle, "hit", None)``

    If **no** capture's app had a parser but at least one capture carried an
    ``ax_tree`` + ``bundle`` → ``("", <most-recent-such-bundle>, "fallback", None)``
    (session modeling then falls back to the raw ``focus_excerpt``).

    If the window had nothing parseable at all (no ax_tree+bundle) →
    ``("", None, None, None)`` and the caller records no tick.

    Every miss additionally emits one structured ``parser_miss`` log line
    (``bundle= reason= capture=``) so the three causes are separable in the
    logs without touching the ``parser_ticks`` schema (#548).

    Never raises — a parser failure on one capture is logged and treated as a
    ``miss``. Returns ``(text, bundle, outcome, miss_reason)``.
    """
    fallback_bundle: str | None = None
    for _p, data in reversed(parsed):
        ax = data.get("ax_tree")
        if not ax:
            continue
        wm = data.get("window_meta") or {}
        bundle = str(wm.get("bundle_id") or "")
        parser = parser_for_capture(bundle, ax if isinstance(ax, dict) else None)
        if parser is None:
            # Remember the newest ax_tree-bearing bundle so a window with no
            # parseable app is still attributed to a real app on fallback.
            if fallback_bundle is None:
                fallback_bundle = bundle
            continue
        try:
            conv = parser.parse(ax, window_title=wm.get("title"))
        except Exception as exc:  # noqa: BLE001 - a parser bug must not break the tick
            logger.warning(
                "timeline: parser_miss bundle=%s reason=exception capture=%s error=%s",
                bundle,
                _p.name,
                exc,
            )
            return "", bundle, "miss", "exception"
        if conv is None:
            logger.info(
                "timeline: parser_miss bundle=%s reason=decline capture=%s", bundle, _p.name
            )
            return "", bundle, "miss", "decline"
        rendered = conv.render().strip()
        if not rendered:
            logger.info(
                "timeline: parser_miss bundle=%s reason=empty_render capture=%s", bundle, _p.name
            )
            return "", bundle, "miss", "empty_render"
        return rendered, bundle, "hit", None
    if fallback_bundle is not None:
        return "", fallback_bundle, "fallback", None
    return "", None, None, None


def captures_in_window(start: datetime, end: datetime) -> list[Path]:
    buf = paths.capture_buffer_dir()
    if not buf.exists():
        return []
    timestamped: list[tuple[datetime, Path]] = []
    for p in buf.iterdir():
        if p.suffix != ".json" or not p.is_file():
            continue
        timestamp = parse_capture_path_timestamp(p)
        if timestamp is not None and start <= timestamp < end:
            timestamped.append((timestamp, p))
    timestamped.sort(key=lambda item: (item[0], item[1].name))
    return [path for _, path in timestamped]


def _load_captures(capture_files: list[Path]) -> list[tuple[Path, dict]]:
    """Parse every capture JSON once. Files that fail to read/parse are dropped.

    The window is small (≤30 files) so the entire parsed list stays cheap to
    pass around; the win is avoiding a second ``json.loads`` per file when
    ``_heuristic_entries`` runs after the LLM returns no usable output.
    """
    parsed: list[tuple[Path, dict]] = []
    for p in capture_files:
        # read_bytes() + json.loads handles BOM/encoding sniffing; read_text()
        # would raise UnicodeDecodeError (a ValueError, not OSError) on a
        # mis-encoded file and crash the aggregator instead of dropping it.
        try:
            data = json.loads(p.read_bytes())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("timeline: failed to load capture %s: %s", p.name, exc)
            continue
        if not isinstance(data, dict):
            logger.warning("timeline: capture %s is not a JSON object", p.name)
            continue
        # Sanitize once at the replay boundary so prompt rendering, heuristic
        # fallback, focus_excerpt, and per-app structured parsers all consume
        # the same placeholder-free projection.
        parsed.append((p, s1_parser.sanitize_capture(data, replace_ax_tree=True)))
    return parsed


def bounded_prompt_captures(
    parsed: list[tuple[Path, dict]],
) -> list[tuple[Path, dict]]:
    """Return the exact capture subset eligible for one timeline-style prompt."""
    return parsed[-_MAX_EVENTS_PER_WINDOW:]


def _timeline_source(path: Path, data: dict) -> store.TimelineBlockSource:
    captured_at = parse_capture_timestamp(str(data.get("timestamp") or ""))
    if captured_at is None:
        captured_at = parse_capture_path_timestamp(path)
    if captured_at is None:
        raise ValueError(f"timeline source has no recoverable timestamp: {path.name}")
    return store.TimelineBlockSource(
        capture_id=f"capture:{path.stem}",
        captured_at=captured_at,
    )


def _format_events(
    parsed: list[tuple[Path, dict]],
    *,
    locus_enabled: bool = True,
    display_tz: tzinfo | None = None,
) -> tuple[str, list[str], AttentionLocus | None]:
    """Render captures for the timeline prompt.

    Returns ``(events_text, apps_used, block_locus)``. When ``locus_enabled``
    the per-capture ``| `` line is the resolved attention-locus content
    (``PRIMARY:`` / ``PERIPHERAL:``) — code-owned localization, chrome dropped
    for resolver-backed apps — and ``block_locus`` is the window's dominant
    locus (highest confidence, latest on ties) for persistence. When disabled
    it reproduces the pre-Step-1 feed (``FOCUSED PANE`` / raw visible_text) and
    ``block_locus`` is ``None``.

    Reads the structured S1 fields written by ``capture/s1_parser.py`` —
    ``focused_element``, ``visible_text``, ``url`` — and lays them out in
    the one-line-per-capture format matching Einsia's S1 prompt rendering.
    Pre-v2 captures without those fields fall back to a bounded
    ``ax_tree_to_markdown`` render so historical buffer contents still work.
    """
    lines: list[str] = []
    apps: set[str] = set()
    block_locus: AttentionLocus | None = None

    files = bounded_prompt_captures(parsed)
    for i, (p, data) in enumerate(files, 1):
        # Direct unit callers can pass pre-parsed captures without going
        # through ``_load_captures``. Keep this idempotent boundary guard.
        data = s1_parser.sanitize_capture(data, replace_ax_tree=True)
        ts_raw = str(data.get("timestamp", p.stem))
        ts = _short_time(ts_raw, display_tz=display_tz)

        wm = data.get("window_meta") or {}
        app = str(wm.get("app_name") or "Unknown")
        title = str(wm.get("title") or "")
        bundle = str(wm.get("bundle_id") or "")
        if app:
            apps.add(app)

        trigger = data.get("trigger") or {}
        event_type = str(trigger.get("event_type") or "")

        parts = [f"{i}. [{ts}] {app}"]
        if title:
            parts.append(f"— {title}")
        if bundle:
            parts.append(f"({bundle})")

        url = data.get("url")
        if url:
            parts.append(f"(URL: {url})")

        fe = data.get("focused_element") or {}
        role = str(fe.get("role") or "")
        if role:
            role_desc = f"[{role}]"
            if fe.get("is_editable"):
                role_desc += " (editing)"
            fe_title = str(fe.get("title") or "")
            if fe_title:
                role_desc += f" title={fe_title[:80]}"
            value_length = int(fe.get("value_length") or 0)
            if value_length:
                role_desc += f" len={value_length}"
            value = str(fe.get("value") or "")
            if value:
                role_desc += f": {value}"
            parts.append(role_desc)

        if event_type:
            parts.append(f"<{event_type}>")

        # Attention anchor: for a pointer event the watcher hit-tests the AX
        # element directly under the cursor and ships it on trigger.details.
        # This is the "what did the user point at" signal — the strongest
        # focus cue in AX-opaque apps (terminals) where focused_element is
        # empty. Render the element so the normalizer can anchor the entry on
        # the clicked target instead of the window chrome.
        anchor = _click_anchor(trigger)
        if anchor:
            parts.append(anchor)

        lines.append(" ".join(parts))

        visible_text = data.get("visible_text")
        if visible_text is None:
            # Pre-v2 capture — fall back to rendering the raw AX tree.
            ax = data.get("ax_tree")
            visible_text = ax_tree_to_markdown(ax) if ax else ""
        visible_text = str(visible_text).strip()

        # If AX produced no text but OCR was submitted, use the OCR result.
        if not visible_text and data.get("ocr_submitted"):
            try:
                with fts_store.cursor() as conn:
                    ocr_text = fts_store.get_ocr_result_for_capture(conn, p.stem)
                    if ocr_text:
                        visible_text = s1_parser.sanitize_ocr_text(data, ocr_text).strip()
            except Exception:  # noqa: BLE001
                pass

        if visible_text:
            fe_value = str((data.get("focused_element") or {}).get("value") or "")
            if locus_enabled:
                # Code owns "where": resolve the attention locus and feed ITS
                # content (chrome dropped for resolver-backed apps) instead of
                # the raw dump. Track the window's dominant locus (highest
                # confidence, latest on ties) for persistence on the block.
                loc = resolve_locus(data, visible_text=visible_text)
                if block_locus is None or loc.confidence >= block_locus.confidence:
                    block_locus = loc
                primary = _slice_visible_text(
                    loc.content, fe_value, bundle, _PER_CAPTURE_TEXT_LIMIT
                )
                lines.append(f"| PRIMARY: {primary.replace(chr(10), ' ')}")
                if loc.peripheral:
                    per = _slice_visible_text(
                        loc.peripheral, "", bundle, _PER_CAPTURE_TEXT_LIMIT // 2
                    )
                    lines.append(f"| PERIPHERAL: {per.replace(chr(10), ' ')}")
            else:
                pane, focused = _focus_pane(visible_text)
                pane = _slice_visible_text(pane, fe_value, bundle, _PER_CAPTURE_TEXT_LIMIT)
                preview = pane.replace("\n", " ")
                label = "FOCUSED PANE: " if focused else ""
                lines.append(f"| {label}{preview}")

        lines.append("")
    return "\n".join(lines).strip(), sorted(apps), block_locus


@dataclass(frozen=True)
class ExactSliceBlock:
    """One cutoff-safe block and the source receipts used to render it."""

    block: store.TimelineBlock
    persisted_block_id: str
    parsed_captures: tuple[tuple[Path, dict], ...]
    full_window: bool


@dataclass(frozen=True)
class ExactSliceResult:
    """Cutoff-safe timeline evidence for one half-open interval."""

    evidence: tuple[ExactSliceBlock, ...] = ()
    skipped_reason: str = ""

    @property
    def blocks(self) -> list[store.TimelineBlock]:
        return [item.block for item in self.evidence]


def render_capture_slice(
    cfg: Config,
    *,
    start: datetime,
    end: datetime,
    parsed: list[tuple[Path, dict]],
    persisted_block_id: str = "",
) -> store.TimelineBlock | None:
    """Reconstruct an exact boundary slice without whole-minute metadata."""
    bounded = bounded_prompt_captures(parsed)
    if not bounded:
        return None
    events_text, apps_used, _ = _format_events(
        bounded,
        locus_enabled=bool(getattr(cfg.timeline, "attention_locus_enabled", True)),
        display_tz=start.tzinfo,
    )
    if not events_text.strip():
        return None
    first_stem = bounded[0][0].stem
    return store.TimelineBlock(
        id=(f"slice:{persisted_block_id}:{first_stem}:{start.isoformat()}:{end.isoformat()}"),
        start_time=start,
        end_time=end,
        entries=[events_text],
        apps_used=apps_used,
        capture_count=len(bounded),
        created_at=end,
        # Deliberately omit focus/attention/action/skill fields: all are
        # whole-minute derivatives and may contain post-cutoff evidence.
    )


def _window_has_durable_occupancy(
    conn: sqlite3.Connection,
    *,
    start: datetime,
    end: datetime,
) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM captures"
            " WHERE persome_epoch(timestamp) >= persome_epoch(?)"
            " AND persome_epoch(timestamp) < persome_epoch(?) LIMIT 1",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
    except sqlite3.Error:
        row = None
    return row is not None or bool(captures_in_window(start, end))


def exact_timeline_slice(
    cfg: Config,
    conn: sqlite3.Connection,
    *,
    start: datetime,
    end: datetime,
    require_closing_block: bool = False,
    limit: int | None = None,
    include_full_captures: bool = False,
) -> ExactSliceResult:
    """Return timeline evidence that is causally bounded to ``[start, end)``.

    Persisted blocks are safe only when the requested slice covers their whole
    wall window. A straddling first or last block is reconstructed from raw
    captures strictly inside the half-open interval. For terminal processing,
    the closing wall block is a completion barrier: until it exists, the minute
    is not known to be closed. Once it exists, missing raw boundary provenance
    is a retryable failure rather than permission to expose whole-minute text.
    """
    if start >= end or (limit is not None and limit <= 0):
        return ExactSliceResult()

    window_minutes = max(1, int(getattr(cfg.timeline, "window_minutes", 1)))
    step = timedelta(minutes=window_minutes)
    closing_start = store.floor_to_window(end, window_minutes)
    closing_partial = closing_start < end
    if require_closing_block and closing_partial:
        closing_end = closing_start + step
        if store.get_window(conn, closing_start, closing_end) is None and (
            _window_has_durable_occupancy(
                conn,
                start=max(start, closing_start),
                end=end,
            )
        ):
            return ExactSliceResult(skipped_reason="awaiting_closing_block")

    # Scan the capture buffer once. Re-running captures_in_window for every
    # persisted minute turns a multi-day pattern lookback into O(blocks*files).
    timestamped_paths = [
        (timestamp, path)
        for path in captures_in_window(start, end)
        if (timestamp := parse_capture_path_timestamp(path)) is not None
    ]
    capture_epochs = [timestamp.timestamp() for timestamp, _ in timestamped_paths]

    def paths_between(slice_start: datetime, slice_end: datetime) -> list[Path]:
        left = bisect_left(capture_epochs, slice_start.timestamp())
        right = bisect_left(capture_epochs, slice_end.timestamp())
        return [path for _, path in timestamped_paths[left:right]]

    params: list[str | int] = [start.isoformat(), end.isoformat()]
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        params.append(max(0, min(int(limit), 200)))
    rows = conn.execute(
        "SELECT * FROM timeline_blocks"
        " WHERE persome_epoch(end_time) > persome_epoch(?)"
        " AND persome_epoch(start_time) < persome_epoch(?)"
        " ORDER BY persome_epoch(start_time) DESC, id DESC" + limit_sql,
        params,
    ).fetchall()
    rows.reverse()
    persisted_blocks = [store._row_to_block(row) for row in rows]

    # A later materialized block must never let a reducer/model watermark jump
    # over an occupied minute whose TimelineBlock is still missing (for example,
    # after a crash between capture commit and timeline aggregation). Active
    # callers may ignore only the not-yet-closed trailing wall window.
    coverage_start = (
        max(start, persisted_blocks[0].start_time)
        if limit is not None and persisted_blocks
        else start
    )
    coverage_end = end if require_closing_block else store.floor_to_window(end, window_minutes)
    occupied_at: list[datetime] = []
    if coverage_start < coverage_end:
        try:
            capture_rows = conn.execute(
                "SELECT timestamp FROM captures"
                " WHERE persome_epoch(timestamp) >= persome_epoch(?)"
                " AND persome_epoch(timestamp) < persome_epoch(?)",
                (coverage_start.isoformat(), coverage_end.isoformat()),
            ).fetchall()
        except sqlite3.Error:
            capture_rows = []
        for capture_row in capture_rows:
            timestamp = parse_capture_timestamp(str(capture_row[0] or ""))
            if timestamp is not None:
                occupied_at.append(timestamp)
        occupied_at.extend(
            timestamp
            for timestamp, _ in timestamped_paths
            if coverage_start <= timestamp < coverage_end
        )

    checked_windows: set[tuple[float, float]] = set()
    for timestamp in occupied_at:
        local_timestamp = timestamp.astimezone(coverage_start.tzinfo)
        wall_start = store.floor_to_window(local_timestamp, window_minutes)
        wall_end = wall_start + step
        key = (wall_start.timestamp(), wall_end.timestamp())
        if key in checked_windows:
            continue
        checked_windows.add(key)
        if wall_end > coverage_end and not require_closing_block:
            continue
        if store.get_window(conn, wall_start, wall_end) is None:
            return ExactSliceResult(skipped_reason="awaiting_timeline_block")

    evidence: list[ExactSliceBlock] = []
    for persisted in persisted_blocks:
        slice_start = max(start, persisted.start_time)
        slice_end = min(end, persisted.end_time)
        if slice_start >= slice_end:
            continue
        full_window = slice_start == persisted.start_time and slice_end == persisted.end_time
        parsed = (
            bounded_prompt_captures(_load_captures(paths_between(slice_start, slice_end)))
            if include_full_captures or not full_window
            else []
        )
        if full_window:
            evidence.append(
                ExactSliceBlock(
                    block=persisted,
                    persisted_block_id=persisted.id,
                    parsed_captures=tuple(parsed),
                    full_window=True,
                )
            )
            continue

        manifest = store.source_manifest(conn, persisted.id)
        if manifest:
            if any(source.captured_at is None for source in manifest):
                return ExactSliceResult(skipped_reason="no_cutoff_safe_blocks")
            manifest_ids = [source.capture_id for source in manifest]
            expected_ids: list[str] = []
            for source in manifest:
                if slice_start <= source.captured_at < slice_end:
                    expected_ids.append(source.capture_id)

            manifest_set = set(manifest_ids)
            raw_by_id = {f"capture:{path.stem}": (path, data) for path, data in parsed}
            bounded_raw_manifest_ids = [
                capture_id for capture_id in raw_by_id if capture_id in manifest_set
            ]
            if bounded_raw_manifest_ids != expected_ids:
                # The durable manifest proves this slice originally contained
                # source evidence that retention/corruption has since removed.
                # Rebuilding from the surviving subset would silently change
                # the question asked of every downstream model.
                return ExactSliceResult(skipped_reason="no_cutoff_safe_blocks")
            # A cutoff slice is a projection of the persisted block's exact
            # bounded source set, not a fresh chance for a 31st raw capture that
            # the whole-block prompt deliberately excluded to re-enter.
            parsed = [raw_by_id[capture_id] for capture_id in expected_ids]
            if not expected_ids:
                # A modern manifest proves that none of this block's bounded
                # evidence belongs to the requested partial slice. Treat that
                # as an empty projection; legacy blocks cannot make this proof.
                continue

        clipped = render_capture_slice(
            cfg,
            start=slice_start,
            end=slice_end,
            parsed=parsed,
            persisted_block_id=persisted.id,
        )
        if clipped is None:
            return ExactSliceResult(skipped_reason="no_cutoff_safe_blocks")
        evidence.append(
            ExactSliceBlock(
                block=clipped,
                persisted_block_id=persisted.id,
                parsed_captures=tuple(parsed),
                full_window=False,
            )
        )

    return ExactSliceResult(evidence=tuple(evidence))


def _short_time(ts: str, *, display_tz: tzinfo | None = None) -> str:
    """`2026-04-21T17:07:32+08:00` → `17:07:32`. Best-effort only."""
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is not None:
            dt = dt.astimezone(display_tz) if display_tz is not None else dt.astimezone()
        return dt.strftime("%H:%M:%S")
    except ValueError:
        return ts[:19]


def _format_window(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


_SKILL_CONFIDENCE_FLOOR = 0.65


def _validate_skill_hint(raw: object, *, skill_paths: set[str]) -> dict | None:
    """Coerce one skill_hints element into canonical form, or None to drop it."""
    if not isinstance(raw, dict):
        return None
    skill = str(raw.get("skill") or "").strip()
    if not skill or skill not in skill_paths:
        return None
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None
    if confidence < _SKILL_CONFIDENCE_FLOOR:
        return None
    confidence = max(0.0, min(1.0, confidence))
    rationale = str(raw.get("rationale") or "").strip()
    if not rationale:
        return None
    return {"skill": skill, "confidence": confidence, "rationale": rationale}


def _echo_skill_hints(block: store.TimelineBlock, skill_hints: list[dict]) -> None:
    """Append at most one triggered-echo entry per skill and session."""
    for hint in skill_hints:
        skill_name = hint["skill"].removesuffix(".md")
        content = (
            f"Triggered with confidence {hint['confidence']:.2f}: {hint['rationale']}. "
            f"Context: {', '.join(block.apps_used)} at {block.start_time.strftime('%H:%M')}."
        )
        try:
            with fts_store.cursor() as conn:
                if not store.claim_skill_observation(
                    conn,
                    block=block,
                    skill_path=hint["skill"],
                ):
                    logger.debug(
                        "timeline: skill %s already observed in block session, skipping echo",
                        hint["skill"],
                    )
                    continue
                entries_mod.append_entry(
                    conn,
                    name=skill_name,
                    content=content,
                    tags=["triggered", "echo"],
                )
        except FileNotFoundError:
            logger.warning("timeline: skill file %s not found, skipping echo", hint["skill"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("timeline: failed to echo skill hint to %s: %s", hint["skill"], exc)


def _estimate_tokens(text: str) -> int:
    """Return a cheap deterministic prompt-budget proxy.

    CJK characters count individually; other text uses a conservative 4:1
    character ratio. This is a hard local cap, not a provider tokenizer claim.
    """
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    return cjk + (len(text) - cjk + 3) // 4


def _registered_skills_section(
    rows: list,
    *,
    events_text: str,
    max_registered: int,
    token_budget: int,
) -> tuple[str, set[str]]:
    """Render only relevance-ranked skills that fit the configured hard caps."""
    if max_registered <= 0 or token_budget <= 0:
        return "", set()

    terms = set(re.findall(r"[a-z0-9_-]+|[\u4e00-\u9fff]", events_text.casefold()))

    def rank(row: object) -> tuple[int, str]:
        text = f"{row.path} {row.description}".casefold()
        row_terms = set(re.findall(r"[a-z0-9_-]+|[\u4e00-\u9fff]", text))
        return (-len(terms & row_terms), row.path)

    header = "\n\n## Registered Skills\n\n"
    selected: list[str] = []
    selected_paths: set[str] = set()
    used = _estimate_tokens(header)
    for row in sorted(rows, key=rank):
        if len(selected) >= max_registered:
            break
        description = " ".join(str(row.description).split())
        line = f"- {row.path}" + (f": {description}" if description else "")
        cost = _estimate_tokens(line + "\n")
        if used + cost > token_budget:
            continue
        selected.append(line)
        selected_paths.add(row.path)
        used += cost
    if not selected:
        return "", set()
    return header + "\n".join(selected), selected_paths


def produce_block_for_window(
    cfg: Config,
    *,
    start: datetime,
    end: datetime,
) -> store.TimelineBlock | None:
    """Build one block. Returns ``None`` if the window is empty or already done.

    Opens its own DB connections so it is safe to call from multiple threads
    in parallel. The has-window check and the final insert use separate
    short-lived connections; no connection is held open during the LLM call.
    """
    with fts_store.cursor() as conn:
        if store.has_window(conn, start, end):
            logger.debug(
                "timeline: window %s → %s already has a block", start.isoformat(), end.isoformat()
            )
            return None

    capture_files = captures_in_window(start, end)
    if not capture_files:
        logger.info(
            "timeline: window %s → %s has 0 captures, skipping",
            start.isoformat(),
            end.isoformat(),
        )
        return None

    # Parse capture JSON once; reused for prompt rendering AND the heuristic
    # fallback so an LLM miss doesn't trigger a second pass over the same files.
    parsed = bounded_prompt_captures(_load_captures(capture_files))
    events_text, apps_used, block_locus = _format_events(
        parsed,
        locus_enabled=cfg.timeline.attention_locus_enabled,
        display_tz=start.tzinfo,
    )
    # Every whole-block derivative shares this exact bounded subset: prompt,
    # heuristic fallback, count, focus excerpt/structured parser, attention,
    # skills, and action trace. This keeps source receipts complete even when a
    # noisy minute contains more than _MAX_EVENTS_PER_WINDOW captures.
    capture_count = len(parsed)

    skill_rows: list = []
    if cfg.skill_check.enabled:
        # Pattern detection writes nested skills/skill-*.md files while legacy
        # user-authored skills can still be flat. Register both active layouts.
        with fts_store.cursor() as conn:
            skill_rows = [
                f
                for f in fts_store.list_files(conn)
                if f.path.startswith("skill-") or f.path.startswith("skills/skill-")
            ]
    skill_index_section, skill_paths = _registered_skills_section(
        skill_rows,
        events_text=events_text,
        max_registered=cfg.skill_check.max_registered,
        token_budget=cfg.skill_check.token_budget,
    )

    system_text = load_prompt("timeline_block.system.md")
    user_text = load_prompt("timeline_block.user.md").format(
        start_time=_format_window(start),
        end_time=_format_window(end),
        capture_count=capture_count,
        events_text=events_text,
        skill_index_section=skill_index_section,
    )

    entries: list[str] = []
    skill_hints: list[dict] = []
    action_trace: list[dict] = []
    try:
        resp = llm_mod.call_llm(
            cfg,
            "timeline",
            messages=[
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "text",
                            "text": system_text,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                },
                {"role": "user", "content": user_text},
            ],
            json_mode=True,
        )
        text = llm_mod.extract_text(resp).strip()
        data = json.loads(text) if text else {}
        if isinstance(data, dict):
            raw_entries = data.get("entries")
            if isinstance(raw_entries, list):
                entries = [str(e).strip() for e in raw_entries if str(e).strip()]
            raw_skills = data.get("skill_hints")
            if isinstance(raw_skills, list) and skill_paths:
                for raw in raw_skills:
                    validated_skill = _validate_skill_hint(raw, skill_paths=skill_paths)
                    if validated_skill is not None:
                        skill_hints.append(validated_skill)
                    else:
                        logger.debug("timeline: dropped malformed skill hint: %r", raw)
            raw_trace = data.get("action_trace")
            if isinstance(raw_trace, list):
                action_trace = [r for r in raw_trace if isinstance(r, dict)]
    except json.JSONDecodeError as exc:
        logger.warning("timeline: malformed JSON from LLM: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("timeline: LLM call failed: %s", exc)

    if not entries:
        entries = _heuristic_entries(parsed)

    focus_structured, parser_bundle, parser_outcome, _parser_miss_reason = (
        _focus_structured_with_outcome(parsed)
    )
    block = store.TimelineBlock(
        start_time=start,
        end_time=end,
        timezone=start.tzname() or "",
        entries=entries,
        apps_used=apps_used,
        capture_count=capture_count,
        skill_hints=skill_hints,
        action_trace=action_trace,
        focus_excerpt=_focus_excerpt(parsed),
        focus_structured=focus_structured,
        attention_surface=(block_locus.surface if block_locus else ""),
        attention_confidence=(block_locus.confidence if block_locus else 0.0),
        attention_rung=(block_locus.rung if block_locus else ""),
    )
    with fts_store.cursor() as conn:
        store.insert(
            conn,
            block,
            source_captures=[_timeline_source(path, data) for path, data in parsed],
        )
        # Parser-hit telemetry (general observability): one tick per window that
        # had something parseable. Records hit/miss/fallback bucketed by bundle
        # so we can prove the per-app parsers are firing and catch semantic-class

        # production: a telemetry write failure is logged and swallowed.
        if parser_outcome is not None:
            try:
                parser_ticks.record_tick(
                    conn,
                    ts=start.isoformat(),
                    bundle_id=parser_bundle or "",
                    outcome=parser_outcome,
                )
            except Exception as exc:  # noqa: BLE001 - telemetry must not break ingestion
                logger.warning("timeline: parser tick record failed: %s", exc)
    logger.info(
        "timeline: stored block %s — %s → %s (%d entries, %d captures, %d skills, %d actions, apps=%s)",
        block.id,
        start.isoformat(),
        end.isoformat(),
        len(entries),
        capture_count,
        len(skill_hints),
        len(action_trace),
        ", ".join(apps_used),
    )
    if skill_hints:
        _echo_skill_hints(block, skill_hints)
    return block


def _heuristic_entries(parsed: list[tuple[Path, dict]]) -> list[str]:
    """Cheap fallback when the LLM returns no parseable entries."""
    groups: list[tuple[str, str, int]] = []
    for _p, data in parsed:
        wm = data.get("window_meta") or {}
        app = str(wm.get("app_name") or "Unknown")
        title = str(wm.get("title") or "")
        if groups and groups[-1][0] == app and groups[-1][1] == title:
            groups[-1] = (app, title, groups[-1][2] + 1)
        else:
            groups.append((app, title, 1))

    entries: list[str] = []
    for app, title, _count in groups:
        if title:
            entries.append(f"[{app}] worked in window '{title}', involving —")
        else:
            entries.append(f"[{app}] active, involving —")
    return entries
