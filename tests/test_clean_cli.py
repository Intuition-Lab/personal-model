"""Personal-data deletion must cover canonical, projected, and exported state."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from persome import cli, paths
from persome.evomem.models import MemoryLayer, MemoryNode
from persome.evomem.store import NodeStore
from persome.store import capture_content_receipts, fts, schema_faces
from persome.store import entries as entries_mod


def _seed_capture() -> None:
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id="synthetic-capture",
            timestamp="2026-07-10T08:00:00+00:00",
            app_name="TestApp",
            bundle_id="test.app",
            window_title="Synthetic",
            focused_role="AXTextArea",
            focused_value="synthetic text",
            visible_text="synthetic text",
            url="",
        )


def _seed_capture_receipt() -> None:
    with fts.cursor() as conn:
        capture_content_receipts.record_success(
            conn,
            fingerprint="f" * 64,
            capture_id="synthetic-capture",
            committed_at="2026-07-10T08:00:00+00:00",
        )


def _copy_live_index(target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with fts.cursor() as source:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
    target.chmod(0o600)


def _seed_model() -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name="project-synthetic.md",
            description="Synthetic memory",
            tags=["synthetic"],
        )
        entry_id = entries_mod.append_entry(
            conn,
            name="project-synthetic.md",
            content="Synthetic personal-model fact.",
            tags=["synthetic"],
        )
        schema_faces.upsert_root_with_receipt(
            conn,
            receipt=schema_faces.make_input_receipt(
                producer="root_synthesis",
                sampled_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
                input_value={"members": [entry_id], "profile": []},
            ),
            signature="Synthetic root.",
            members=[entry_id],
            anchors=["self"],
        )
    NodeStore().save(
        MemoryNode(
            node_id=entry_id,
            content="Synthetic personal-model fact.",
            layer=MemoryLayer.L2_FACT,
            file_name="project-synthetic.md",
            valid_from="2026-07-10T08:00:00+00:00",
            gmt_created=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        )
    )


def test_clean_captures_removes_files_and_index_rows(ac_root) -> None:
    _seed_capture()
    _seed_capture_receipt()
    capture_file = paths.capture_buffer_dir() / "synthetic.json"
    capture_file.write_text("{}")
    snapshot = paths.backup_dir() / "evo-20260710.db"
    quarantine = paths.root() / "index.db.corrupt.capture-clean-test"
    _copy_live_index(snapshot)
    _copy_live_index(quarantine)

    assert cli._clean_captures() == (1, 1)
    assert not capture_file.exists()
    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM capture_content_receipts").fetchone()[0] == 0
    for database_copy in (snapshot, quarantine):
        with sqlite3.connect(database_copy) as conn:
            assert conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM capture_content_receipts").fetchone()[0] == 0


@pytest.mark.parametrize("merge", [False, True])
def test_rebuild_captures_receipt_boundary(
    ac_root: Path,
    merge: bool,
) -> None:
    _seed_capture()
    _seed_capture_receipt()
    args = ["rebuild-captures-index"] + (["--merge"] if merge else [])

    result = CliRunner().invoke(cli.app, args)

    assert result.exit_code == 0, result.output
    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM capture_content_receipts").fetchone()[0] == 0


@pytest.mark.parametrize("merge", [False, True])
def test_capture_rebuild_rolls_back_receipt_reset_on_failure(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    merge: bool,
) -> None:
    _seed_capture()
    _seed_capture_receipt()
    capture_file = paths.capture_buffer_dir() / "replacement.json"
    capture_file.write_text(
        json.dumps(
            {
                "timestamp": "2026-07-10T08:01:00+00:00",
                "window_meta": {
                    "app_name": "TestApp",
                    "bundle_id": "test.app",
                    "title": "Replacement",
                },
                "focused_element": {"role": "AXTextArea", "value": "replacement"},
                "visible_text": "replacement",
                "url": "",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        fts,
        "insert_capture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    args = ["rebuild-captures-index"] + (["--merge"] if merge else [])
    result = CliRunner().invoke(cli.app, args)

    assert result.exit_code != 0
    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM capture_content_receipts").fetchone()[0] == 1


def test_clean_memory_removes_canonical_model_exports_and_backups(ac_root, monkeypatch) -> None:
    _seed_capture()
    _seed_capture_receipt()
    _seed_model()
    paths.exports_dir().mkdir()
    (paths.exports_dir() / "model.json").write_text("{}")
    paths.backup_dir().mkdir()
    (paths.backup_dir() / "evo.db").write_text("synthetic")
    paths.human_file().write_text("# HUMAN.md\n")
    paths.model_build_manifest().write_text("{}")

    real_remove = cli._remove_path
    removed_inside_gate: list[bool] = []

    def guarded_remove(path):  # noqa: ANN001, ANN202
        removed_inside_gate.append(fts._in_exclusive_maintenance())  # noqa: SLF001
        return real_remove(path)

    monkeypatch.setattr(cli, "_remove_path", guarded_remove)
    files, entries, model_rows, artifacts = cli._clean_memory()

    assert files == 1
    assert entries == 1
    assert model_rows >= 2
    assert artifacts == 4
    assert removed_inside_gate and all(removed_inside_gate)
    assert not paths.exports_dir().exists()
    assert not paths.backup_dir().exists()
    assert not paths.human_file().exists()
    assert not paths.model_build_manifest().exists()
    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM evo_nodes").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM schema_faces").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM schema_input_receipts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM capture_content_receipts").fetchone()[0] == 1


def test_clean_memory_table_boundary_includes_gator_state() -> None:
    assert {
        "memory_delta_items",
        "memory_delta_window_claims",
        "model_candidate_evidence",
        "model_candidate_decisions",
        "model_candidates",
        "owner_alias_evidence",
        "owner_aliases",
        "event_occurrences",
        "relation_edge_effects",
        "schema_input_receipts",
        "source_imports",
    } <= set(cli._MODEL_TABLES)
    assert "capture_content_receipts" not in cli._MODEL_TABLES


def test_clean_all_keeps_only_install_configuration(ac_root, monkeypatch) -> None:
    _seed_capture()
    _seed_model()
    paths.config_file().write_text("[capture]\n")
    paths.env_file().write_text("PERSOME_LLM_API_KEY=synthetic\n")
    paths.human_file().write_text("# HUMAN.md\n")
    (paths.root() / "venv").mkdir()
    # Legacy Chat-era data from an older install: a full wipe must still purge it.
    (paths.root() / "chat-history").mkdir()
    (paths.root() / "chat-history" / "active.json").write_text("[]")
    (paths.root() / "skills").mkdir()
    (paths.root() / "skills" / "custom.md").write_text("Synthetic legacy skill.")
    paths.logs_dir().mkdir(exist_ok=True)
    (paths.logs_dir() / "daemon.log").write_text("synthetic")

    real_remove = cli._remove_path
    removed_inside_gate: list[bool] = []

    def guarded_remove(path):  # noqa: ANN001, ANN202
        removed_inside_gate.append(fts._in_exclusive_maintenance())  # noqa: SLF001
        return real_remove(path)

    monkeypatch.setattr(cli, "_remove_path", guarded_remove)
    cli.clean_all(yes=True)

    assert paths.config_file().exists()
    assert paths.env_file().exists()
    assert (paths.root() / "venv").is_dir()
    assert removed_inside_gate and all(removed_inside_gate)
    for deleted in (
        paths.capture_buffer_dir(),
        paths.memory_dir(),
        paths.logs_dir(),
        paths.human_file(),
        paths.root() / "chat-history",
        paths.root() / "skills",
        paths.index_db(),
    ):
        assert not deleted.exists()


def test_clean_refuses_to_race_a_running_daemon(ac_root, monkeypatch) -> None:
    capture_file = paths.capture_buffer_dir() / "must-survive.json"
    capture_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli, "_read_pid", lambda: 4242)

    result = CliRunner().invoke(cli.app, ["clean", "all", "--yes"])

    assert result.exit_code == 1
    assert "Refusing to clean" in result.output
    assert capture_file.exists()


def test_clean_running_guard_precedes_database_or_integrity_work(ac_root, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_read_pid", lambda: 4242)
    monkeypatch.setattr(
        cli.fts,
        "cursor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("DB was opened")),
    )
    monkeypatch.setattr(
        cli.integrity,
        "check_and_recover",
        lambda: (_ for _ in ()).throw(AssertionError("integrity work ran")),
    )

    result = CliRunner().invoke(cli.app, ["clean", "memory", "--yes"])

    assert result.exit_code == 1
    assert "Refusing to clean" in result.output
