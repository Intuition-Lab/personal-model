"""Classifies AX watcher events and triggers captures.

Inspired by Einsia-Partner's S0/S1 collector pipeline, collapsed into a single
dispatcher that writes one JSON per semantic event into the capture buffer.

Classification rules:
  AXFocusedWindowChanged   → immediate capture
  AXApplicationActivated   → short trailing capture; replaced by paired Focus
  UserMouseClick           → immediate capture
  UserTextInput            → immediate capture (Swift already debounced typing)
  AXValueChanged           → debounced capture (3s)
  AXTitleChanged           → skip (too noisy, covered by window/app events)

Additional guards:
  * Same-surface dedup: skip a non-focus-change capture if the last capture in
    the same bundle/app happened less than
    ``same_window_dedup_seconds`` ago. Focus changes always pass.
  * App activation and focused-window notifications for one surface transition
    share an event-family key. Activation waits one dedup interval; a paired
    Focus replaces it, while Focus followed by Activation keeps the Focus.
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
_PendingEvent = tuple[dict[str, Any], _EventKey, float, _EventRecord | None]
_PendingActivationEvent = tuple[
    dict[str, Any],
    _EventKey,
    float,
    _EventRecord | None,
    int,
]
_PendingActivation = tuple[_PendingActivationEvent, threading.Timer, object]


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
        self._idle = threading.Condition(self._lock)
        self._closed = False
        self._inflight_captures = 0
        self._debounce_timer: threading.Timer | None = None
        self._pending_trigger: (
            tuple[dict[str, Any], _EventKey, float, _EventRecord | None] | None
        ) = None
        self._pending_activations: dict[_EventKey, _PendingActivation] = {}

        # Tuple keys avoid delimited-string collisions. Dynamic titles are not
        # identities: the two focus notifications emitted for one app switch can
        # observe different titles even though they describe one transition.
        self._last_event_time: dict[_EventKey, _EventRecord] = {}
        self._latest_observed_strong_event: tuple[float, tuple[str, str]] | None = None
        self._latest_strong_capture_reservation: tuple[float, object] | None = None
        self._inflight_strong_tokens: set[object] = set()
        self._activation_generation = 0
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
        activation_generation: int | None = None
        with self._lock:
            if self._closed:
                return
            # Strong observations are foreground watermarks even when the
            # source occurrence is later rejected by event-family dedup. For
            # example, B click -> A activation -> duplicate B click must not
            # let A's delayed trigger label the now-current B surface.
            if event_type in _IMMEDIATE_EVENTS:
                self._latest_observed_strong_event = (
                    now,
                    (surface_kind, surface_id),
                )
            if event_type == "AXApplicationActivated":
                self._activation_generation += 1
                activation_generation = self._activation_generation
            last = self._last_event_time.get(dedup_key)
            within_dedup = last is not None and now - last[0] < self._dedup_interval
            complementary_focus = bool(
                within_dedup and event_type in _FOCUS_TRANSITION_EVENTS and event_type != last[1]
            )
            if within_dedup:
                if complementary_focus:
                    # A focused-window notification is the more specific end
                    # of an activation transition. Activation after Focus is
                    # redundant; Focus after Activation replaces its short
                    # trailing reservation below.
                    if event_type == "AXApplicationActivated":
                        return
                elif event_type not in _FOCUS_TRANSITION_EVENTS or event_type != last[1]:
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

        if event_type == "AXApplicationActivated":
            assert activation_generation is not None
            self._cancel_debounce()
            self._schedule_activation(
                trigger,
                dedup_key=dedup_key,
                event_time=now,
                previous_event_record=previous_event_record,
                activation_generation=activation_generation,
            )
        elif event_type == "AXFocusedWindowChanged":
            self._cancel_debounce()
            replaced = self._cancel_pending_activations(dedup_key)
            accepted = self._maybe_capture(
                trigger,
                dedup_key=dedup_key,
                event_time=now,
                previous_event_record=previous_event_record,
            )
            if not accepted and replaced is not None:
                self._restore_pending_activation(replaced)
        elif event_type in _IMMEDIATE_EVENTS:
            self._cancel_debounce()
            # Even a rate-limited/full-queue event proves that the foreground
            # moved. Retain a same-surface Activation as fallback on rejection,
            # but never let another surface's delayed trigger label this one.
            self._cancel_pending_activations_from_other_surfaces(
                (surface_kind, surface_id),
                event_time=now,
            )
            accepted = self._maybe_capture(
                trigger,
                dedup_key=dedup_key,
                event_time=now,
                previous_event_record=previous_event_record,
            )
            if accepted:
                self._cancel_pending_activations_through(now)
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
            if self._closed:
                self._rollback_event_reservation_locked(
                    dedup_key,
                    event_time,
                    str(trigger["event_type"]),
                    previous_event_record,
                )
                return
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

    def _arm_activation(self, pending: _PendingActivationEvent, *, delay: float) -> None:
        trigger, dedup_key, event_time, _previous, _generation = pending
        del trigger
        token = object()
        timer = threading.Timer(
            max(0.0, delay),
            self._flush_activation,
            args=(dedup_key, token),
        )
        timer.daemon = True
        self._pending_activations[dedup_key] = (pending, timer, token)
        timer.start()

    def _schedule_activation(
        self,
        trigger: dict[str, Any],
        *,
        dedup_key: _EventKey,
        event_time: float,
        previous_event_record: _EventRecord | None,
        activation_generation: int,
    ) -> None:
        pending = (
            trigger,
            dedup_key,
            event_time,
            previous_event_record,
            activation_generation,
        )
        with self._lock:
            if self._closed:
                self._rollback_event_reservation_locked(
                    dedup_key,
                    event_time,
                    str(trigger["event_type"]),
                    previous_event_record,
                )
                return
            # Capture happens after the trailing delay, not at notification
            # time. Once a newer surface transition arrives, any older pending
            # Activation would read the new screen while carrying a stale app
            # trigger and could move the session back to the wrong surface.
            for _prior_pending, prior_timer, _token in self._pending_activations.values():
                prior_timer.cancel()
            self._pending_activations.clear()
            self._arm_activation(pending, delay=self._dedup_interval)

    def _cancel_pending_activations(self, dedup_key: _EventKey) -> _PendingActivationEvent | None:
        with self._lock:
            same_surface: _PendingActivationEvent | None = None
            for key, (pending, timer, _token) in self._pending_activations.items():
                timer.cancel()
                if key == dedup_key:
                    same_surface = pending
            self._pending_activations.clear()
            return same_surface

    def _cancel_pending_activations_through(self, event_time: float) -> None:
        """Cancel delayed captures that an accepted newer event made stale."""
        with self._lock:
            stale = [
                key
                for key, (pending, _timer, _token) in self._pending_activations.items()
                if pending[2] <= event_time
            ]
            for key in stale:
                _pending, timer, _token = self._pending_activations.pop(key)
                timer.cancel()

    def _cancel_pending_activations_from_other_surfaces(
        self,
        surface: tuple[str, str],
        *,
        event_time: float,
    ) -> None:
        with self._lock:
            stale = [
                key
                for key, (pending, _timer, _token) in self._pending_activations.items()
                if key[1:] != surface and pending[2] <= event_time
            ]
            for key in stale:
                _pending, timer, _token = self._pending_activations.pop(key)
                timer.cancel()

    def _restore_pending_activation(self, pending: _PendingActivationEvent) -> None:
        _trigger, dedup_key, event_time, _previous, _generation = pending
        with self._lock:
            if self._closed:
                return
            elapsed = max(0.0, time.monotonic() - event_time)
            self._arm_activation(
                pending,
                delay=max(0.0, self._dedup_interval - elapsed),
            )

    def _flush_activation(self, dedup_key: _EventKey, token: object | None = None) -> None:
        with self._lock:
            reserved = self._pending_activations.get(dedup_key)
            if reserved is None:
                return
            pending, _timer, current_token = reserved
            if token is not None and token is not current_token:
                return
            self._pending_activations.pop(dedup_key, None)
        trigger, key, reserved_at, previous, activation_generation = pending
        self._maybe_capture(
            trigger,
            dedup_key=key,
            event_time=reserved_at,
            previous_event_record=previous,
            activation_generation=activation_generation,
        )

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
        activation_generation: int | None = None,
    ) -> bool:
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
            if self._closed:
                self._rollback_event_reservation_locked(
                    dedup_key,
                    event_time,
                    event_type,
                    previous_event_record,
                )
                return False
            if event_type == "AXApplicationActivated":
                while True:
                    stale_generation = bool(
                        activation_generation is not None
                        and activation_generation < self._activation_generation
                    )
                    latest_observed = self._latest_observed_strong_event
                    stale_for_surface = bool(
                        latest_observed is not None
                        and latest_observed[0] > event_time
                        and latest_observed[1] != key
                    )
                    latest_reservation = self._latest_strong_capture_reservation
                    stale_for_capture = bool(
                        latest_reservation is not None and latest_reservation[0] > event_time
                    )
                    if stale_generation or stale_for_surface:
                        self._rollback_event_reservation_locked(
                            dedup_key,
                            event_time,
                            event_type,
                            previous_event_record,
                        )
                        return False
                    if not stale_for_capture:
                        break
                    assert latest_reservation is not None
                    if latest_reservation[1] in self._inflight_strong_tokens:
                        # A same-surface strong callback is still provisional.
                        # Wait for its exact accept/reject rollback before
                        # deciding whether it supersedes this Activation.
                        self._idle.wait()
                        if self._closed:
                            self._rollback_event_reservation_locked(
                                dedup_key,
                                event_time,
                                event_type,
                                previous_event_record,
                            )
                            return False
                        continue
                    self._rollback_event_reservation_locked(
                        dedup_key,
                        event_time,
                        event_type,
                        previous_event_record,
                    )
                    return False
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
                return False

            gap = now - self._last_capture_monotonic
            if gap < self._min_capture_gap and not is_focus_change:
                logger.debug("capture skipped (rate limit %.1fs): %s", gap, event_type)
                return False

            previous_capture_key = self._last_capture_key
            previous_capture_monotonic = self._last_capture_monotonic
            self._last_capture_key = key
            self._last_capture_monotonic = now
            previous_strong_reservation = self._latest_strong_capture_reservation
            strong_token: object | None = None
            if (
                event_type in _IMMEDIATE_EVENTS
                and event_type != "AXApplicationActivated"
                and (
                    previous_strong_reservation is None
                    or event_time >= previous_strong_reservation[0]
                )
            ):
                strong_token = object()
                self._latest_strong_capture_reservation = (event_time, strong_token)
                self._inflight_strong_tokens.add(strong_token)
            self._inflight_captures += 1

        accepted = False
        try:
            try:
                accepted = self._capture_fn(trigger) is not False
            except Exception as exc:  # noqa: BLE001
                logger.warning("capture callback failed: %s", exc)
        finally:
            # Queue rejection (or callback failure) did not accept this source
            # occurrence. Roll back only our exact reservations: a concurrent
            # newer capture/event must never be rewound by a late callback.
            with self._idle:
                if not accepted:
                    self._rollback_event_reservation_locked(
                        dedup_key,
                        event_time,
                        event_type,
                        previous_event_record,
                    )
                    if self._last_capture_key == key and self._last_capture_monotonic == now:
                        self._last_capture_key = previous_capture_key
                        self._last_capture_monotonic = previous_capture_monotonic
                    if (
                        strong_token is not None
                        and self._latest_strong_capture_reservation is not None
                        and self._latest_strong_capture_reservation[1] is strong_token
                    ):
                        self._latest_strong_capture_reservation = previous_strong_reservation
                if strong_token is not None:
                    self._inflight_strong_tokens.discard(strong_token)
                self._inflight_captures -= 1
                self._idle.notify_all()
        return accepted

    def shutdown(self) -> None:
        with self._idle:
            self._closed = True
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
            for _pending, timer, _token in self._pending_activations.values():
                timer.cancel()
            self._pending_activations.clear()
            while self._inflight_captures:
                self._idle.wait()
