from pathlib import Path

from scripts import language_scan


def test_claude_config_is_scanned_but_embedded_worktrees_are_skipped(tmp_path: Path) -> None:
    config = tmp_path / ".claude" / "settings.json"
    embedded = tmp_path / ".claude" / "worktrees" / "old" / "copied.py"
    config.parent.mkdir(parents=True)
    embedded.parent.mkdir(parents=True)
    config.write_text("{}", encoding="utf-8")
    embedded.write_text("copied = True", encoding="utf-8")

    files = language_scan._text_files(tmp_path)

    assert config in files
    assert embedded not in files
