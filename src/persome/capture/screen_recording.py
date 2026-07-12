"""macOS Screen-Recording (kTCCServiceScreenCapture) permission helpers.

Without this permission, `mss` / `CGDisplayCreateImage` silently return ONLY the
desktop wallpaper — every other app's window is blanked by the OS — so a daemon that
captures screenshots stores useless wallpaper frames. A launchd background process
calling the capture API never gets prompted, so the permission must be *requested*
(which also registers the binary, here ``Persome Backend``, in the Screen Recording list
so the user can toggle it on). Only explicit onboarding/setup actions call the
request function; ordinary Runtime startup uses the preflight check only.

We call CoreGraphics directly via ctypes — no pyobjc/Quartz dependency to bundle:
- ``CGPreflightScreenCaptureAccess()`` → has the permission already been granted?
- ``CGRequestScreenCaptureAccess()`` → register + prompt (idempotent).

Non-Darwin hosts treat the permission as not applicable. On macOS, an
unresolvable CoreGraphics probe fails closed so onboarding can never claim a
permission it did not actually prove.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys

from ..logger import get

logger = get("persome.capture.screen_recording")

_cg: ctypes.CDLL | None = None
_resolved = False


def _coregraphics() -> ctypes.CDLL | None:
    global _cg, _resolved
    if _resolved:
        return _cg
    _resolved = True
    if sys.platform != "darwin":
        return None
    try:
        path = (
            ctypes.util.find_library("CoreGraphics")
            or "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
        lib = ctypes.CDLL(path)
        lib.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        lib.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
        _cg = lib
    except Exception as exc:  # noqa: BLE001 — surfaced as denied on macOS
        logger.debug("CoreGraphics unavailable for screen-recording check: %s", exc)
        _cg = None
    return _cg


def has_screen_recording() -> bool:
    """Whether Screen Recording is granted (fail-closed on macOS)."""
    lib = _coregraphics()
    if lib is None:
        return sys.platform != "darwin"
    try:
        return bool(lib.CGPreflightScreenCaptureAccess())
    except Exception as exc:  # noqa: BLE001
        logger.debug("CGPreflightScreenCaptureAccess failed: %s", exc)
        return sys.platform != "darwin"


def request_screen_recording() -> bool:
    """Register this process (``Persome Backend``) in the Screen Recording list + prompt.

    Returns whether access is granted *now* (usually False on first call — the user
    still has to flip the toggle, but the binary now appears in System Settings).
    This may show a system dialog and must only be called after an explicit
    user-facing confirmation, never during ordinary Runtime startup.
    """
    lib = _coregraphics()
    if lib is None:
        return sys.platform != "darwin"
    try:
        granted = bool(lib.CGRequestScreenCaptureAccess())
        if granted:
            logger.info("Screen Recording permission granted")
        else:
            logger.warning(
                "Screen Recording NOT granted — screenshots will be wallpaper-only until "
                "the user enables 'Persome Backend' under System Settings → Privacy & Security "
                "→ Screen Recording, then restarts Persome"
            )
        return granted
    except Exception as exc:  # noqa: BLE001
        logger.debug("CGRequestScreenCaptureAccess failed: %s", exc)
        return False
