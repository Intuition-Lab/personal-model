"""Lifecycle guards around the local daemon's SQLite ownership."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from persome import cli


def test_init_skips_mutable_integrity_recovery_while_daemon_is_running(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_read_pid", lambda: 4242)
    monkeypatch.setattr(
        cli.integrity,
        "check_and_recover",
        lambda: (_ for _ in ()).throw(AssertionError("active DB was touched")),
    )

    cfg = cli._init()

    assert cfg is not None


def test_start_short_circuits_before_initialization_when_already_running(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_read_pid", lambda: 4242)
    monkeypatch.setattr(
        cli,
        "_init",
        lambda: (_ for _ in ()).throw(AssertionError("start initialized an active runtime")),
    )

    result = CliRunner().invoke(cli.app, ["start"])

    assert result.exit_code == 1
    assert "Already running (pid 4242)" in result.output


def test_daemon_lifetime_lock_excludes_a_second_start(ac_root) -> None:
    first = cli._acquire_daemon_lock()
    try:
        with pytest.raises(RuntimeError, match="already starting or running"):
            cli._acquire_daemon_lock()
    finally:
        first.close()

    replacement = cli._acquire_daemon_lock()
    replacement.close()


def test_start_lock_failure_never_initializes_or_forks(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_read_pid", lambda: None)
    monkeypatch.setattr(cli, "_fail_if_runtime_state_is_ambiguous", lambda: None)
    monkeypatch.setattr(
        cli,
        "_acquire_daemon_lock",
        lambda: (_ for _ in ()).throw(RuntimeError("another Runtime is starting")),
    )
    monkeypatch.setattr(
        cli,
        "_init",
        lambda **kwargs: pytest.fail("losing start must not initialize the Runtime"),
    )

    result = CliRunner().invoke(cli.app, ["start"])

    assert result.exit_code == 1
    assert "another Runtime is starting" in result.output


def test_non_starting_client_skips_integrity_during_pid_publication_window(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = cli._acquire_daemon_lock()
    monkeypatch.setattr(cli, "_read_pid", lambda: None)
    monkeypatch.setattr(
        cli.integrity,
        "check_and_recover",
        lambda: pytest.fail("active startup window must not mutate SQLite"),
    )
    try:
        assert cli._init() is not None
    finally:
        lock.close()
