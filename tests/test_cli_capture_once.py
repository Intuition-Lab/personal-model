"""Safety boundary for the isolated capture-once diagnostic."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from persome import cli
from persome.capture import ax_capture, scheduler


@pytest.mark.parametrize(("pid", "lock_held"), [(4242, False), (None, True)])
def test_capture_once_refuses_running_or_starting_runtime_before_init(
    ac_root,
    monkeypatch: pytest.MonkeyPatch,
    pid: int | None,
    lock_held: bool,
) -> None:
    monkeypatch.setattr(cli, "_read_pid", lambda: pid)
    if lock_held:
        monkeypatch.setattr(
            cli,
            "_acquire_daemon_lock",
            lambda: (_ for _ in ()).throw(RuntimeError("held")),
        )
    else:
        monkeypatch.setattr(
            cli,
            "_acquire_daemon_lock",
            lambda: pytest.fail("a live PID must refuse before acquiring the lock"),
        )
    monkeypatch.setattr(
        cli,
        "_init",
        lambda *args, **kwargs: pytest.fail("runtime refusal must happen before initialization"),
    )

    result = CliRunner().invoke(cli.app, ["capture-once"])

    assert result.exit_code == 1
    output = " ".join(result.output.split())
    assert "Refusing to run capture-once" in output
    assert "persome stop" in output


def test_capture_once_holds_runtime_lock_through_direct_write(
    ac_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Lock:
        closed = False

        def close(self) -> None:
            self.closed = True

    lock = Lock()
    monkeypatch.setattr(cli, "_read_pid", lambda: None)
    monkeypatch.setattr(cli, "_acquire_daemon_lock", lambda: lock)

    def init(*, recover_integrity: bool = True):
        assert recover_integrity is False
        assert lock.closed is False
        return SimpleNamespace(
            capture=SimpleNamespace(ax_depth=4, ax_timeout_seconds=2.0),
        )

    monkeypatch.setattr(cli, "_init", init)
    monkeypatch.setattr(ax_capture, "create_provider", lambda **_kwargs: object())

    def write_once(_cfg, _provider):
        assert lock.closed is False
        return Path("/private/tmp/synthetic-capture.json")

    monkeypatch.setattr(scheduler, "capture_once", write_once)

    result = CliRunner().invoke(cli.app, ["capture-once"])

    assert result.exit_code == 0
    assert lock.closed is True


def test_capture_once_rechecks_pid_after_acquiring_runtime_lock(
    ac_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Lock:
        closed = False

        def close(self) -> None:
            self.closed = True

    lock = Lock()
    observed_pids = iter([None, 4242])
    monkeypatch.setattr(cli, "_read_pid", lambda: next(observed_pids))
    monkeypatch.setattr(cli, "_acquire_daemon_lock", lambda: lock)
    monkeypatch.setattr(
        cli,
        "_init",
        lambda *args, **kwargs: pytest.fail("a newly published PID must refuse before init"),
    )

    result = CliRunner().invoke(cli.app, ["capture-once"])

    assert result.exit_code == 1
    assert "pid 4242" in " ".join(result.output.split())
    assert lock.closed is True


@pytest.mark.parametrize("failure_stage", ["init", "provider", "write"])
def test_capture_once_releases_runtime_lock_after_failure(
    ac_root,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    class Lock:
        closed = False

        def close(self) -> None:
            self.closed = True

    lock = Lock()
    monkeypatch.setattr(cli, "_read_pid", lambda: None)
    monkeypatch.setattr(cli, "_acquire_daemon_lock", lambda: lock)

    def init(*, recover_integrity: bool = True):
        assert recover_integrity is False
        if failure_stage == "init":
            raise RuntimeError("init failed")
        return SimpleNamespace(capture=SimpleNamespace(ax_depth=4, ax_timeout_seconds=2.0))

    def create_provider(**_kwargs):
        if failure_stage == "provider":
            raise RuntimeError("provider failed")
        return object()

    def write_once(_cfg, _provider):
        if failure_stage == "write":
            raise RuntimeError("write failed")
        return Path("/private/tmp/synthetic-capture.json")

    monkeypatch.setattr(cli, "_init", init)
    monkeypatch.setattr(ax_capture, "create_provider", create_provider)
    monkeypatch.setattr(scheduler, "capture_once", write_once)

    result = CliRunner().invoke(cli.app, ["capture-once"])

    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    assert lock.closed is True
