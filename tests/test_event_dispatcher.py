"""S0 AX event-family and surface admission policy."""

from __future__ import annotations

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


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("AXApplicationActivated", "AXFocusedWindowChanged"),
        ("AXFocusedWindowChanged", "AXApplicationActivated"),
    ],
)
def test_focus_transition_collapses_complementary_notifications(
    monkeypatch: pytest.MonkeyPatch,
    first: str,
    second: str,
) -> None:
    now = _clock(monkeypatch)
    captured: list[dict] = []
    dispatcher = dispatcher_mod.EventDispatcher(captured.append, min_capture_gap_seconds=0)

    dispatcher.on_event(_event(first, title="Loading"))
    now[0] += 0.1
    dispatcher.on_event(_event(second, title="Ready — 42%"))

    assert [trigger["event_type"] for trigger in captured] == [first]


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


def test_focus_transition_different_surfaces_are_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _clock(monkeypatch)
    captured: list[dict] = []
    dispatcher = dispatcher_mod.EventDispatcher(captured.append, min_capture_gap_seconds=0)

    dispatcher.on_event(_event("AXApplicationActivated", bundle="com.example.a"))
    now[0] += 0.1
    dispatcher.on_event(_event("AXFocusedWindowChanged", bundle="com.example.b"))

    assert len(captured) == 2


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
    dispatcher.on_event(_event("AXApplicationActivated"))
    now[0] += 0.1
    dispatcher.on_event(_event("AXFocusedWindowChanged"))

    assert attempts == ["AXApplicationActivated", "AXFocusedWindowChanged"]


def test_legacy_none_callback_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    now = _clock(monkeypatch)
    captured: list[dict] = []
    dispatcher = dispatcher_mod.EventDispatcher(captured.append, min_capture_gap_seconds=0)

    dispatcher.on_event(_event("AXApplicationActivated"))
    now[0] += 0.1
    dispatcher.on_event(_event("AXFocusedWindowChanged"))

    assert len(captured) == 1


def test_queue_rejected_focus_allows_complementary_notification(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = _clock(monkeypatch)
    runner = scheduler._CaptureRunner(load_config().capture, provider=None)
    for index in range(runner._MAX_PENDING):  # noqa: SLF001
        runner._queue.put_nowait({"filler": index})  # noqa: SLF001
    dispatcher = dispatcher_mod.EventDispatcher(runner.run_threaded, min_capture_gap_seconds=0)

    dispatcher.on_event(_event("AXApplicationActivated"))
    runner._queue.get_nowait()  # noqa: SLF001
    now[0] += 0.1
    dispatcher.on_event(_event("AXFocusedWindowChanged"))

    assert runner._queue.qsize() == runner._MAX_PENDING  # noqa: SLF001
    queued = list(runner._queue.queue)  # noqa: SLF001
    assert queued[-1]["event_type"] == "AXFocusedWindowChanged"
