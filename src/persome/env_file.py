"""Dotenv-format env loader for daemon startup.

Runtime secrets live in ``~/.persome/env``. A user may edit that owner-only
file directly, or an embedding product may mirror secrets from its own secure
store. Business code stays on ``os.environ.get(...)``; initialized CLI commands
merge the file's contents into ``os.environ`` before doing work, and ``start``
does so before forking.

Semantics:

* Already-set env vars win (shell ``export`` for CLI debugging keeps priority).
* Missing file is fine — returns 0.
* Format is minimal dotenv: ``KEY=VALUE`` per line, ``#`` comments, blank lines
  ignored, optional single/double-quoted values (quotes stripped, no escapes).
* No shell expansion, no ``$VAR`` interpolation — behavior is identical for
  direct CLI and embedding-product launch paths.
* ``PERSOME_ROOT`` is never sourced from the file stored under that root. The
  parent process must select the data root before the owner env is located.
"""

from __future__ import annotations

import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Literal

SCREENSHOT_KEY_ENV = "PERSOME_SCREENSHOT_KEY"
_SCREENSHOT_KEY_HEX_LENGTH = 64
LOCAL_API_TOKEN_ENV = "PERSOME_LOCAL_API_TOKEN"
_LOCAL_API_TOKEN_MIN_BYTES = 32
_LOCAL_API_TOKEN_MAX_BYTES = 512
_LOCAL_API_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
_OWNER_ENV_BLOCKED_KEYS = frozenset({"PERSOME_ROOT"})

ScreenshotKeyStatus = Literal["existing", "generated"]
LocalAPITokenStatus = Literal["existing", "generated"]


def _parse_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "=" not in line:
        return None
    key, _, value = line.partition("=")
    key = key.strip()
    if not key or not key.replace("_", "").isalnum():
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return key, value


def load_env_file(path: Path) -> int:
    """Merge ``path`` into ``os.environ``. Returns the number of keys added.

    Pre-existing env vars are NOT overwritten. Unreadable / missing files are
    silently ignored (returns 0) — the daemon will surface a clearer error
    later when the selected provider credential lookup comes back empty.
    """
    if path.is_symlink() or not path.exists():
        return 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    added = 0
    for line in text.splitlines():
        parsed = _parse_line(line)
        if parsed is None:
            continue
        key, value = parsed
        if key in _OWNER_ENV_BLOCKED_KEYS:
            continue
        if key in os.environ:
            continue
        os.environ[key] = value
        added += 1
    return added


def write_env_values(path: Path, updates: dict[str, str]) -> None:
    """Atomically upsert dotenv values while preserving unrelated entries.

    Used by guided onboarding so credentials are durable across daemon restarts.
    Updated keys are emitted once at the end of the owner-only file.
    """
    if path.is_symlink():
        raise RuntimeError(f"environment file must not be a symlink: {path}")
    invalid = [key for key in updates if not key or not key.replace("_", "").isalnum()]
    if invalid:
        raise ValueError(f"invalid environment variable name: {invalid[0]}")
    if any("\n" in value or "\r" in value for value in updates.values()):
        raise ValueError("environment variable values must be single-line")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []

    kept: list[str] = []
    for line in lines:
        parsed = _parse_line(line)
        if parsed is not None and parsed[0] in updates:
            continue
        kept.append(line)
    kept.extend(f"{key}={value}" for key, value in updates.items())
    payload = "\n".join(kept).rstrip("\n") + "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)

    for key, value in updates.items():
        os.environ[key] = value


def is_valid_screenshot_key(value: str | None) -> bool:
    if value is None or len(value) != _SCREENSHOT_KEY_HEX_LENGTH:
        return False
    try:
        return len(bytes.fromhex(value)) == 32
    except ValueError:
        return False


def is_valid_local_api_token(value: str | None) -> bool:
    """Return whether ``value`` is a durable URL-safe bearer credential.

    The canonical value is persisted without a dotenv escaping layer. Using
    the same alphabet as ``token_urlsafe`` makes that serialization exactly
    reversible for every later CLI or daemon process.
    """
    if value is None or _LOCAL_API_TOKEN_RE.fullmatch(value) is None:
        return False
    length = len(value)
    return _LOCAL_API_TOKEN_MIN_BYTES <= length <= _LOCAL_API_TOKEN_MAX_BYTES


def ensure_local_api_token(path: Path) -> LocalAPITokenStatus:
    """Generate and preserve one owner-only local REST/MCP bearer token.

    The value is never returned or logged. Invalid/duplicate entries are
    replaced atomically with a fresh high-entropy URL-safe token. A valid
    shell-provided token becomes the durable canonical value so the daemon and
    later CLI processes cannot diverge onto different bearer credentials.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []

    canonical: str | None = None
    for line in lines:
        parsed = _parse_line(line)
        if parsed is None:
            continue
        key, value = parsed
        if key == LOCAL_API_TOKEN_ENV:
            if canonical is None and is_valid_local_api_token(value):
                canonical = value
            continue

    shell_override = os.environ.get(LOCAL_API_TOKEN_ENV)
    if is_valid_local_api_token(shell_override):
        canonical = shell_override
        status: LocalAPITokenStatus = "existing"
    elif canonical is None:
        canonical = secrets.token_urlsafe(48)
        status = "generated"
    else:
        status = "existing"
    write_env_values(path, {LOCAL_API_TOKEN_ENV: canonical})
    return status


def ensure_screenshot_key(path: Path) -> ScreenshotKeyStatus:
    """Ensure ``path`` contains one valid machine-local screenshot key.

    The installer calls this after creating its virtualenv. Existing canonical
    keys are preserved. Missing or malformed values are replaced with a freshly
    generated 256-bit key. The key is never returned or logged, and the dotenv
    file is atomically rewritten with mode ``0600``.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""

    lines = text.splitlines()
    canonical: str | None = None
    kept: list[str] = []
    for line in lines:
        parsed = _parse_line(line)
        if parsed is None:
            kept.append(line)
            continue
        key, value = parsed
        if key == SCREENSHOT_KEY_ENV:
            if canonical is None and is_valid_screenshot_key(value):
                canonical = value
            continue
        kept.append(line)

    if canonical is not None:
        value = canonical
        status: ScreenshotKeyStatus = "existing"
    else:
        value = secrets.token_hex(32)
        status = "generated"

    kept.append(f"{SCREENSHOT_KEY_ENV}={value}")
    payload = "\n".join(kept).rstrip("\n") + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return status
