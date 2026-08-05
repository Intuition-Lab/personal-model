"""Tests for `persome stats` — local usage summary and consented export."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from typer.testing import CliRunner

from persome import __version__, cli
from persome.store import fts
from persome.store import tool_ticks as tt


def _seed(days_ago_counts: dict[int, int]) -> None:
    """Insert ``count`` search ticks ``days_ago`` days before today."""
    today = date.today()
    with fts.cursor() as conn:
        for days_ago, count in days_ago_counts.items():
            day = (today - timedelta(days=days_ago)).isoformat()
            for i in range(count):
                tt.record_tick(conn, ts=f"{day}T10:{i:02d}:00+08:00", tool="search")


def test_stats_show_summarizes_window(ac_root: Path) -> None:
    _seed({0: 2, 1: 3, 40: 5})  # the 40-days-ago ticks fall outside --days 30
    runner = CliRunner()
    result = runner.invoke(cli.app, ["stats", "show", "--days", "30"])
    assert result.exit_code == 0, result.output
    assert "tool calls, last 30d: 5" in result.output
    assert "active days: 2/30" in result.output
    assert "search" in result.output


def test_stats_export_payload_is_bare_daily_counts(ac_root: Path) -> None:
    _seed({0: 2, 1: 3})
    runner = CliRunner()
    result = runner.invoke(cli.app, ["stats", "export", "--days", "7"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["persome_version"] == __version__
    assert payload["window_days"] == 7
    assert payload["days_active"] == 2
    # Exactly one integer per day, zero-days explicit, newest day included.
    assert len(payload["daily_calls"]) == 7
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    assert payload["daily_calls"][today] == 2
    assert payload["daily_calls"][yesterday] == 3
    assert sum(payload["daily_calls"].values()) == 5
    # Privacy contract: no tool names, no client names anywhere in the export.
    assert "by_tool" not in payload
    assert "by_client" not in payload
    assert "search" not in result.output


def test_stats_export_writes_file(ac_root: Path, tmp_path: Path) -> None:
    _seed({0: 1})
    out = tmp_path / "usage.json"
    runner = CliRunner()
    result = runner.invoke(cli.app, ["stats", "export", "--days", "3", "--out", str(out)])
    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["days_active"] == 1
    assert len(payload["daily_calls"]) == 3


def test_stats_export_empty_db(ac_root: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli.app, ["stats", "export", "--days", "14"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["first_recorded"] is None
    assert payload["days_active"] == 0
    assert sum(payload["daily_calls"].values()) == 0
