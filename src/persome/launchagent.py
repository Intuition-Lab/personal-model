"""macOS LaunchAgent integration for a launchd-owned daemon process.

Design (issue #194):

- launchd becomes the *owner* of the daemon. The plist runs the daemon in the
  foreground (``persome start --foreground``); ``KeepAlive`` makes launchd
  relaunch it whenever it exits (crash, ``stop``, OOM). Lifecycle ownership stays
  in one place instead of an embedding product spawning a competing daemon.
- stdout/stderr are routed into ``logs/launchd.{out,err}.log`` under the data root
  so the diagnostic bundle (#168), which globs ``logs/``, picks them up unchanged.
- The plist itself must live in ``~/Library/LaunchAgents/`` — launchd only scans
  that directory for per-user agents. Everything else (label, log sinks) honours
  ``PERSOME_ROOT`` so tests stay hermetic.

This module is the single source of truth for the label, plist location, and
``launchctl`` invocations.
"""

from __future__ import annotations

import contextlib
import os
import plistlib
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import paths, runtime_pid

#: launchd job label.
LABEL = "com.persome.runtime"

#: Labels this agent shipped under before the Persome rename. ``install()``
#: boots these out and removes their plists so an upgraded machine never keeps
#: a second copy of the daemon alive under an old name. Product-specific
#: labels belong to the consumer (see CLAUDE.md); consumers with their own
#: legacy launchd labels clean those up themselves.
LEGACY_LABELS: tuple[str, ...] = ()


def plist_path() -> Path:
    """Absolute path to the agent plist. Always under ``~/Library/LaunchAgents``
    — launchd does not scan ``PERSOME_ROOT``."""
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def gui_domain_target() -> str:
    """``gui/<uid>/<label>`` service target used by modern ``launchctl`` verbs."""
    return f"gui/{os.getuid()}/{LABEL}"


def build_plist(binary: str) -> dict[str, object]:
    """Return the plist dict for the daemon, parameterised by the daemon
    ``binary`` path (the bundled ``persome`` executable).

    ``--foreground`` keeps the process in launchd's control group (no
    double-fork); ``KeepAlive=true`` provides crash-relaunch; ``RunAtLoad=true``
    starts it as soon as the agent is bootstrapped and on every login.
    """
    env: dict[str, str] = {}
    # Propagate the data-root override so a test/dev launchd job and the CLI
    # that registered it agree on where state lives.
    root_override = os.environ.get("PERSOME_ROOT")
    if root_override:
        env["PERSOME_ROOT"] = root_override

    plist: dict[str, object] = {
        "Label": LABEL,
        "ProgramArguments": [binary, "start", "--foreground"],
        "RunAtLoad": True,
        "KeepAlive": True,
        # launchd otherwise inherits the user session's commonly permissive
        # 022 umask. Runtime state contains raw screen context and must be born
        # owner-only even before the explicit chmod defense runs.
        "Umask": 0o077,
        "ProcessType": "Background",
        "StandardOutPath": str(paths.launchd_stdout_log()),
        "StandardErrorPath": str(paths.launchd_stderr_log()),
    }
    if env:
        plist["EnvironmentVariables"] = env
    return plist


def write_plist(binary: str) -> Path:
    """Render the plist for ``binary`` and write it to [plist_path]. Returns the
    path written."""
    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    paths.ensure_private_dir(paths.logs_dir())
    payload = plistlib.dumps(build_plist(binary), fmt=plistlib.FMT_XML).decode("utf-8")
    paths.atomic_write_private_text(target, payload)
    return target


def configured_binary_matches(binary: str) -> bool:
    """Whether the owner-only plist already describes this exact Runtime binary."""
    target = plist_path()
    try:
        paths.ensure_private_file(target)
        with target.open("rb") as handle:
            payload = plistlib.load(handle)
    except (FileNotFoundError, OSError, RuntimeError, plistlib.InvalidFileException):
        return False
    return payload.get("ProgramArguments") == [binary, "start", "--foreground"]


def configured_runtime_binary() -> str | None:
    """Return a valid configured daemon binary from the owner-only plist."""

    target = plist_path()
    try:
        paths.ensure_private_file(target)
        with target.open("rb") as handle:
            payload = plistlib.load(handle)
    except (FileNotFoundError, OSError, RuntimeError, plistlib.InvalidFileException):
        return None
    arguments = payload.get("ProgramArguments")
    if not isinstance(arguments, list) or arguments[1:] != ["start", "--foreground"]:
        return None
    binary = arguments[0] if arguments else None
    if not isinstance(binary, str) or not Path(binary).is_absolute():
        return None
    candidate = Path(binary)
    if candidate.is_symlink() or not candidate.is_file() or not os.access(candidate, os.X_OK):
        return None
    return binary


@dataclass(frozen=True)
class LoadedJob:
    """Minimal launchd state needed to bind a job to its daemon process."""

    pid: int
    program: str


def loaded_job() -> LoadedJob | None:
    """Return launchd's cached program and live PID for the Runtime job."""

    try:
        result = subprocess.run(
            ["launchctl", "print", gui_domain_target()],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    pid_match = re.search(r"(?m)^\s*pid\s*=\s*(\d+)\s*$", result.stdout)
    program_match = re.search(r"(?m)^\s*program\s*=\s*(.+?)\s*$", result.stdout)
    if pid_match is None or program_match is None:
        return None
    pid = int(pid_match.group(1))
    if pid <= 1:
        return None
    program = program_match.group(1).strip().strip('"')
    if not program:
        return None
    return LoadedJob(pid=pid, program=program)


def owns_recorded_runtime(binary: str) -> bool:
    """Whether launchd, the plist, and the generation receipt name one daemon."""

    job = loaded_job()
    process = runtime_pid.resolve_recorded_process()
    return bool(
        job is not None
        and job.program == binary
        and configured_binary_matches(binary)
        and process is not None
        and process.pid == job.pid
    )


def _wait_for_owned_runtime(binary: str, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if owns_recorded_runtime(binary):
            return True
        time.sleep(0.2)
    return owns_recorded_runtime(binary)


def owner_intended() -> bool:
    """Whether a prior successful install recorded launchd lifecycle intent."""

    marker = paths.launchagent_owner_file()
    try:
        paths.ensure_private_file(marker)
        return marker.read_text(encoding="utf-8").strip() == "enabled"
    except (FileNotFoundError, OSError, RuntimeError):
        return False


def _record_owner_intent() -> None:
    paths.atomic_write_private_text(paths.launchagent_owner_file(), "enabled\n")


def is_loaded() -> bool:
    """True iff launchd currently has the job registered (loaded), regardless
    of whether the process is up at this instant."""
    try:
        result = subprocess.run(
            ["launchctl", "print", gui_domain_target()],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def bootstrap() -> subprocess.CompletedProcess[str]:
    """Load the agent into the user's GUI domain. Idempotent-ish: launchd
    returns non-zero if already bootstrapped, which callers may ignore."""
    return subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path())],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def kickstart(*, kill: bool) -> subprocess.CompletedProcess[str]:
    """Ask launchd to start, or atomically replace, its owned daemon."""

    command = ["launchctl", "kickstart"]
    if kill:
        command.append("-k")
    command.append(gui_domain_target())
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def bootout() -> subprocess.CompletedProcess[str]:
    """Unload the agent from the user's GUI domain. Non-zero when not loaded."""
    return subprocess.run(
        ["launchctl", "bootout", gui_domain_target()],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _terminate_stray_daemon(timeout: float = 2.0) -> None:
    """SIGTERM a *live* daemon recorded in the pid file before we start a fresh
    one, then wait (up to ``timeout``) for it to exit.

    Two cases this covers, both of which would otherwise make the new daemon's
    ``start`` bail with "Already running (pid …)":

    1. **Orphan from a pre-launchd version** — old builds double-forked the
       daemon into the background (not under launchd). ``bootout()`` only stops
       the launchd job, so on app upgrade that orphan keeps running with its
       live pid in the pid file. Kill it so launchd's ``RunAtLoad`` start can
       take over (this is the "new dmg cleans up the old daemon" path).
    2. **Slow-to-die launchd daemon** — right after ``bootout()`` the old
       process may still be shutting down; waiting here avoids a port race with
       the daemon we're about to bootstrap.

    A stale/dead pid is ignored. Ambiguous live state fails closed so takeover
    can never create a second writer."""
    process = runtime_pid.signal_recorded_process(signal.SIGTERM)
    if process is None:
        problem = runtime_pid.unresolved_runtime_reason()
        if problem is not None:
            raise RuntimeError(f"cannot take over Runtime lifecycle: {problem}")
        return
    if not runtime_pid.wait_for_exit(process, timeout):
        raise RuntimeError(f"Persome daemon pid {process.pid} did not stop during takeover")


def _bootout_legacy_labels() -> None:
    """Best-effort cleanup of pre-rename launchd agents (see ``LEGACY_LABELS``).

    Boots each legacy job out of the GUI domain and removes its plist. The
    plist directory is derived from ``plist_path()`` so tests that redirect the
    plist location never touch the real ``~/Library/LaunchAgents``."""
    for label in LEGACY_LABELS:
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        legacy_plist = plist_path().parent / f"{label}.plist"
        try:
            if legacy_plist.exists():
                legacy_plist.unlink()
        except OSError:
            pass


def install(binary: str) -> Path:
    """Write the plist and bootstrap it. Returns the plist path. If the agent is
    already loaded, it is booted out first so the new ProgramArguments (e.g. a
    fresh binary path after an app upgrade) take effect. Any stray daemon (a
    pre-launchd orphan, or the just-booted-out one still exiting) is terminated
    first so the fresh ``start`` won't bail with "Already running"."""
    _bootout_legacy_labels()
    loaded = is_loaded()
    if loaded and configured_binary_matches(binary) and owns_recorded_runtime(binary):
        # The stable install path is unchanged across a venv swap. Keep a
        # generation that the updater has already fully proved.
        _record_owner_intent()
        return plist_path()
    if loaded:
        try:
            result = bootout()
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"could not stop the existing Persome LaunchAgent: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "launchctl bootout failed"
            raise RuntimeError(f"could not stop the existing Persome LaunchAgent: {detail}")
    _terminate_stray_daemon()
    target = write_plist(binary)
    try:
        result = bootstrap()
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"could not start the Persome LaunchAgent: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "launchctl bootstrap failed"
        raise RuntimeError(f"could not start the Persome LaunchAgent: {detail}")
    if not _wait_for_owned_runtime(binary):
        with contextlib.suppress(OSError):
            bootout()
        raise RuntimeError(
            "Persome LaunchAgent loaded but did not produce a matching daemon generation"
        )
    _record_owner_intent()
    return target


def uninstall() -> None:
    """Boot the agent out (if loaded) and remove the plist file."""
    if is_loaded():
        try:
            result = bootout()
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"could not stop the Persome LaunchAgent: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "launchctl bootout failed"
            raise RuntimeError(f"could not stop the Persome LaunchAgent: {detail}")
    target = plist_path()
    if target.exists():
        target.unlink()
    with contextlib.suppress(FileNotFoundError):
        paths.launchagent_owner_file().unlink()
