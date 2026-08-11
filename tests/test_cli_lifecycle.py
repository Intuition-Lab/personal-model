"""Lifecycle guards around the local daemon's SQLite ownership."""

from __future__ import annotations

import contextlib
import json
import shlex
import subprocess
import time
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from persome import cli


class _FakeLock:
    def __init__(self, fd: int = 91) -> None:
        self.closed = False
        self.fd = fd

    def fileno(self) -> int:
        return self.fd

    def close(self) -> None:
        self.closed = True


def _http_config() -> SimpleNamespace:
    return SimpleNamespace(
        mcp=SimpleNamespace(
            auto_start=True,
            transport="streamable-http",
            host="127.0.0.1",
            port=8742,
        )
    )


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


def test_init_runs_recovery_inside_reentrant_exclusive_database_gate(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_read_pid", lambda: None)
    monkeypatch.setattr(cli, "_daemon_lock_is_held", lambda: False)
    recovered: list[bool] = []

    def fake_recovery() -> list[object]:
        assert cli.fts._in_exclusive_maintenance()  # noqa: SLF001
        # Real recovery uses fts.cursor in some rebuild branches. It must reuse
        # this thread's exclusive boundary instead of self-deadlocking.
        with cli.fts.cursor() as conn:
            conn.execute("SELECT 1").fetchone()
        recovered.append(True)
        return []

    monkeypatch.setattr(cli.integrity, "check_and_recover", fake_recovery)

    assert cli._init() is not None
    assert recovered == [True]


def test_start_initialization_is_blocked_by_unresolved_authority(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_read_pid", lambda: None)
    monkeypatch.setattr(cli, "_daemon_lock_is_held", lambda: False)
    monkeypatch.setattr(cli.integrity, "check_and_recover", lambda: [])
    paths = cli.paths
    paths.atomic_write_private_text(
        paths.integrity_config_recovery_pending(),
        '{"version": 1, "phase": "authority_unresolved"}',
    )

    with pytest.raises(cli.typer.Exit) as exc:
        cli._init(starting_runtime=True)

    assert exc.value.exit_code == 2


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


def test_background_exec_adopts_the_same_canonical_lifetime_lock(ac_root) -> None:
    original = cli._acquire_daemon_lock()
    inherited_fd = cli.os.dup(original.fileno())
    adopted = cli._adopt_daemon_lock_fd(inherited_fd)
    original.close()
    try:
        assert cli.fcntl.fcntl(adopted.fileno(), cli.fcntl.F_GETFD) & cli.fcntl.FD_CLOEXEC
        with pytest.raises(RuntimeError, match="already starting or running"):
            cli._acquire_daemon_lock()
    finally:
        adopted.close()

    replacement = cli._acquire_daemon_lock()
    replacement.close()


def test_background_exec_rejects_a_noncanonical_lock_descriptor(ac_root) -> None:
    canonical = cli._acquire_daemon_lock()
    wrong_path = ac_root / "wrong-daemon.lock"
    wrong_fd = cli.os.open(wrong_path, cli.os.O_CREAT | cli.os.O_RDWR, 0o600)
    try:
        with pytest.raises(RuntimeError, match="canonical private lock"):
            cli._adopt_daemon_lock_fd(wrong_fd)
        with pytest.raises(OSError):
            cli.os.fstat(wrong_fd)
    finally:
        canonical.close()


def test_background_lock_marker_is_private_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(cli._BACKGROUND_DAEMON_LOCK_FD_ENV, "not-a-descriptor")

    with pytest.raises(RuntimeError, match="invalid inherited daemon lock"):
        cli._inherited_background_lock_fd()

    assert cli._BACKGROUND_DAEMON_LOCK_FD_ENV not in cli.os.environ


def test_background_spawn_execs_fresh_interpreter_and_transfers_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = _FakeLock(fd=91)
    seen: dict[str, object] = {}

    def fake_posix_spawn(
        executable: str,
        command: list[str],
        env: dict[str, str],
        **kwargs: object,
    ) -> int:
        seen.update(executable=executable, command=command, env=env, **kwargs)
        return 4242

    monkeypatch.delattr(cli.sys, "frozen", raising=False)
    monkeypatch.delenv("PYTHONNOUSERSITE", raising=False)
    monkeypatch.setenv("PERSOME_PARENT_PID", "1234")
    monkeypatch.setenv("PYTHONEXECUTABLE", "/tmp/foreign-python")
    monkeypatch.setenv("PYTHONHOME", "/tmp/foreign-python")
    monkeypatch.setenv("PYTHONPATH", "/tmp/foreign-package")
    monkeypatch.setenv("PYTHONPLATLIBDIR", "/tmp/foreign-lib")
    monkeypatch.setenv("VIRTUAL_ENV", "/tmp/foreign-venv")
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "/tmp/foreign-python")
    monkeypatch.setattr(cli.os, "posix_spawn", fake_posix_spawn)

    assert cli._spawn_background_runtime(lock, capture_only=True) == 4242
    command = seen["command"]
    assert command == [
        cli.sys.executable,
        "-m",
        "persome.cli",
        "start",
        "--foreground",
        "--capture-only",
    ]
    assert seen["executable"] == cli.sys.executable
    assert seen["setsid"] is True
    assert seen["env"][cli._BACKGROUND_DAEMON_LOCK_FD_ENV] == "3"
    assert "PERSOME_PARENT_PID" not in seen["env"]
    assert seen["env"]["PYTHONSAFEPATH"] == "1"
    assert "PYTHONNOUSERSITE" not in seen["env"]
    assert "PYTHONEXECUTABLE" not in seen["env"]
    assert "PYTHONHOME" not in seen["env"]
    assert "PYTHONPATH" not in seen["env"]
    assert "PYTHONPLATLIBDIR" not in seen["env"]
    assert "VIRTUAL_ENV" not in seen["env"]
    assert "__PYVENV_LAUNCHER__" not in seen["env"]
    assert seen["file_actions"] == [
        (cli.os.POSIX_SPAWN_DUP2, 91, 3),
        (cli.os.POSIX_SPAWN_CLOSE, 91),
        (cli.os.POSIX_SPAWN_OPEN, 0, cli.os.devnull, cli.os.O_RDWR, 0o666),
        (cli.os.POSIX_SPAWN_DUP2, 0, 1),
        (cli.os.POSIX_SPAWN_DUP2, 0, 2),
    ]


def test_background_spawn_cannot_import_persome_from_caller_working_directory(
    ac_root, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    caller_directory = tmp_path / "foreign-cwd"
    caller_directory.mkdir()
    fake_package = caller_directory / "persome"
    fake_package.mkdir()
    (fake_package / "__init__.py").write_text("", encoding="utf-8")
    marker = caller_directory / "cwd-package-imported"
    (fake_package / "cli.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    def fake_posix_spawn(
        _executable: str,
        command: list[str],
        env: dict[str, str],
        **_kwargs: object,
    ) -> int:
        seen.update(command=command, env=env)
        return 4242

    monkeypatch.delattr(cli.sys, "frozen", raising=False)
    monkeypatch.chdir(caller_directory)
    monkeypatch.setenv("PYTHONPATH", str(caller_directory))
    monkeypatch.setattr(cli.os, "posix_spawn", fake_posix_spawn)

    assert cli._spawn_background_runtime(_FakeLock(), capture_only=False) == 4242
    command = seen["command"]
    result = subprocess.run(
        [*command[:3], "--help"],
        cwd=caller_directory,
        env=seen["env"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()


def test_background_spawn_uses_fd_four_when_parent_lock_is_fd_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_posix_spawn(
        _executable: str,
        _command: list[str],
        env: dict[str, str],
        **kwargs: object,
    ) -> int:
        seen.update(env=env, **kwargs)
        return 4242

    monkeypatch.setattr(cli.os, "posix_spawn", fake_posix_spawn)

    assert cli._spawn_background_runtime(_FakeLock(fd=3), capture_only=False) == 4242
    assert seen["env"][cli._BACKGROUND_DAEMON_LOCK_FD_ENV] == "4"
    assert seen["file_actions"][:2] == [
        (cli.os.POSIX_SPAWN_DUP2, 3, 4),
        (cli.os.POSIX_SPAWN_CLOSE, 3),
    ]


def test_background_spawn_transfers_the_real_lock_across_exec(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready_path = ac_root / "spawn-lock-ready"
    child_code = (
        "import os,pathlib,time;"
        f"os.fstat(int(os.environ[{cli._BACKGROUND_DAEMON_LOCK_FD_ENV!r}]));"
        f"pathlib.Path({str(ready_path)!r}).touch();"
        "time.sleep(30)"
    )
    monkeypatch.setattr(
        cli,
        "_background_daemon_command",
        lambda *, capture_only: [cli.sys.executable, "-c", child_code],
    )

    parent_lock = cli._acquire_daemon_lock()
    spawned_pid = cli._spawn_background_runtime(parent_lock, capture_only=False)
    parent_lock.close()
    reaped = False
    try:
        deadline = time.monotonic() + 5.0
        while not ready_path.exists():
            observed_pid, status = cli.os.waitpid(spawned_pid, cli.os.WNOHANG)
            if observed_pid == spawned_pid:
                reaped = True
                pytest.fail(f"exec child exited before lock proof (wait status {status})")
            if time.monotonic() >= deadline:
                pytest.fail("exec child did not publish its lock proof")
            time.sleep(0.01)

        with pytest.raises(RuntimeError, match="already starting or running"):
            cli._acquire_daemon_lock()
    finally:
        if not reaped:
            with contextlib.suppress(ProcessLookupError):
                cli.os.kill(spawned_pid, cli.signal.SIGTERM)
            with contextlib.suppress(ChildProcessError):
                cli.os.waitpid(spawned_pid, 0)

    replacement = cli._acquire_daemon_lock()
    replacement.close()


def test_background_daemon_command_reexecs_the_frozen_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.sys, "frozen", True, raising=False)

    assert cli._background_daemon_command(capture_only=False) == [
        cli.sys.executable,
        "start",
        "--foreground",
    ]


def test_background_daemon_command_matches_runtime_identity_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(cli.sys, "frozen", raising=False)
    command = cli._background_daemon_command(capture_only=False)

    assert cli.runtime_pid.is_runtime_command(
        shlex.join(command),
        executable=command[0],
    )


def test_background_exec_child_skips_mutable_integrity_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = _FakeLock()
    cfg = object()
    init_kwargs: list[dict[str, bool]] = []
    runs: list[tuple[object, bool]] = []
    exits: list[int] = []
    monkeypatch.setattr(cli, "_adopt_daemon_lock_fd", lambda _fd: lock)
    monkeypatch.setattr(
        cli,
        "_init",
        lambda **kwargs: init_kwargs.append(kwargs) or cfg,
    )

    from persome import daemon

    monkeypatch.setattr(
        daemon,
        "run",
        lambda value, *, capture_only: runs.append((value, capture_only)),
    )
    monkeypatch.setattr(cli.os, "_exit", lambda code: exits.append(code))

    cli._run_background_exec_child(91, capture_only=True)

    assert init_kwargs == [{"starting_runtime": True, "recover_integrity": False}]
    assert runs == [(cfg, True)]
    assert lock.closed is True
    assert exits == [0]


def test_background_probe_rejects_receipt_from_a_different_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = SimpleNamespace(pid=9002)
    monkeypatch.setattr(cli.runtime_pid, "resolve_recorded_process", lambda: other)

    state, detail, process = cli._probe_background_runtime(
        _http_config(),
        spawned_pid=9001,
    )

    assert state == "fatal"
    assert "pid 9002, not spawned pid 9001" in detail
    assert process is None


def test_background_wait_reaps_and_reports_pre_receipt_sigbus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli.os,
        "waitpid",
        lambda pid, options: (pid, int(cli.signal.SIGBUS)),
    )
    monkeypatch.setattr(
        cli,
        "_probe_background_runtime",
        lambda *_args, **_kwargs: pytest.fail("an exited child must be reported before probing"),
    )

    ready, detail, process = cli._wait_for_background_start(
        _http_config(),
        spawned_pid=9001,
    )

    assert ready is False
    assert "SIGBUS" in detail
    assert process is None


def test_receiptless_spawned_child_is_terminated_and_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits = iter([False, True])
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        cli,
        "_wait_for_spawned_child_exit",
        lambda _pid, _timeout: next(waits),
    )
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    assert cli._terminate_failed_background_start(None, spawned_pid=9001) is True
    assert signals == [(9001, cli.signal.SIGTERM)]


def test_recorded_cleanup_still_requires_direct_child_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = SimpleNamespace(pid=9001)
    direct_children: list[int] = []
    monkeypatch.setattr(cli, "_terminate_recorded_background_start", lambda _process: True)
    monkeypatch.setattr(cli, "_wait_for_spawned_child_exit", lambda _pid, _timeout: False)
    monkeypatch.setattr(
        cli,
        "_terminate_spawned_background_child",
        lambda pid: direct_children.append(pid) or True,
    )

    assert cli._terminate_failed_background_start(process, spawned_pid=9001) is True
    assert direct_children == [9001]


def test_failed_start_never_signals_a_mismatched_runtime_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other_process = SimpleNamespace(pid=9002)
    direct_children: list[int] = []
    monkeypatch.setattr(
        cli,
        "_terminate_recorded_background_start",
        lambda _process: pytest.fail("a foreign Runtime receipt must not be signaled"),
    )
    monkeypatch.setattr(
        cli,
        "_terminate_spawned_background_child",
        lambda pid: direct_children.append(pid) or True,
    )

    assert cli._terminate_failed_background_start(other_process, spawned_pid=9001) is True
    assert direct_children == [9001]


def test_background_wait_timeout_keeps_spawned_pid_for_receiptless_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.os, "waitpid", lambda _pid, _options: (0, 0))
    monkeypatch.setattr(
        cli,
        "_probe_background_runtime",
        lambda _cfg, _expected=None, *, spawned_pid: (
            "retry",
            f"waiting for spawned pid {spawned_pid}",
            None,
        ),
    )

    ready, detail, process = cli._wait_for_background_start(
        _http_config(),
        spawned_pid=9001,
        timeout_seconds=0,
    )

    assert ready is False
    assert "startup timed out" in detail
    assert process is None


def test_start_lock_failure_never_initializes_or_spawns(
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


def test_non_recovering_init_never_inspects_or_repairs_database(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_fail_if_runtime_state_is_ambiguous", lambda: None)
    monkeypatch.setattr(
        cli,
        "_read_pid",
        lambda: pytest.fail("non-recovering init inspected the daemon PID"),
    )
    monkeypatch.setattr(
        cli.integrity,
        "check_and_recover",
        lambda: pytest.fail("non-recovering init touched SQLite"),
    )

    assert cli._init(recover_integrity=False) is not None


def test_mcp_uses_non_recovering_initialization(monkeypatch: pytest.MonkeyPatch) -> None:
    init_kwargs: list[dict[str, bool]] = []
    started: list[bool] = []
    monkeypatch.setattr(cli, "_init", lambda **kwargs: init_kwargs.append(kwargs))

    from persome.mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "run_stdio", lambda: started.append(True))
    cli.mcp()

    assert init_kwargs == [{"recover_integrity": False}]
    assert started == [True]


def test_mcp_keeps_initialization_notices_off_protocol_stdout(
    ac_root,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli.paths.config_file().unlink(missing_ok=True)

    from persome.mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "run_stdio", lambda: None)
    cli.mcp()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Created default config" in captured.err


def test_cli_surfaces_database_recovery_and_model_rebuild_next_step(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_read_pid", lambda: None)
    monkeypatch.setattr(cli, "_daemon_lock_is_held", lambda: False)
    monkeypatch.setattr(
        cli.integrity,
        "check_and_recover",
        lambda: [
            cli.integrity.QuarantinedFile(
                kind="database",
                original_path="index.db",
                quarantine_path="index.db.corrupt.test",
                reason="synthetic corruption",
            )
        ],
    )

    result = CliRunner().invoke(cli.app, ["model", "status"])

    assert result.exit_code == 0, result.output
    assert "recovered a damaged local database" in result.output
    assert "persome model build" in result.output


def test_background_start_reports_success_only_after_readiness(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = _FakeLock()
    cfg = _http_config()
    observed: list[str] = []
    monkeypatch.setattr(cli, "_fail_if_runtime_state_is_ambiguous", lambda: None)
    monkeypatch.setattr(cli, "_read_pid", lambda: None)
    monkeypatch.setattr(cli, "_acquire_daemon_lock", lambda: lock)
    monkeypatch.setattr(cli.env_file_mod, "ensure_local_api_token", lambda _path: "existing")
    monkeypatch.setattr(cli, "_init", lambda **_kwargs: cfg)
    spawned: list[tuple[object, bool]] = []
    monkeypatch.setattr(
        cli,
        "_spawn_background_runtime",
        lambda value, *, capture_only: spawned.append((value, capture_only)) or 4321,
    )
    monkeypatch.setattr(
        cli,
        "_wait_for_background_start",
        lambda _cfg, *, spawned_pid: (
            observed.append(f"ready:{spawned_pid}") or True,
            "ready",
            SimpleNamespace(pid=spawned_pid),
        ),
    )
    monkeypatch.setattr(
        cli,
        "_terminate_failed_background_start",
        lambda _process: pytest.fail("a ready Runtime must not be terminated"),
    )

    result = CliRunner().invoke(cli.app, ["start"])

    assert result.exit_code == 0, result.output
    assert observed == ["ready:4321"]
    assert spawned == [(lock, False)]
    assert lock.closed is True
    assert "Persome started in background." in result.output


def test_background_start_failure_stops_child_and_reports_port_owner(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = _FakeLock()
    cfg = _http_config()
    process = SimpleNamespace(pid=4321)
    terminated: list[object] = []
    monkeypatch.setattr(cli, "_fail_if_runtime_state_is_ambiguous", lambda: None)
    monkeypatch.setattr(cli, "_read_pid", lambda: None)
    monkeypatch.setattr(cli, "_acquire_daemon_lock", lambda: lock)
    monkeypatch.setattr(cli.env_file_mod, "ensure_local_api_token", lambda _path: "existing")
    monkeypatch.setattr(cli, "_init", lambda **_kwargs: cfg)
    monkeypatch.setattr(cli, "_spawn_background_runtime", lambda *_args, **_kwargs: 4321)
    monkeypatch.setattr(
        cli,
        "_wait_for_background_start",
        lambda _cfg, *, spawned_pid: (
            False,
            f"spawned pid {spawned_pid} did not own port 8742",
            process,
        ),
    )
    monkeypatch.setattr(
        cli,
        "_terminate_failed_background_start",
        lambda value, *, spawned_pid: terminated.append((value, spawned_pid)) or True,
    )

    result = CliRunner().invoke(cli.app, ["start"])

    assert result.exit_code == 1, result.output
    assert lock.closed is True
    assert terminated == [(process, 4321)]
    assert "Persome started in background." not in result.output
    assert "did not start correctly" in result.output
    assert "incomplete background Runtime was stopped" in result.output
    assert "lsof -nP -iTCP:8742 -sTCP:LISTEN" in result.output
    assert "persome doctor" in result.output


def test_background_spawn_failure_is_reported_without_waiting(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = _FakeLock()
    cfg = _http_config()
    terminated: list[object] = []
    monkeypatch.setattr(cli, "_fail_if_runtime_state_is_ambiguous", lambda: None)
    monkeypatch.setattr(cli, "_read_pid", lambda: None)
    monkeypatch.setattr(cli, "_acquire_daemon_lock", lambda: lock)
    monkeypatch.setattr(cli.env_file_mod, "ensure_local_api_token", lambda _path: "existing")
    monkeypatch.setattr(cli, "_init", lambda **_kwargs: cfg)
    monkeypatch.setattr(
        cli,
        "_spawn_background_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("spawn failed")),
    )
    monkeypatch.setattr(
        cli,
        "_wait_for_background_start",
        lambda _cfg: pytest.fail("a failed spawn must not enter readiness polling"),
    )
    monkeypatch.setattr(
        cli,
        "_terminate_failed_background_start",
        lambda process, *, spawned_pid: terminated.append((process, spawned_pid)) or True,
    )

    result = CliRunner().invoke(cli.app, ["start"])

    assert result.exit_code == 1, result.output
    assert lock.closed is True
    assert terminated == [(None, None)]
    assert "could not spawn the background Runtime" in result.output
    assert "OSError" in result.output


def test_background_verification_exception_never_targets_another_receipt(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = _FakeLock()
    cfg = _http_config()
    other_process = SimpleNamespace(pid=9999)
    terminated: list[tuple[object, int | None]] = []
    monkeypatch.setattr(cli, "_fail_if_runtime_state_is_ambiguous", lambda: None)
    monkeypatch.setattr(cli, "_read_pid", lambda: None)
    monkeypatch.setattr(cli, "_acquire_daemon_lock", lambda: lock)
    monkeypatch.setattr(cli.env_file_mod, "ensure_local_api_token", lambda _path: "existing")
    monkeypatch.setattr(cli, "_init", lambda **_kwargs: cfg)
    monkeypatch.setattr(cli, "_spawn_background_runtime", lambda *_args, **_kwargs: 4321)
    monkeypatch.setattr(
        cli,
        "_wait_for_background_start",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("probe failed")),
    )
    monkeypatch.setattr(
        cli.runtime_pid,
        "resolve_recorded_process",
        lambda: other_process,
    )
    monkeypatch.setattr(
        cli,
        "_terminate_failed_background_start",
        lambda process, *, spawned_pid: terminated.append((process, spawned_pid)) or True,
    )

    result = CliRunner().invoke(cli.app, ["start"])

    assert result.exit_code == 1, result.output
    assert terminated == [(None, 4321)]
    assert "startup verification failed" in result.output


def test_failed_background_cleanup_escalates_only_the_observed_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = SimpleNamespace(pid=4242)
    signals: list[tuple[object, int]] = []
    cleared: list[object] = []
    waits = iter([False, True])
    monkeypatch.setattr(
        cli.runtime_pid,
        "signal_process",
        lambda target, sig: signals.append((target, sig)) or True,
    )
    monkeypatch.setattr(
        cli.runtime_pid,
        "wait_for_exit",
        lambda target, timeout: next(waits),
    )
    monkeypatch.setattr(
        cli,
        "_clear_failed_background_receipts",
        lambda target: cleared.append(target) or True,
    )

    assert cli._terminate_failed_background_start(process) is True
    assert signals == [(process, cli.signal.SIGTERM), (process, cli.signal.SIGKILL)]
    assert cleared == [process]


def test_sigterm_startup_exit_clears_matching_stale_runtime_receipts(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = "b" * 32
    started_at = 1_752_300_000.0
    process = SimpleNamespace(
        pid=4242,
        generation=generation,
        runtime_started_at=started_at,
    )
    cli.paths.atomic_write_private_text(cli.paths.pid_file(), str(process.pid))
    cli.paths.atomic_write_private_text(
        cli.paths.runtime_state_file(),
        json.dumps(
            {
                "schema_version": 1,
                "pid": process.pid,
                "generation": generation,
                "started_at": started_at,
                "updated_at": started_at,
            }
        ),
    )
    signals: list[tuple[object, int]] = []
    monkeypatch.setattr(
        cli.runtime_pid,
        "signal_process",
        lambda target, sig: signals.append((target, sig)) or True,
    )
    monkeypatch.setattr(cli.runtime_pid, "wait_for_exit", lambda _target, _timeout: True)
    monkeypatch.setattr(cli.runtime_pid, "same_process_is_running", lambda _process: False)

    assert cli._terminate_failed_background_start(process) is True
    assert signals == [(process, cli.signal.SIGTERM)]
    assert not cli.paths.pid_file().exists()
    assert not cli.paths.runtime_state_file().exists()


def test_sigterm_graceful_exit_cleanup_is_idempotent(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = SimpleNamespace(
        pid=4242,
        generation="c" * 32,
        runtime_started_at=1_752_300_000.0,
    )
    monkeypatch.setattr(cli.runtime_pid, "signal_process", lambda _target, _sig: True)
    monkeypatch.setattr(cli.runtime_pid, "wait_for_exit", lambda _target, _timeout: True)
    monkeypatch.setattr(cli.runtime_pid, "same_process_is_running", lambda _process: False)

    assert cli._terminate_failed_background_start(process) is True
    assert not cli.paths.pid_file().exists()
    assert not cli.paths.runtime_state_file().exists()


def test_sigkill_cleanup_removes_only_matching_stale_runtime_receipts(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = "a" * 32
    started_at = 1_752_300_000.0
    process = SimpleNamespace(
        pid=4242,
        generation=generation,
        runtime_started_at=started_at,
    )
    cli.paths.atomic_write_private_text(cli.paths.pid_file(), str(process.pid))
    cli.paths.atomic_write_private_text(
        cli.paths.runtime_state_file(),
        json.dumps(
            {
                "schema_version": 1,
                "pid": process.pid,
                "generation": generation,
                "started_at": started_at,
                "updated_at": started_at,
            }
        ),
    )
    monkeypatch.setattr(cli.runtime_pid, "same_process_is_running", lambda _process: False)

    assert cli._clear_failed_background_receipts(process) is True
    assert not cli.paths.pid_file().exists()
    assert not cli.paths.runtime_state_file().exists()
