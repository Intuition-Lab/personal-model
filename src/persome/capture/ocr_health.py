"""Side-effect-free health model for the local OCR capture path."""

from __future__ import annotations

import platform
import sys
from dataclasses import asdict, dataclass
from typing import Literal

from ..config import CaptureConfig
from . import ocr_local, screen_recording

OCRState = Literal[
    "ready",
    "disabled",
    "disabled_by_environment",
    "runtime_unavailable",
    "models_missing",
    "permission_required",
]


@dataclass(frozen=True)
class OCRHealth:
    state: OCRState
    ready: bool
    enabled: bool
    tier: str
    runtime_available: bool
    models_available: bool
    screen_recording: Literal["granted", "denied", "not_applicable"]
    disabled_by_environment: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def inspect(cfg: CaptureConfig) -> OCRHealth:
    """Return the effective OCR state without importing Paddle or requesting TCC access."""
    enabled = bool(cfg.enable_ocr_fallback)
    tier = cfg.ocr_tier
    runtime_ready = ocr_local.runtime_available()
    models_ready = ocr_local.models_available(tier)
    env_disabled = ocr_local.disabled_by_environment()

    if sys.platform == "darwin":
        permission = "granted" if screen_recording.has_screen_recording() else "denied"
    else:
        permission = "not_applicable"

    if not enabled:
        state: OCRState = "disabled"
        detail = "disabled in capture settings; run `persome ocr setup`"
    elif env_disabled:
        state = "disabled_by_environment"
        detail = "disabled by PERSOME_DISABLE_OCR"
    elif not runtime_ready:
        state = "runtime_unavailable"
        machine = platform.machine() or "unknown architecture"
        detail = f"Paddle OCR runtime unavailable on {machine}"
    elif not models_ready:
        state = "models_missing"
        detail = f"bundled PP-OCRv6 {tier} weights are missing"
    elif permission != "granted":
        state = "permission_required"
        detail = "Screen Recording permission is required"
    else:
        state = "ready"
        detail = f"local PP-OCRv6 {tier}, isolated worker"

    return OCRHealth(
        state=state,
        ready=state == "ready",
        enabled=enabled,
        tier=tier,
        runtime_available=runtime_ready,
        models_available=models_ready,
        screen_recording=permission,
        disabled_by_environment=env_disabled,
        detail=detail,
    )
