"""Classifies AX watcher events and triggers captures.

Inspired by Einsia-Partner's S0/S1 collector pipeline, collapsed into a single
dispatcher that writes one JSON per semantic event into the capture buffer.

Classification rules:
  AXFocusedWindowChanged   → immediate capture
  AXApplicationActivated   → immediate capture
  UserMouseClick           → immediate capture
  UserTextInput            → immediate capture (Swift already debounced typing)
  AXValueChanged           → debounced capture (3s)
  AXTitleChanged           → skip (too noisy, covered by window/app events)

Additional guards:
  * Same-surface dedup: skip a non-focus-change capture if the last capture in
    the same bundle/app happened less than
    ``same_window_dedup_seconds`` ago. Focus changes always pass.
  * App activation and focused-window notifications for one surface transition
    share an event-family key, so the native watcher's paired notifications do
    not enqueue two captures merely because their titles were sampled at
    slightly different moments.
  * Rate limit: sequentialize captures and enforce a minimum gap so bursts
    of events (e.g. a rapid click → value change → focus change) don't
    write 5 frames in 200ms.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from ..logger import get

logger = get("persome.capture")

_IMMEDIATE_EVENTS = {
    "AXFocusedWindowChanged",
    "AXApplicationActivated",
    "UserMouseClick",
    "UserTextInput",
}
_DEBOUNCED_EVENTS = {"AXValueChanged"}
_SKIP_EVENTS = {"AXTitleChanged"}
_FOCUS_TRANSITION_EVENTS = {
    "AXFocusedWindowChanged",
    "AXApplicationActivated",
}
_EventKey = tuple[str, str, str]
_EventRecord = tuple[float, str]


def _surface_key(*, bundle_id: str, app_name: str) -> tuple[str, str]:
    """Return the stable event surface identity, deliberately excluding title."""
    if bundle_id:
        return ("bundle", bundle_id)
    if app_name:
        return ("app", app_name)
    return ("unknown", "")


class EventDispatcher:
    """Consumes watcher events and invokes a capture callback.

    ``capture_fn`` should be idempotent and safe to call from this thread.
    It will be called with a kwarg ``trigger`` carrying the event metadata
    (event_type / app_name / bundle_id / window_title) so captures can be logged.
    Returning ``False`` rejects the reservation (e.g. a full queue); ``True``
    or legacy ``None`` accepts it.
    """

    def __init__(
        self,
        capture_fn: Callable[[dict[str, Any]], bool | None],
        *,
        debounce_seconds: float = 3.0,
        min_capture_gap_seconds: float = 2.0,
        dedup_interval_seconds: float = 1.0,
        same_window_dedup_seconds: float = 5.0,
    ) -> None:
        self._capture_fn = capture_fn
        self._debounce_seconds = debounce_seconds
        self._min_capture_gap = min_capture_gap_seconds
        self._dedup_interval = dedup_interval_seconds
        self._same_window_dedup = same_window_dedup_seconds

        self._lock = threading.Lock()
        self._debounce_timer: threading.Timer | None = None
        self._pending_trigger: (
            tuple[dict[str, Any], _EventKey, float, _EventRecord | None] | None
        ) = None

        # Tuple keys avoid delimited-string collisions. Dynamic titles are not
        # identities: the two focus notifications emitted for one app switch can
        # observe different titles even though they describe one transition.
        self._last_event_time: dict[_EventKey, _EventRecord] = {}
        self._last_capture_key: tuple[str, str] = ("", "")
        self._last_capture_monotonic: float = 0.0

    # Periodically prune entries that can no longer suppress dedup so the
    # map can't grow forever as the user visits many distinct windows.
    _PRUNE_EVERY: int = 256

    def on_event(self, raw: dict[str, Any]) -> None:
        """Watcher callback. Classifies the event and (maybe) triggers capture."""
        event_type = str(raw.get("event_type", "") or "")
        if not event_type or event_type in _SKIP_EVENTS:
            return

        bundle_id = str(raw.get("bundle_id", "") or "")
        app_name = str(raw.get("app_name", "") or "")
        window_title = str(raw.get("window_title", "") or "")
        surface_kind, surface_id = _surface_key(bundle_id=bundle_id, app_name=app_name)
        event_family = (
            "surface-transition" if event_type in _FOCUS_TRANSITION_EVENTS else event_type
        )
        dedup_key = (event_family, surface_kind, surface_id)

        now = time.monotonic()
        with self._lock:
            last = self._last_event_time.get(dedup_key)
            # Paired activation/focused-window notifications collapse, but two
            # focused-window notifications may represent distinct windows inside
            # one app. Let those reach the content gate.
            if (
                last is not None
                and now - last[0] < self._dedup_interval
                and (event_type not in _FOCUS_TRANSITION_EVENTS or event_type != last[1])
            ):
                return
            previous_event_record = last
            self._last_event_time[dedup_key] = (now, event_type)
            if len(self._last_event_time) >= self._PRUNE_EVERY:
                self._prune_event_times(now)

        trigger = {
            "event_type": event_type,
            "app_name": app_name,
            "bundle_id": bundle_id,
            "window_title": window_title,
        }
        # Preserve the attention-localization payload the watcher attaches to
        # pointer events: UserMouseClick carries details.{button, x, y, element}
        # — the cursor position plus the AX element hit-tested under the cursor.
        # This is the strongest "where is the user attending" signal for
        # AX-opaque apps (terminals like cmux) where focused_element is empty.
        # It used to be dropped here, collapsing every click to just its
        # event_type. Carry it through so the capture's `trigger` retains it.
        details = raw.get("details")
        if isinstance(details, dict) and details:
            trigger["details"] = details

        if event_type in _IMMEDIATE_EVENTS:
            self._cancel_debounce()
            self._maybe_capture(
                trigger,
                dedup_key=dedup_key,
                event_time=now,
                previous_event_record=previous_event_record,
            )
        elif event_type in _DEBOUNCED_EVENTS:
            self._schedule_debounce(
                trigger,
                dedup_key=dedup_key,
                event_time=now,
                previous_event_record=previous_event_record,
            )

    def _prune_event_times(self, now: float) -> None:
        cutoff = now - self._dedup_interval
        self._last_event_time = {
            key: reservation
            for key, reservation in self._last_event_time.items()
            if reservation[0] >= cutoff
        }

    def _schedule_debounce(
        self,
        trigger: dict[str, Any],
        *,
        dedup_key: _EventKey,
        event_time: float,
        previous_event_record: _EventRecord | None,
    ) -> None:
        with self._lock:
            if self._pending_trigger is not None:
                pending_trigger, pending_key, pending_time, pending_previous = self._pending_trigger
                self._rollback_event_reservation_locked(
                    pending_key,
                    pending_time,
                    str(pending_trigger["event_type"]),
                    pending_previous,
                )
            self._pending_trigger = (
                trigger,
                dedup_key,
                event_time,
                previous_event_record,
            )
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
            t = threading.Timer(self._debounce_seconds, self._flush_debounce)
            t.daemon = True
            self._debounce_timer = t
            t.start()

    def _cancel_debounce(self) -> None:
        with self._lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
                self._debounce_timer = None
            if self._pending_trigger is not None:
                pending_trigger, pending_key, pending_time, pending_previous = self._pending_trigger
                self._rollback_event_reservation_locked(
                    pending_key,
                    pending_time,
                    str(pending_trigger["event_type"]),
                    pending_previous,
                )
            self._pending_trigger = None

    def _flush_debounce(self) -> None:
        with self._lock:
            pending = self._pending_trigger
            self._pending_trigger = None
            self._debounce_timer = None
        if pending is not None:
            trigger, dedup_key, event_time, previous_event_record = pending
            self._maybe_capture(
                trigger,
                dedup_key=dedup_key,
                event_time=event_time,
                previous_event_record=previous_event_record,
            )

    def _rollback_event_reservation_locked(
        self,
        dedup_key: _EventKey,
        event_time: float,
        event_type: str,
        previous_event_record: _EventRecord | None,
    ) -> None:
        if self._last_event_time.get(dedup_key) == (event_time, event_type):
            if previous_event_record is None:
                self._last_event_time.pop(dedup_key, None)
            else:
                self._last_event_time[dedup_key] = previous_event_record

    def _maybe_capture(
        self,
        trigger: dict[str, Any],
        *,
        dedup_key: _EventKey,
        event_time: float,
        previous_event_record: _EventRecord | None,
    ) -> None:
        """Apply last-frame dedup + rate limit, then invoke the capture fn."""
        event_type = trigger["event_type"]
        key = _surface_key(
            bundle_id=str(trigger.get("bundle_id") or ""),
            app_name=str(trigger.get("app_name") or ""),
        )
        now = time.monotonic()
        is_focus_change = event_type in _FOCUS_TRANSITION_EVENTS

        # Decide-and-commit under the lock: this method is called from both the
        # watcher reader thread (immediate events) and the debounce Timer
        # thread, so reading then writing _last_capture_* without serialization
        # races and lets two near-simultaneous events bypass dedup/rate-limit.
        # Keep _capture_fn outside the lock so a slow callback can't stall the
        # other thread.
        with self._lock:
            if (
                not is_focus_change
                and key == self._last_capture_key
                and (now - self._last_capture_monotonic) < self._same_window_dedup
            ):
                logger.debug(
                    "capture skipped (same-window dedup <%.1fs): %s",
                    self._same_window_dedup,
                    trigger["window_title"][:40],
                )
                return

            gap = now - self._last_capture_monotonic
            if gap < self._min_capture_gap and not is_focus_change:
                logger.debug("capture skipped (rate limit %.1fs): %s", gap, event_type)
                return

            previous_capture_key = self._last_capture_key
            previous_capture_monotonic = self._last_capture_monotonic
            self._last_capture_key = key
            self._last_capture_monotonic = now

        try:
            accepted = self._capture_fn(trigger)
        except Exception as exc:  # noqa: BLE001
            logger.warning("capture callback failed: %s", exc)
            accepted = False
        if accepted is not False:
            return

        # Queue rejection (or callback failure) did not accept this source
        # occurrence. Roll back only our exact reservations: a concurrent newer
        # capture/event must never be rewound by a late callback result.
        with self._lock:
            self._rollback_event_reservation_locked(
                dedup_key,
                event_time,
                event_type,
                previous_event_record,
            )
            if self._last_capture_key == key and self._last_capture_monotonic == now:
                self._last_capture_key = previous_capture_key
                self._last_capture_monotonic = previous_capture_monotonic

    def shutdown(self) -> None:
        self._cancel_debounce()
