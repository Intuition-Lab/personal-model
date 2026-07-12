"""Recovery-safe capture-index reconciliation CLI tests."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from persome import cli, paths
from persome.store import fts


def _seed_snapshot_only_capture() -> None:
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id="snapshot-only",
            timestamp="2026-06-01T00:00:00+00:00",
            app_name="Archive",
            bundle_id="com.persome.archive",
            window_title="Older snapshot capture",
            focused_role="AXTextArea",
            focused_value="historical",
            visible_text="historical snapshot evidence",
            url="https://example.test/archive",
        )


def _write_buffer_capture() -> Path:
    target = paths.capture_buffer_dir() / "buffer-new.json"
    target.write_text(
        json.dumps(
            {
                "timestamp": "2026-07-12T00:00:00+00:00",
                "window_meta": {
                    "app_name": "Browser",
                    "bundle_id": "com.persome.browser",
                    "title": "New buffer capture",
                },
                "focused_element": {"role": "AXTextField", "value": "fresh"},
                "visible_text": "fresh retained buffer evidence",
                "url": "https://example.test/fresh",
            }
        ),
        encoding="utf-8",
    )
    return target


def test_rebuild_captures_merge_preserves_snapshot_rows_and_upserts_buffer(ac_root: Path) -> None:
    _seed_snapshot_only_capture()
    _write_buffer_capture()

    result = CliRunner().invoke(cli.app, ["rebuild-captures-index", "--merge"])

    assert result.exit_code == 0, result.output
    assert "Captures index merged" in result.output
    with fts.cursor() as conn:
        ids = {row[0] for row in conn.execute("SELECT id FROM captures").fetchall()}
        historical_hits = fts.search_captures(conn, query="historical", limit=5)
        fresh_hits = fts.search_captures(conn, query="fresh", limit=5)
    assert ids == {"snapshot-only", "buffer-new"}
    assert [hit.id for hit in historical_hits] == ["snapshot-only"]
    assert [hit.id for hit in fresh_hits] == ["buffer-new"]


def test_rebuild_captures_default_remains_exact_buffer_reconciliation(ac_root: Path) -> None:
    _seed_snapshot_only_capture()
    _write_buffer_capture()

    result = CliRunner().invoke(cli.app, ["rebuild-captures-index"])

    assert result.exit_code == 0, result.output
    assert "Captures index rebuilt" in result.output
    with fts.cursor() as conn:
        ids = {row[0] for row in conn.execute("SELECT id FROM captures").fetchall()}
    assert ids == {"buffer-new"}
