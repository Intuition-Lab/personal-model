"""S0 AX event-family and surface admission policy."""

from __future__ import annotations

import threading

import pytest

from persome.capture import event_dispatcher as dispatcher_mod
from persome.capture import scheduler
from persome.config import load as load_config


def _event(event_type: str, *, bundle: str = "com.example.app", title: str = "A") -> dict:
    return {
        "event_type": event_type,
        "app_name": "Example",
        "bundle_id": bundle,
        "window_title": title,
    }


def _clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [100.0]
    monkeypatch.setattr(dispatcher_mod.time, "monotonic", lambda: now[0])
    return now


def test_activation_waits_for_and_is_replaced_by_focused_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _clock(monkeypatch)
    captured: list[dict] = []
    dispatcher = dispatcher_mod.EventDispatcher(captured.append, min_capture_gap_seconds=0)

    dispatcher.on_event(_event("AXApplicationActivated", title="Loading"))
    assert captured == []
    now[0] += 0.1
    dispatcher.on_event(_event("AXFocusedWindowChanged", title="Ready — 42%"))

    assert [trigger["event_type"] for trigger in captured] == ["AXFocusedWindowChanged"]


def test_solo_activation_flushes_after_pair_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _clock(monkeypatch)
    captured: list[dict] = []
    dispatcher = dispatcher_mod.EventDispatcher(captured.append, min_capture_gap_seconds=0)
    event = _event("AXApplicationActivated", title="Ready")
    dispatcher.on_event(event)

    assert captured == []
    key = ("surface-transition", "bundle", event["bundle_id"])
    dispatcher._flush_activation(key)  # noqa: SLF001

    assert [trigger["event_type"] for trigger in captured] == ["AXApplicationActivated"]


def test_focus_then_activation_emits_focus_once(monkeypatch: pytest.MonkeyPatch) -> None:
    now = _clock(monkeypatch)
    captured: list[dict] = []
    dispatcher = dispatcher_mod.EventDispatcher(captured.append, min_capture_gap_seconds=0)

    dispatcher.on_event(_event("AXFocusedWindowChanged", title="Ready"))
    now[0] += 0.1
    dispatcher.on_event(_event("AXApplicationActivated", title="Ready"))

    assert [trigger["event_type"] for trigger in captured] == ["AXFocusedWindowChanged"]


def test_same_type_focus_notifications_reach_content_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _clock(monkeypatch)
    captured: list[dict] = []
    dispatcher = dispatcher_mod.EventDispatcher(captured.append, min_capture_gap_seconds=0)

    dispatcher.on_event(_event("AXFocusedWindowChanged", title="Window A"))
    now[0] += 0.1
    dispatcher.on_event(_event("AXFocusedWindowChanged", title="Window B"))

    assert [trigger["window_title"] for trigger in captured] == ["Window A", "Window B"]


def test_new_surface_focus_cancels_stale_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _clock(monkeypatch)
    captured: list[dict] = []
    dispatcher = dispatcher_mod.EventDispatcher(captured.append, min_capture_gap_seconds=0)

    dispatcher.on_event(_event("AXApplicationActivated", bundle="com.example.a"))
    now[0] += 0.1
    dispatcher.on_event(_event("AXFocusedWindowChanged", bundle="com.example.b"))

    key = ("surface-transition", "bundle", "com.example.a")
    dispatcher._flush_activation(key)  # noqa: SLF001
    assert [(item["event_type"], item["bundle_id"]) for item in captured] == [
        ("AXFocusedWindowChanged", "com.example.b")
    ]


def test_new_surface_click_cancels_stale_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _clock(monkeypatch)
    captured: list[dict] = []
    dispatcher = dispatcher_mod.EventDispatcher(captured.append, min_capture_gap_seconds=0)

    dispatcher.on_event(_event("AXApplicationActivated", bundle="com.example.a"))
    now[0] += 0.1
    dispatcher.on_event(_event("UserMouseClick", bundle="com.example.b"))

    key = ("surface-transition", "bundle", "com.example.a")
    dispatcher._flush_activation(key)  # noqa: SLF001
    assert [(item["event_type"], item["bundle_id"]) for item in captured] == [
        ("UserMouseClick", "com.example.b")
    ]


def test_rejected_new_surface_click_still_invalidates_stale_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _clock(monkeypatch)
    attempts: list[tuple[str, str]] = []

    def reject(trigger: dict) -> bool:
        attempts.append((trigger["event_type"], trigger["bundle_id"]))
        return False

    dispatcher = dispatcher_mod.EventDispatcher(reject, min_capture_gap_seconds=0)
    dispatcher.on_event(_event("AXApplicationActivated", bundle="com.example.a"))
    now[0] += 0.1
    dispatcher.on_event(_event("UserMouseClick", bundle="com.example.b"))

    key = ("surface-transition", "bundle", "com.example.a")
    dispatcher._flush_activation(key)  # noqa: SLF001
    assert attempts == [("UserMouseClick", "com.example.b")]


def test_deduped_strong_event_still_invalidates_other_surface_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _clock(monkeypatch)
    captured: list[tuple[str, str]] = []
    dispatcher = dispatcher_mod.EventDispatcher(
        lambda trigger: captured.append((trigger["event_type"], trigger["bundle_id"])),
        min_capture_gap_seconds=0,
    )

    dispatcher.on_event(_event("UserMouseClick", bundle="com.example.b"))
    now[0] += 0.1
    dispatcher.on_event(_event("AXApplicationActivated", bundle="com.example.a"))
    now[0] += 0.1
    dispatcher.on_event(_event("UserMouseClick", bundle="com.example.b"))

    key = ("surface-transition", "bundle", "com.example.a")
    dispatcher._flush_activation(key)  # noqa: SLF001
    assert captured == [("UserMouseClick", "com.example.b")]


def test_activation_rechecks_strong_watermark_after_timer_pop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _clock(monkeypatch)
    captured: list[tuple[str, str]] = []
    dispatcher = dispatcher_mod.EventDispatcher(
        lambda trigger: captured.append((trigger["event_type"], trigger["bundle_id"])),
        min_capture_gap_seconds=0,
    )
    dispatcher.on_event(_event("AXApplicationActivated", bundle="com.example.a"))
    key = ("surface-transition", "bundle", "com.example.a")
    with dispatcher._lock:  # noqa: SLF001
        pending, timer, _token = dispatcher._pending_activations.pop(key)  # noqa: SLF001
        timer.cancel()

    now[0] += 0.1
    dispatcher.on_event(_event("UserMouseClick", bundle="com.example.b"))
    trigger, dedup_key, event_time, previous, activation_generation = pending
    assert (
        dispatcher._maybe_capture(  # noqa: SLF001
            trigger,
            dedup_key=dedup_key,
            event_time=event_time,
            previous_event_record=previous,
            activation_generation=activation_generation,
        )
        is False
    )
    assert captured == [("UserMouseClick", "com.example.b")]


def test_activation_sees_same_surface_strong_reservation_before_callback_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _clock(monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    captured: list[str] = []

    def blocking_click(trigger: dict) -> bool:
        captured.append(trigger["event_type"])
        if trigger["event_type"] == "UserMouseClick":
            entered.set()
            assert release.wait(timeout=2)
        return True

    dispatcher = dispatcher_mod.EventDispatcher(blocking_click, min_capture_gap_seconds=0)
    dispatcher.on_event(_event("AXApplicationActivated"))
    key = ("surface-transition", "bundle", "com.example.app")
    with dispatcher._lock:  # noqa: SLF001
        pending, timer, _token = dispatcher._pending_activations.pop(key)  # noqa: SLF001
        timer.cancel()

    now[0] += 0.1
    click = threading.Thread(target=dispatcher.on_event, args=(_event("UserMouseClick"),))
    click.start()
    assert entered.wait(timeout=2)
    trigger, dedup_key, event_time, previous, activation_generation = pending
    activation_result: list[bool] = []

    def run_activation() -> None:
        activation_result.append(
            dispatcher._maybe_capture(  # noqa: SLF001
                trigger,
                dedup_key=dedup_key,
                event_time=event_time,
                previous_event_record=previous,
                activation_generation=activation_generation,
            )
        )

    activation = threading.Thread(target=run_activation)
    activation.start()
    assert activation_result == []
    release.set()
    click.join(timeout=2)
    activation.join(timeout=2)
    assert not click.is_alive()
    assert not activation.is_alive()
    assert activation_result == [False]
    assert captured == ["UserMouseClick"]


def test_activation_waits_for_same_surface_strong_rejection_before_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _clock(monkeypatch)
    click_entered = threading.Event()
    release_click = threading.Event()
    activation_done = threading.Event()
    attempts: list[str] = []

    def reject_blocking_click(trigger: dict) -> bool:
        attempts.append(trigger["event_type"])
        if trigger["event_type"] == "UserMouseClick":
            click_entered.set()
            assert release_click.wait(timeout=2)
            return False
        return True

    dispatcher = dispatcher_mod.EventDispatcher(
        reject_blocking_click,
        min_capture_gap_seconds=0,
    )
    dispatcher.on_event(_event("AXApplicationActivated"))
    key = ("surface-transition", "bundle", "com.example.app")
    with dispatcher._lock:  # noqa: SLF001
        pending, timer, _token = dispatcher._pending_activations.pop(key)  # noqa: SLF001
        timer.cancel()

    now[0] += 0.1
    click = threading.Thread(target=dispatcher.on_event, args=(_event("UserMouseClick"),))
    click.start()
    assert click_entered.wait(timeout=2)

    trigger, dedup_key, event_time, previous, activation_generation = pending

    def run_activation() -> None:
        dispatcher._maybe_capture(  # noqa: SLF001
            trigger,
            dedup_key=dedup_key,
            event_time=event_time,
            previous_event_record=previous,
            activation_generation=activation_generation,
        )
        activation_done.set()

    activation = threading.Thread(target=run_activation)
    activation.start()
    assert activation_done.wait(timeout=0.05) is False
    release_click.set()
    click.join(timeout=2)
    activation.join(timeout=2)

    assert not click.is_alive()
    assert not activation.is_alive()
    assert attempts == ["UserMouseClick", "AXApplicationActivated"]


def test_new_same_surface_activation_invalidates_timer_already_popped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _clock(monkeypatch)
    captured: list[str] = []
    dispatcher = dispatcher_mod.EventDispatcher(
        lambda trigger: captured.append(trigger["window_title"]),
        min_capture_gap_seconds=0,
    )
    dispatcher.on_event(_event("AXApplicationActivated", title="First"))
    key = ("surface-transition", "bundle", "com.example.app")
    with dispatcher._lock:  # noqa: SLF001
        pending, timer, _token = dispatcher._pending_activations.pop(key)  # noqa: SLF001
        timer.cancel()

    now[0] += 0.1
    dispatcher.on_event(_event("AXApplicationActivated", title="Second"))
    trigger, dedup_key, event_time, previous, activation_generation = pending
    assert (
        dispatcher._maybe_capture(  # noqa: SLF001
            trigger,
            dedup_key=dedup_key,
            event_time=event_time,
            previous_event_record=previous,
            activation_generation=activation_generation,
        )
        is False
    )
    dispatcher._flush_activation(key)  # noqa: SLF001
    assert captured == ["Second"]


def test_stale_same_timestamp_timer_cannot_flush_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clock(monkeypatch)
    captured: list[dict] = []
    dispatcher = dispatcher_mod.EventDispatcher(captured.append, min_capture_gap_seconds=0)
    key = ("surface-transition", "bundle", "com.example.app")

    dispatcher.on_event(_event("AXApplicationActivated", title="First"))
    first_token = dispatcher._pending_activations[key][2]  # noqa: SLF001
    dispatcher.on_event(_event("AXApplicationActivated", title="Second"))
    second_token = dispatcher._pending_activations[key][2]  # noqa: SLF001

    assert first_token is not second_token
    dispatcher._flush_activation(key, first_token)  # noqa: SLF001
    assert captured == []
    dispatcher._flush_activation(key, second_token)  # noqa: SLF001
    assert [item["window_title"] for item in captured] == ["Second"]


def test_nonfocus_dynamic_title_cannot_bypass_same_surface_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _clock(monkeypatch)
    captured: list[dict] = []
    dispatcher = dispatcher_mod.EventDispatcher(
        captured.append,
        min_capture_gap_seconds=0,
        dedup_interval_seconds=1,
        same_window_dedup_seconds=5,
    )

    dispatcher.on_event(_event("UserMouseClick", title="Build 1%"))
    now[0] += 1.1  # past event-key dedup, still inside same-surface window
    dispatcher.on_event(_event("UserMouseClick", title="Build 99%"))

    assert len(captured) == 1


def test_rejected_focus_reservation_allows_complementary_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _clock(monkeypatch)
    attempts: list[str] = []

    def reject_then_accept(trigger: dict) -> bool:
        attempts.append(trigger["event_type"])
        return len(attempts) > 1

    dispatcher = dispatcher_mod.EventDispatcher(reject_then_accept, min_capture_gap_seconds=0)
    activation = _event("AXApplicationActivated")
    dispatcher.on_event(activation)
    now[0] += 0.1
    dispatcher.on_event(_event("AXFocusedWindowChanged"))
    key = ("surface-transition", "bundle", activation["bundle_id"])
    dispatcher._flush_activation(key)  # noqa: SLF001

    assert attempts == ["AXFocusedWindowChanged", "AXApplicationActivated"]


def test_legacy_none_callback_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    now = _clock(monkeypatch)
    captured: list[dict] = []
    dispatcher = dispatcher_mod.EventDispatcher(captured.append, min_capture_gap_seconds=0)

    dispatcher.on_event(_event("AXFocusedWindowChanged"))
    now[0] += 0.1
    dispatcher.on_event(_event("AXApplicationActivated"))

    assert len(captured) == 1


def test_queue_rejected_focus_falls_back_to_pending_activation(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = _clock(monkeypatch)
    runner = scheduler._CaptureRunner(load_config().capture, provider=None)
    for index in range(runner._MAX_PENDING):  # noqa: SLF001
        runner._queue.put_nowait({"filler": index})  # noqa: SLF001
    dispatcher = dispatcher_mod.EventDispatcher(runner.run_threaded, min_capture_gap_seconds=0)

    activation = _event("AXApplicationActivated")
    dispatcher.on_event(activation)
    now[0] += 0.1
    dispatcher.on_event(_event("AXFocusedWindowChanged"))
    assert runner._queue.qsize() == runner._MAX_PENDING  # noqa: SLF001

    runner._queue.get_nowait()  # noqa: SLF001
    key = ("surface-transition", "bundle", activation["bundle_id"])
    dispatcher._flush_activation(key)  # noqa: SLF001

    assert runner._queue.qsize() == runner._MAX_PENDING  # noqa: SLF001
    queued = list(runner._queue.queue)  # noqa: SLF001
    assert queued[-1]["event_type"] == "AXApplicationActivated"


def test_shutdown_waits_for_activation_capture_already_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clock(monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    shutdown_done = threading.Event()

    def blocking_capture(_trigger: dict) -> None:
        entered.set()
        assert release.wait(timeout=2)

    dispatcher = dispatcher_mod.EventDispatcher(blocking_capture, min_capture_gap_seconds=0)
    event = _event("AXApplicationActivated")
    dispatcher.on_event(event)
    key = ("surface-transition", "bundle", event["bundle_id"])
    flush = threading.Thread(target=dispatcher._flush_activation, args=(key,))  # noqa: SLF001
    flush.start()
    assert entered.wait(timeout=2)

    shutdown = threading.Thread(
        target=lambda: (dispatcher.shutdown(), shutdown_done.set()),
    )
    shutdown.start()
    assert shutdown_done.wait(timeout=0.05) is False

    release.set()
    flush.join(timeout=2)
    shutdown.join(timeout=2)
    assert shutdown_done.is_set()
    assert not flush.is_alive()
    assert not shutdown.is_alive()
