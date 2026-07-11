"""Tests for the interactive ``persome chat`` entry point."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from persome import chat as chat_mod
from persome import cli, paths


def test_chat_loads_runtime_env_before_building_agent(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    paths.env_file().write_text(
        "ANTHROPIC_API_KEY=sk-test-from-env-file\n"
        "ANTHROPIC_BASE_URL=https://gateway.example/anthropic\n"
    )

    seen: dict[str, str | None] = {}

    def capture_env(_cfg: object) -> None:
        seen["api_key"] = os.environ.get("ANTHROPIC_API_KEY")
        seen["base_url"] = os.environ.get("ANTHROPIC_BASE_URL")

    monkeypatch.setattr(chat_mod, "run_chat_sync", capture_env)

    result = CliRunner().invoke(cli.app, ["chat"])

    assert result.exit_code == 0, result.output
    assert seen == {
        "api_key": "sk-test-from-env-file",
        "base_url": "https://gateway.example/anthropic",
    }
