"""Persistence helpers for local OCR onboarding."""

from __future__ import annotations

from pathlib import Path

from . import paths

VALID_TIERS = ("tiny", "small", "medium")


def save_ocr_config(*, enabled: bool, tier: str, config_path: Path) -> None:
    """Update only the OCR fields while preserving the rest of config.toml."""
    import tomlkit
    from tomlkit.items import Table

    if tier not in VALID_TIERS:
        raise ValueError(f"unsupported OCR tier: {tier}")
    if config_path.is_symlink():
        raise RuntimeError(f"config file must not be a symlink: {config_path}")

    if config_path.exists():
        document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    else:
        document = tomlkit.document()

    capture = document.get("capture")
    if not isinstance(capture, Table):
        capture = tomlkit.table()
        document["capture"] = capture
    capture["enable_ocr_fallback"] = enabled
    capture["ocr_tier"] = tier
    capture["ocr_structured"] = True

    paths.atomic_write_private_text(config_path, tomlkit.dumps(document))
