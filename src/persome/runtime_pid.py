"""Safe identity checks for the daemon PID recorded under ``PERSOME_ROOT``.

A PID by itself is not a process identity: after the daemon exits, macOS can
reuse that number for an unrelated process.  Lifecycle code must therefore
resolve the pid file to a verified :class:`ProcessIdentity` before sending a
signal, and re-check that identity immediately before the signal is delivered.

Current releases write the legacy, numeric-only pid file.  That format is
accepted conservatively during upgrades only when the process:

* is owned by the current user;
* has an exact Persome daemon command shape; and
* started no later than the pid file was written (so an obviously reused PID is
  rejected).

The versioned JSON format additionally pins owner, process start time, and
command.  ``write_current_process`` is provided for a future daemon migration;
readers support both formats now so lifecycle modules can migrate first.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import paths

PID_FILE_VERSION = 1
_MAX_PID_FILE_BYTES = 4096
_START_RE = re.compile(
    r"^\s*(?P<uid>\d+)\s+"
    r"(?P<start>[A-Za-z]{3}\s+[A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})"
    r"\s+(?P<command>.+?)\s*$"
)
_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}
_DAEMON_OPTIONS = {"--foreground", "-f", "--capture-only"}


@dataclass(frozen=True)
class ProcessIdentity:
    """A process identity stable across PID reuse for the available ps data."""

    pid: int
    uid: int
    started_at: float
    command: str
    generation: str | None = None
    runtime_started_at: float | None = None
    executable: str = ""


@dataclass(frozen=True)
class PIDFileRecord:
    """Parsed pid-file state.

    ``legacy`` records contain only a PID plus the trusted pid-file mtime.
    Versioned records carry the complete expected process identity.
    """

    pid: int
    file_mtime: float
    legacy: bool
    uid: int | None = None
    started_at: float | None = None
    command: str | None = None
    generation: str | None = None


@dataclass(frozen=True)
class RuntimeGeneration:
    """Identity fields published by the daemon's owner-only readiness state."""

    pid: int
    generation: str
    started_at: float
    updated_at: float


def _normalise_command(command: str) -> str:
    return " ".join(command.split())


def _valid_generation(value: object) -> str | None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        return None
    return value


def _valid_pid(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 1:
        return None
    return value


def _read_private_regular_file(path: Path) -> tuple[str, float] | None:
    """Read a small owner-owned regular file without following a symlink."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            return None
        payload = os.read(fd, _MAX_PID_FILE_BYTES + 1)
        if len(payload) > _MAX_PID_FILE_BYTES:
            return None
        try:
            return payload.decode("utf-8"), metadata.st_mtime
        except UnicodeDecodeError:
            return None
    finally:
        os.close(fd)


def read_pid_file(path: Path | None = None) -> PIDFileRecord | None:
    """Parse a legacy numeric or versioned runtime pid file.

    Invalid files, unsafe PIDs (including PID 0 and PID 1), symlinks, and
    incomplete versioned identities are rejected.
    """

    loaded = _read_private_regular_file(path or paths.pid_file())
    if loaded is None:
        return None
    raw, mtime = loaded
    stripped = raw.strip()
    try:
        legacy_pid = int(stripped)
    except ValueError:
        legacy_pid = None
    if legacy_pid is not None:
        pid = _valid_pid(legacy_pid)
        if pid is None:
            return None
        return PIDFileRecord(pid=pid, file_mtime=mtime, legacy=True)

    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != PID_FILE_VERSION:
        return None
    pid = _valid_pid(payload.get("pid"))
    uid = payload.get("uid")
    started_at = payload.get("started_at")
    command = payload.get("command")
    generation = _valid_generation(payload.get("generation"))
    if (
        pid is None
        or isinstance(uid, bool)
        or not isinstance(uid, int)
        or uid < 0
        or isinstance(started_at, bool)
        or not isinstance(started_at, (int, float))
        or not math.isfinite(started_at)
        or started_at <= 0
        or not isinstance(command, str)
        or not command.strip()
        or generation is None
    ):
        return None
    return PIDFileRecord(
        pid=pid,
        file_mtime=mtime,
        legacy=False,
        uid=uid,
        started_at=float(started_at),
        command=_normalise_command(command),
        generation=generation,
    )


def read_runtime_generation(path: Path | None = None) -> RuntimeGeneration | None:
    """Read the daemon generation from the owner-only runtime state sidecar."""

    loaded = _read_private_regular_file(path or paths.runtime_state_file())
    if loaded is None:
        return None
    raw, _mtime = loaded
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return None
    pid = _valid_pid(payload.get("pid"))
    generation = _valid_generation(payload.get("generation"))
    started_at = payload.get("started_at")
    updated_at = payload.get("updated_at")
    if (
        pid is None
        or generation is None
        or isinstance(started_at, bool)
        or not isinstance(started_at, (int, float))
        or not math.isfinite(started_at)
        or started_at <= 0
        or isinstance(updated_at, bool)
        or not isinstance(updated_at, (int, float))
        or not math.isfinite(updated_at)
        or updated_at < started_at
    ):
        return None
    return RuntimeGeneration(
        pid=pid,
        generation=generation,
        started_at=float(started_at),
        updated_at=float(updated_at),
    )


def _parse_started_at(value: str) -> float | None:
    """Parse macOS/BSD ``ps -o lstart=`` without depending on process locale."""

    parts = value.split()
    if len(parts) != 5 or parts[1] not in _MONTHS:
        return None
    try:
        hour, minute, second = (int(part) for part in parts[3].split(":"))
        local = datetime(
            int(parts[4]),
            _MONTHS[parts[1]],
            int(parts[2]),
            hour,
            minute,
            second,
        )
        return local.astimezone().timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def inspect_process(pid: int) -> ProcessIdentity | None:
    """Return owner, start time, and full command for ``pid`` via macOS ``ps``."""

    if _valid_pid(pid) is None:
        return None
    try:
        result = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "uid=", "-o", "lstart=", "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    matched = _START_RE.match(result.stdout)
    if matched is None:
        return None
    started_at = _parse_started_at(matched.group("start"))
    if started_at is None:
        return None
    return ProcessIdentity(
        pid=pid,
        uid=int(matched.group("uid")),
        started_at=started_at,
        command=_normalise_command(matched.group("command")),
        executable=_process_executable(pid) or "",
    )


def _process_executable(pid: int) -> str | None:
    """Return the kernel-reported executable path without parsing argv text.

    ``ps command`` cannot quote a frozen executable such as ``Persome Backend``
    reliably.  macOS libproc gives us the executable boundary independently of
    spaces; the ``/proc`` fallback keeps unit/dev use portable.
    """

    if sys.platform == "darwin":
        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            proc_pidpath = libproc.proc_pidpath
            proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            proc_pidpath.restype = ctypes.c_int
            buffer = ctypes.create_string_buffer(4096)
            length = int(proc_pidpath(pid, buffer, len(buffer)))
            if length > 0:
                return os.fsdecode(buffer.raw[:length])
        except (AttributeError, OSError, ValueError):
            return None
        return None
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return None


def _valid_daemon_arguments(arguments: list[str]) -> bool:
    return (
        bool(arguments)
        and arguments[0] == "start"
        and all(option in _DAEMON_OPTIONS for option in arguments[1:])
    )


def _is_python_executable(argument: str) -> bool:
    return Path(argument).name.lower().startswith("python")


def is_runtime_command(command: str, *, executable: str = "") -> bool:
    """Return whether ``command`` has an exact supported daemon argv shape."""

    try:
        arguments = shlex.split(command)
    except ValueError:
        return False
    if Path(executable).name == "Persome Backend":
        for index, argument in enumerate(arguments):
            if argument == executable or Path(argument).name == "Persome Backend":
                return _valid_daemon_arguments(arguments[index + 1 :])
    for index, argument in enumerate(arguments):
        if Path(argument).name == "persome":
            if index > 1 or (index == 1 and not _is_python_executable(arguments[0])):
                continue
            remainder = arguments[index + 1 :]
        elif (
            argument == "-m"
            and index == 1
            and _is_python_executable(arguments[0])
            and index + 1 < len(arguments)
            and arguments[index + 1]
            in {
                "persome",
                "persome.cli",
            }
        ):
            remainder = arguments[index + 2 :]
        else:
            continue
        return _valid_daemon_arguments(remainder)

    # PyInstaller ships a bare executable named ``Persome Backend``. macOS
    # renders its argv in ``ps`` without quoting the space, so shlex necessarily
    # sees two tokens. Trust the kernel-reported executable boundary, then parse
    # and validate only the remaining arguments.
    if Path(executable).name == "Persome Backend":
        normalised = _normalise_command(command)
        prefixes = (executable, str(Path(executable).resolve()))
        for prefix in prefixes:
            if normalised == prefix:
                return False
            if normalised.startswith(f"{prefix} "):
                try:
                    return _valid_daemon_arguments(shlex.split(normalised[len(prefix) :].strip()))
                except ValueError:
                    return False
        marker = "Persome Backend "
        marker_index = normalised.rfind(marker)
        if marker_index >= 0:
            try:
                return _valid_daemon_arguments(
                    shlex.split(normalised[marker_index + len(marker) :])
                )
            except ValueError:
                return False
    return False


def is_runtime_process(process: ProcessIdentity) -> bool:
    """Whether a kernel-inspected process has an exact Persome daemon shape."""

    return is_runtime_command(process.command, executable=process.executable)


def _matches_record(record: PIDFileRecord, process: ProcessIdentity) -> bool:
    if process.pid != record.pid or process.uid != os.getuid():
        return False
    if not is_runtime_process(process):
        return False
    if record.legacy:
        # The daemon writes its pid file after the process starts.  If the
        # process is newer than the legacy file, the PID has been reused.
        return process.started_at <= record.file_mtime
    return (
        process.uid == record.uid
        and process.started_at == record.started_at
        and process.command == record.command
    )


def resolve_recorded_process(path: Path | None = None) -> ProcessIdentity | None:
    """Resolve the pid file to a verified, currently live Persome process."""

    record = read_pid_file(path)
    if record is None:
        return None
    process = inspect_process(record.pid)
    if process is None or not _matches_record(record, process):
        return None
    state_path = paths.runtime_state_file()
    try:
        state_path.lstat()
    except FileNotFoundError:
        state = None
    except OSError:
        return None
    else:
        state = read_runtime_generation(state_path)
        # Once the new daemon sidecar exists, never downgrade to the weaker
        # legacy check because a malformed/stale sidecar is itself suspicious.
        if (
            state is None
            or state.pid != process.pid
            or process.started_at > state.started_at
            or (record.generation is not None and record.generation != state.generation)
        ):
            return None
    if state is None and record.generation is not None:
        # Versioned pid records are generation-bound and require the matching
        # readiness sidecar.  Numeric first-upgrade records remain compatible.
        return None
    if state is not None:
        return ProcessIdentity(
            pid=process.pid,
            uid=process.uid,
            started_at=process.started_at,
            command=process.command,
            generation=state.generation,
            runtime_started_at=state.started_at,
            executable=process.executable,
        )
    return process


def unresolved_runtime_reason(path: Path | None = None) -> str | None:
    """Explain recorded state that is unsafe to treat as a stopped Runtime.

    A dead PID or a PID reused by an unrelated process is safely stale and
    returns ``None``. An unreadable/malformed pid file, or a live Persome-shaped
    process whose generation sidecar cannot be verified, must stop lifecycle
    takeover rather than letting a second daemon start.
    """

    target = path or paths.pid_file()
    try:
        target.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"cannot inspect Runtime PID state at {target}: {exc}"
    record = read_pid_file(target)
    if record is None:
        return f"Runtime PID state at {target} is invalid or unsafe"
    process = inspect_process(record.pid)
    if process is None or not is_runtime_process(process):
        return None
    if resolve_recorded_process(target) is not None:
        return None
    return (
        f"live Persome Runtime pid {process.pid} has an invalid or stale generation receipt; "
        "refusing to start a second daemon"
    )


def same_process_is_running(process: ProcessIdentity) -> bool:
    """Return whether ``process`` still owns its PID with the same identity."""

    current = inspect_process(process.pid)
    return not (
        current is None
        or current.pid != process.pid
        or current.uid != process.uid
        or current.started_at != process.started_at
        or current.command != process.command
        or (
            bool(current.executable)
            and bool(process.executable)
            and current.executable != process.executable
        )
        or current.uid != os.getuid()
        or not is_runtime_process(current)
    )


def _same_runtime_generation(process: ProcessIdentity) -> bool:
    """Revalidate the optional daemon-owned generation sidecar."""

    if process.generation is None:
        return True
    state = read_runtime_generation()
    return (
        state is not None
        and state.pid == process.pid
        and state.generation == process.generation
        and state.started_at == process.runtime_started_at
    )


def signal_process(process: ProcessIdentity, sig: signal.Signals | int) -> bool:
    """Signal ``process`` only after immediately revalidating its full identity."""

    if not same_process_is_running(process) or not _same_runtime_generation(process):
        return False
    try:
        os.kill(process.pid, sig)
    except OSError:
        return False
    return True


def signal_recorded_process(
    sig: signal.Signals | int, path: Path | None = None
) -> ProcessIdentity | None:
    """Resolve and safely signal the runtime recorded by ``path``.

    Returns the pinned identity when a signal was sent, otherwise ``None``.
    """

    process = resolve_recorded_process(path)
    if process is None or not signal_process(process, sig):
        return None
    return process


def wait_for_exit(process: ProcessIdentity, timeout: float, *, interval: float = 0.1) -> bool:
    """Wait until this exact process exits; PID reuse counts as an exit."""

    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if not same_process_is_running(process):
            return True
        time.sleep(interval)
    return not same_process_is_running(process)


def write_current_process(*, generation: str, path: Path | None = None) -> ProcessIdentity:
    """Write a versioned identity for the current Persome daemon process.

    The daemon still writes a numeric pid file today.  This API lets it migrate
    atomically later without requiring lifecycle readers to change again.
    """

    valid_generation = _valid_generation(generation)
    if valid_generation is None:
        raise ValueError("generation must be 32 lowercase hexadecimal characters")
    process = inspect_process(os.getpid())
    if process is None or process.uid != os.getuid() or not is_runtime_process(process):
        raise RuntimeError("current process is not an identifiable Persome daemon")
    payload = json.dumps(
        {
            "version": PID_FILE_VERSION,
            "pid": process.pid,
            "uid": process.uid,
            "started_at": process.started_at,
            "command": process.command,
            "generation": valid_generation,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    paths.atomic_write_private_text(path or paths.pid_file(), payload)
    return process
