"""Tests for the macOS LaunchAgent integration (issue #194).

These exercise plist content and the launchctl wrappers without touching the
real ``~/Library/LaunchAgents`` directory or invoking ``launchctl`` — the plist
path and ``subprocess.run`` are redirected/monkeypatched.
"""

from __future__ import annotations

import json
import os
import plistlib
import signal
import subprocess
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from persome import cli, launchagent, paths, runtime_pid


@pytest.fixture(autouse=True)
def ready_fake_launchagent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install unit tests never wait on the user's real launchd namespace."""

    monkeypatch.setattr(launchagent, "_wait_for_owned_runtime", lambda binary, timeout=15: True)


def test_label_matches_runtime_contract() -> None:
    assert launchagent.LABEL == "com.persome.runtime"


def test_plist_path_is_under_launchagents() -> None:
    path = launchagent.plist_path()
    assert path.parent == Path.home() / "Library" / "LaunchAgents"
    assert path.name == "com.persome.runtime.plist"


def test_gui_domain_target_shape() -> None:
    target = launchagent.gui_domain_target()
    assert target.startswith("gui/")
    assert target.endswith(f"/{launchagent.LABEL}")


def test_loaded_job_parses_launchd_cached_program_and_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = """
    program = /Applications/Persome.app/Contents/MacOS/Persome Backend
    state = running
    pid = 4242
    """
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, output, ""),
    )

    assert launchagent.loaded_job() == launchagent.LoadedJob(
        4242,
        "/Applications/Persome.app/Contents/MacOS/Persome Backend",
    )


def test_loaded_job_requires_a_live_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            "program = /tmp/persome\nstate = waiting\n",
            "",
        ),
    )

    assert launchagent.loaded_job() is None


def test_build_plist_core_fields(ac_root: Path) -> None:
    binary = "/Applications/acme.app/Contents/Resources/oc/persome"
    pl = launchagent.build_plist(binary)

    assert pl["Label"] == launchagent.LABEL
    assert pl["ProgramArguments"] == [binary, "start", "--foreground"]
    assert pl["KeepAlive"] is True
    assert pl["RunAtLoad"] is True
    assert pl["Umask"] == 0o077
    # Logs route under the data root so the diagnostic bundle collects them.
    assert pl["StandardOutPath"] == str(paths.launchd_stdout_log())
    assert pl["StandardErrorPath"] == str(paths.launchd_stderr_log())
    assert str(ac_root) in pl["StandardOutPath"]


def test_build_plist_propagates_root_override(ac_root: Path) -> None:
    pl = launchagent.build_plist("/bin/persome")
    env = pl["EnvironmentVariables"]
    assert isinstance(env, dict)
    assert env["PERSOME_ROOT"] == str(ac_root)


def test_log_paths_live_under_logs_dir(ac_root: Path) -> None:
    assert paths.launchd_stdout_log() == paths.logs_dir() / "launchd.out.log"
    assert paths.launchd_stderr_log() == paths.logs_dir() / "launchd.err.log"


def test_write_plist_roundtrips(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "LaunchAgents" / "com.persome.runtime.plist"
    monkeypatch.setattr(launchagent, "plist_path", lambda: target)

    binary = "/usr/local/bin/persome"
    written = launchagent.write_plist(binary)

    assert written == target
    assert target.exists()
    with target.open("rb") as fh:
        loaded = plistlib.load(fh)
    assert loaded["ProgramArguments"][0] == binary
    assert loaded["KeepAlive"] is True
    assert loaded["Umask"] == 0o077


def test_install_writes_and_bootstraps(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "LaunchAgents" / "com.persome.runtime.plist"
    monkeypatch.setattr(launchagent, "plist_path", lambda: target)
    # Pretend nothing is loaded yet, and capture launchctl invocations.
    monkeypatch.setattr(launchagent, "is_loaded", lambda: False)
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    written = launchagent.install("/usr/local/bin/persome")
    assert written == target
    assert target.exists()
    # Legacy-label sweep first, then exactly one bootstrap (no prior bootout
    # since not loaded).
    assert [c[1] for c in calls] == ["bootout"] * len(launchagent.LEGACY_LABELS) + ["bootstrap"]


def test_install_reloads_when_already_loaded(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "LaunchAgents" / "com.persome.runtime.plist"
    monkeypatch.setattr(launchagent, "plist_path", lambda: target)
    monkeypatch.setattr(launchagent, "is_loaded", lambda: True)
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    launchagent.install("/usr/local/bin/persome")
    # Legacy sweep, bootout (stale job), then bootstrap (fresh binary path).
    verbs = [c[1] for c in calls]
    assert verbs == ["bootout"] * len(launchagent.LEGACY_LABELS) + ["bootout", "bootstrap"]


def test_install_keeps_already_loaded_matching_generation(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "LaunchAgents" / "com.persome.runtime.plist"
    monkeypatch.setattr(launchagent, "plist_path", lambda: target)
    monkeypatch.setattr(launchagent, "is_loaded", lambda: True)
    launchagent.write_plist("/usr/local/bin/persome")
    monkeypatch.setattr(launchagent, "owns_recorded_runtime", lambda binary: True)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )

    assert launchagent.install("/usr/local/bin/persome") == target
    assert calls == []


def test_install_restarts_loaded_job_when_no_owned_generation_exists(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "LaunchAgents" / "com.persome.runtime.plist"
    monkeypatch.setattr(launchagent, "plist_path", lambda: target)
    launchagent.write_plist("/usr/local/bin/persome")
    monkeypatch.setattr(launchagent, "is_loaded", lambda: True)
    monkeypatch.setattr(launchagent, "owns_recorded_runtime", lambda binary: False)
    monkeypatch.setattr(launchagent, "_terminate_stray_daemon", lambda: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )

    launchagent.install("/usr/local/bin/persome")

    assert [command[1] for command in calls] == ["bootout", "bootstrap"]


def test_install_fails_when_launchctl_bootstrap_fails(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "LaunchAgents" / "com.persome.runtime.plist"
    monkeypatch.setattr(launchagent, "plist_path", lambda: target)
    monkeypatch.setattr(launchagent, "is_loaded", lambda: False)
    monkeypatch.setattr(launchagent, "_terminate_stray_daemon", lambda: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 5, "", "denied"),
    )

    with pytest.raises(RuntimeError, match="denied"):
        launchagent.install("/usr/local/bin/persome")

    assert not paths.launchagent_owner_file().exists()


def test_install_fails_when_loaded_job_never_owns_the_daemon_generation(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "LaunchAgents" / "com.persome.runtime.plist"
    monkeypatch.setattr(launchagent, "plist_path", lambda: target)
    monkeypatch.setattr(launchagent, "is_loaded", lambda: False)
    monkeypatch.setattr(launchagent, "_terminate_stray_daemon", lambda: None)
    monkeypatch.setattr(launchagent, "_wait_for_owned_runtime", lambda binary, timeout=15: False)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )

    with pytest.raises(RuntimeError, match="matching daemon generation"):
        launchagent.install("/usr/local/bin/persome")

    assert [command[1] for command in calls] == ["bootstrap", "bootout"]
    assert not paths.launchagent_owner_file().exists()


def test_uninstall_boots_out_and_removes_plist(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "LaunchAgents" / "com.persome.runtime.plist"
    target.parent.mkdir(parents=True)
    target.write_text("stub")
    monkeypatch.setattr(launchagent, "plist_path", lambda: target)
    monkeypatch.setattr(launchagent, "is_loaded", lambda: True)
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    launchagent.uninstall()
    assert not target.exists()
    assert calls[0][1] == "bootout"


# ── CLI surface ───────────────────────────────────────────────────────────


def test_cli_install_invokes_module(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def fake_install(binary: str) -> Path:
        seen["binary"] = binary
        return Path("/tmp/x.plist")

    monkeypatch.setattr(launchagent, "install", fake_install)
    result = CliRunner().invoke(cli.app, ["launchagent", "install", "--binary", "/opt/oc/persome"])
    assert result.exit_code == 0, result.output
    assert seen["binary"] == "/opt/oc/persome"
    assert "LaunchAgent installed" in result.output


def test_cli_status_exit_codes(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launchagent, "is_loaded", lambda: True)
    monkeypatch.setattr(launchagent, "plist_path", lambda: Path("/tmp/x.plist"))
    loaded = CliRunner().invoke(cli.app, ["launchagent", "status"])
    assert loaded.exit_code == 0
    assert "yes" in loaded.output

    monkeypatch.setattr(launchagent, "is_loaded", lambda: False)
    unloaded = CliRunner().invoke(cli.app, ["launchagent", "status"])
    # status exits non-zero when not loaded (scriptable health check).
    assert unloaded.exit_code == 1


def test_cli_uninstall_invokes_module(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"n": 0}
    monkeypatch.setattr(launchagent, "uninstall", lambda: called.__setitem__("n", 1))
    result = CliRunner().invoke(cli.app, ["launchagent", "uninstall"])
    assert result.exit_code == 0, result.output
    assert called["n"] == 1


# ── _terminate_stray_daemon: kill a pre-launchd orphan on takeover ──────────


def test_terminate_stray_ignores_missing_pidfile(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    waited: list[runtime_pid.ProcessIdentity] = []
    monkeypatch.setattr(runtime_pid, "signal_recorded_process", lambda _sig: None)
    monkeypatch.setattr(
        runtime_pid, "wait_for_exit", lambda process, _timeout: waited.append(process)
    )
    launchagent._terminate_stray_daemon()
    assert waited == []  # no verified process → nothing to wait for


def test_terminate_stray_ignores_dead_pid(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    waited: list[runtime_pid.ProcessIdentity] = []
    monkeypatch.setattr(runtime_pid, "signal_recorded_process", lambda _sig: None)
    monkeypatch.setattr(
        runtime_pid, "wait_for_exit", lambda process, _timeout: waited.append(process)
    )
    launchagent._terminate_stray_daemon()
    assert waited == []


def test_terminate_stray_sigterms_live_pid(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process = runtime_pid.ProcessIdentity(
        pid=4242,
        uid=os.getuid(),
        started_at=time.time() - 10,
        command="/tmp/example/.persome/venv/bin/persome start --foreground",
    )
    sent: list[int] = []
    waited: list[tuple[runtime_pid.ProcessIdentity, float]] = []

    def fake_signal(sig: int) -> runtime_pid.ProcessIdentity:
        sent.append(sig)
        return process

    monkeypatch.setattr(runtime_pid, "signal_recorded_process", fake_signal)
    monkeypatch.setattr(
        runtime_pid,
        "wait_for_exit",
        lambda identity, timeout: waited.append((identity, timeout)) or True,
    )
    launchagent._terminate_stray_daemon(timeout=3.0)
    assert sent == [signal.SIGTERM]
    assert waited == [(process, 3.0)]


def test_terminate_stray_rejects_ambiguous_live_runtime(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_pid, "signal_recorded_process", lambda _sig: None)
    monkeypatch.setattr(
        runtime_pid,
        "unresolved_runtime_reason",
        lambda: "live Persome Runtime pid 4242 has an invalid generation receipt",
    )

    with pytest.raises(RuntimeError, match="refusing|invalid generation"):
        launchagent._terminate_stray_daemon()


def test_install_terminates_stray_before_bootstrap(
    ac_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "agent.plist"
    monkeypatch.setattr(launchagent, "plist_path", lambda: target)
    monkeypatch.setattr(launchagent, "is_loaded", lambda: False)
    order: list[str] = []
    monkeypatch.setattr(launchagent, "_terminate_stray_daemon", lambda: order.append("terminate"))

    def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        order.append(args[1])  # launchctl verb
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    launchagent.install("/usr/local/bin/persome")
    # Stray daemon is killed BEFORE the fresh job is bootstrapped (the legacy
    # label sweep records its bootout verbs first).
    assert order == ["bootout"] * len(launchagent.LEGACY_LABELS) + ["terminate", "bootstrap"]


# ── Runtime PID identity: never signal a stale or unrelated PID ──────────


def _identity(
    pid: int = 4242,
    *,
    uid: int | None = None,
    started_at: float | None = None,
    command: str = "/tmp/example/.persome/venv/bin/persome start --foreground",
) -> runtime_pid.ProcessIdentity:
    return runtime_pid.ProcessIdentity(
        pid=pid,
        uid=os.getuid() if uid is None else uid,
        started_at=time.time() - 10 if started_at is None else started_at,
        command=command,
    )


@pytest.mark.parametrize("raw", ["0", "-1", "1"])
def test_runtime_pid_rejects_unsafe_pid_values(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    paths.pid_file().write_text(raw)
    inspected: list[int] = []
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(runtime_pid, "inspect_process", lambda pid: inspected.append(pid))
    monkeypatch.setattr(runtime_pid.os, "kill", lambda pid, sig: signalled.append((pid, sig)))

    assert runtime_pid.resolve_recorded_process() is None
    assert runtime_pid.signal_recorded_process(signal.SIGTERM) is None
    assert inspected == []
    assert signalled == []


def test_runtime_pid_rejects_unrelated_same_user_process(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths.pid_file().write_text("4242")
    unrelated = _identity(command="/usr/bin/python3 /tmp/report-worker.py")
    monkeypatch.setattr(runtime_pid, "inspect_process", lambda _pid: unrelated)
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(runtime_pid.os, "kill", lambda pid, sig: signalled.append((pid, sig)))

    assert runtime_pid.signal_recorded_process(signal.SIGTERM) is None
    assert signalled == []


def test_runtime_pid_rejects_process_owned_by_another_user(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths.pid_file().write_text("4242")
    monkeypatch.setattr(runtime_pid, "inspect_process", lambda _pid: _identity(uid=os.getuid() + 1))
    monkeypatch.setattr(
        runtime_pid.os,
        "kill",
        lambda _pid, _sig: pytest.fail("must not signal another user's process"),
    )

    assert runtime_pid.signal_recorded_process(signal.SIGTERM) is None


def test_legacy_runtime_pid_rejects_pid_reused_after_pidfile(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths.pid_file().write_text("4242")
    os.utime(paths.pid_file(), (1_000.0, 1_000.0))
    reused = _identity(started_at=1_001.0)
    monkeypatch.setattr(runtime_pid, "inspect_process", lambda _pid: reused)
    monkeypatch.setattr(
        runtime_pid.os,
        "kill",
        lambda _pid, _sig: pytest.fail("must not signal a process newer than the pid file"),
    )

    assert runtime_pid.signal_recorded_process(signal.SIGTERM) is None


def test_versioned_runtime_pid_rejects_reused_identity(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = _identity(started_at=1_000.0)
    paths.pid_file().write_text(
        json.dumps(
            {
                "version": runtime_pid.PID_FILE_VERSION,
                "pid": expected.pid,
                "uid": expected.uid,
                "started_at": expected.started_at,
                "command": expected.command,
                "generation": "a" * 32,
            }
        )
    )
    monkeypatch.setattr(runtime_pid, "inspect_process", lambda _pid: _identity(started_at=2_000.0))
    monkeypatch.setattr(
        runtime_pid.os,
        "kill",
        lambda _pid, _sig: pytest.fail("must not signal a reused versioned PID"),
    )

    assert runtime_pid.signal_recorded_process(signal.SIGTERM) is None


def test_runtime_pid_revalidates_identity_immediately_before_signal(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths.pid_file().write_text("4242")
    first = _identity()
    reused = _identity(started_at=first.started_at + 1)
    inspections = iter([first, reused])
    monkeypatch.setattr(runtime_pid, "inspect_process", lambda _pid: next(inspections))
    monkeypatch.setattr(
        runtime_pid.os,
        "kill",
        lambda _pid, _sig: pytest.fail("must not signal after identity changes"),
    )

    assert runtime_pid.signal_recorded_process(signal.SIGTERM) is None


def test_runtime_pid_signals_verified_legacy_daemon(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths.pid_file().write_text("4242")
    process = _identity()
    monkeypatch.setattr(runtime_pid, "inspect_process", lambda _pid: process)
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(runtime_pid.os, "kill", lambda pid, sig: signalled.append((pid, sig)))

    resolved = runtime_pid.signal_recorded_process(signal.SIGTERM)

    assert resolved == process
    assert signalled == [(4242, signal.SIGTERM)]


def test_wait_for_exit_treats_pid_reuse_as_original_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    original = _identity()
    monkeypatch.setattr(
        runtime_pid,
        "inspect_process",
        lambda _pid: _identity(started_at=original.started_at + 1),
    )

    assert runtime_pid.wait_for_exit(original, timeout=1.0) is True


def test_versioned_pid_file_roundtrips_current_identity(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = _identity(pid=os.getpid())
    monkeypatch.setattr(runtime_pid, "inspect_process", lambda _pid: current)

    assert runtime_pid.write_current_process(generation="a" * 32) == current
    record = runtime_pid.read_pid_file()
    assert record is not None
    assert record.legacy is False
    assert record.pid == current.pid
    assert record.uid == current.uid
    assert record.started_at == current.started_at
    assert record.command == current.command
    assert record.generation == "a" * 32


def test_runtime_generation_is_pinned_across_signal_revalidation(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths.pid_file().write_text("4242")
    process = _identity()
    now = time.time()
    paths.runtime_state_file().write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pid": process.pid,
                "generation": "a" * 32,
                "started_at": now - 5,
                "updated_at": now,
            }
        )
    )
    monkeypatch.setattr(runtime_pid, "inspect_process", lambda _pid: process)
    states = iter(
        [
            runtime_pid.RuntimeGeneration(process.pid, "a" * 32, now - 5, now),
            runtime_pid.RuntimeGeneration(process.pid, "b" * 32, now - 4, now),
        ]
    )
    monkeypatch.setattr(runtime_pid, "read_runtime_generation", lambda _path=None: next(states))
    monkeypatch.setattr(
        runtime_pid.os,
        "kill",
        lambda _pid, _sig: pytest.fail("must not signal a different daemon generation"),
    )

    assert runtime_pid.signal_recorded_process(signal.SIGTERM) is None


def test_runtime_pid_rejects_malformed_generation_sidecar(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths.pid_file().write_text("4242")
    paths.runtime_state_file().write_text("{}")
    monkeypatch.setattr(runtime_pid, "inspect_process", lambda _pid: _identity())

    assert runtime_pid.resolve_recorded_process() is None


@pytest.mark.parametrize(
    "command",
    [
        "/Applications/Persome.app/Contents/MacOS/Persome Backend start --foreground",
        "'/Applications/Persome.app/Contents/MacOS/Persome Backend' start --foreground",
    ],
)
def test_runtime_pid_accepts_frozen_desktop_backend_command(command: str) -> None:
    process = _identity(
        command=command,
    )
    process = runtime_pid.ProcessIdentity(
        **{
            **process.__dict__,
            "executable": "/Applications/Persome.app/Contents/MacOS/Persome Backend",
        }
    )

    assert runtime_pid.is_runtime_process(process) is True


def test_runtime_pid_rejects_frozen_backend_non_daemon_command() -> None:
    process = runtime_pid.ProcessIdentity(
        pid=4242,
        uid=os.getuid(),
        started_at=time.time() - 10,
        command="/Applications/Persome.app/Contents/MacOS/Persome Backend mcp",
        executable="/Applications/Persome.app/Contents/MacOS/Persome Backend",
    )

    assert runtime_pid.is_runtime_process(process) is False


def test_unresolved_runtime_reason_fails_closed_for_live_daemon_with_bad_sidecar(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths.pid_file().write_text("4242")
    paths.runtime_state_file().write_text("{}")
    monkeypatch.setattr(runtime_pid, "inspect_process", lambda _pid: _identity())

    reason = runtime_pid.unresolved_runtime_reason()

    assert reason is not None
    assert "invalid or stale generation" in reason


def test_inspect_process_parses_macos_ps_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    ps_output = (
        "  501 Sun Jul 12 12:24:49 2026     "
        "/tmp/example/.persome/venv/bin/python "
        "/tmp/example/.persome/venv/bin/persome start --foreground\n"
    )
    monkeypatch.setattr(
        runtime_pid.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, ps_output, ""),
    )

    identity = runtime_pid.inspect_process(4242)

    assert identity is not None
    assert identity.pid == 4242
    assert identity.uid == 501
    assert identity.command.endswith("persome start --foreground")
    assert runtime_pid.is_runtime_command(identity.command)
