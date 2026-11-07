"""Container Record — on-disk storage primitives (§2).

Responsibilities:

* path conventions (§2.1) under ``<vault_root>/workspaces/<workspace_id>/containers/``
* atomic JSON writes (temp file + ``os.fsync`` + ``os.replace`` + ``0o600``)
* the per-record lock ``<id>.json.lock`` (§2.3, ``fcntl.flock`` with a
  bounded timeout; best-effort no-op where ``fcntl`` is unavailable,
  e.g. Windows)
* corrupt-file quarantine (§3.1)
* the ``<id>.events.jsonl`` sidecar writer (§1.2 / §2.4)

The vault root is resolved at call time via ``thoughtmachine.vault.vault_root``
so the standard precedence (``THOUGHTMACHINE_VAULT_ROOT`` env → ``~/.thoughtmachine``)
is honoured and stays monkeypatchable in tests.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import RecordCorrupt, RecordLocked

try:  # POSIX only; Windows degrades to atomic-write-only (§2.3).
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - exercised on Windows only
    fcntl = None  # type: ignore


# ── Constants ───────────────────────────────────────────────────────────────

CONTAINERS_SUBDIR = "containers"
WORKSPACES_SUBDIR = "workspaces"
MIGRATIONS_LOG_NAME = "migrations.log"

LOCK_SUFFIX = ".json.lock"
EVENTS_SUFFIX = ".events.jsonl"
CORRUPT_MARKER = ".corrupt-"

#: Embedded event_log ceiling (§1.2 / §2.4).  Above this the log moves to the
#: ``<id>.events.jsonl`` sidecar.
EVENT_LOG_CEILING_BYTES = 256 * 1024

#: Default per-record lock timeout in seconds (§2.3, "bounded timeout").
DEFAULT_LOCK_TIMEOUT = 10.0

DEFAULT_LOCK_POLL = 0.05


# ── Vault-root + path resolution (§2.1) ─────────────────────────────────────


def resolve_vault_root(vault_root: str | os.PathLike | None = None) -> Path:
    """Resolve the vault root: explicit arg → env → ``~/.thoughtmachine``."""
    if vault_root is not None:
        return Path(vault_root)
    # Imported lazily so monkeypatching ``thoughtmachine.vault.vault_root``
    # (the test harness does this) is always observed.
    from thoughtmachine import vault as _vault

    return Path(_vault.vault_root())


def containers_dir(
    workspace_id: str, vault_root: str | os.PathLike | None = None
) -> Path:
    """Return ``<vault_root>/workspaces/<workspace_id>/containers``."""
    return (
        resolve_vault_root(vault_root)
        / WORKSPACES_SUBDIR
        / str(workspace_id)
        / CONTAINERS_SUBDIR
    )


def record_path(
    workspace_id: str, record_id: str, vault_root: str | os.PathLike | None = None
) -> Path:
    """Return ``<vault_root>/workspaces/<workspace_id>/containers/<id>.json``."""
    return containers_dir(workspace_id, vault_root) / f"{record_id}.json"


def lock_path(
    workspace_id: str, record_id: str, vault_root: str | os.PathLike | None = None
) -> Path:
    """Return the sibling lock file ``<id>.json.lock`` (§2.3, verbatim)."""
    return containers_dir(workspace_id, vault_root) / f"{record_id}{LOCK_SUFFIX}"


def events_path(
    workspace_id: str, record_id: str, vault_root: str | os.PathLike | None = None
) -> Path:
    """Return the event-log sidecar ``<id>.events.jsonl`` (§2.4)."""
    return containers_dir(workspace_id, vault_root) / f"{record_id}{EVENTS_SUFFIX}"


def migrations_log_path(
    workspace_id: str, vault_root: str | os.PathLike | None = None
) -> Path:
    """Return the per-workspace migration write-ahead log (§4).

    NOTE: the doc writes this as ``<vault_root>/…/migrations.log`` (ambiguous);
    the subsystem places it inside the workspace ``containers/`` directory.
    """
    return containers_dir(workspace_id, vault_root) / MIGRATIONS_LOG_NAME


def iter_workspace_ids(vault_root: str | os.PathLike | None = None) -> list[str]:
    """Return workspace ids that own a ``containers/`` directory."""
    root = resolve_vault_root(vault_root) / WORKSPACES_SUBDIR
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / CONTAINERS_SUBDIR).is_dir())


def iter_record_files(containers: Path) -> list[Path]:
    """Return the ``<id>.json`` record files, ignoring sidecars and locks.

    Lock files (``.json.lock``), event sidecars (``.events.jsonl``) and
    quarantined files (``.json.corrupt-*``) are all excluded, so a
    ``<id>.json.lock`` is never mistaken for a record (§2.3).
    """
    containers = Path(containers)
    if not containers.is_dir():
        return []
    out: list[Path] = []
    for entry in sorted(containers.iterdir()):
        name = entry.name
        if not entry.is_file() or not name.endswith(".json"):
            continue
        if (
            name.endswith(LOCK_SUFFIX)
            or name.endswith(EVENTS_SUFFIX)
            or CORRUPT_MARKER in name
        ):
            continue
        out.append(entry)
    return out


# ── Atomic write ────────────────────────────────────────────────────────────


def atomic_write_json(path: str | os.PathLike, data: Any) -> None:
    """Write *data* as pretty JSON to *path* atomically.

    Unique temp via ``tempfile.mkstemp`` in the target directory, ``fsync`` the
    file, ``0o600`` mode, then ``os.replace`` (temp + replace, §4 step 6).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def write_record_file(path: str | os.PathLike, data: dict) -> None:
    """Atomically write a record dict to *path*."""
    atomic_write_json(path, data)


# ── Read + quarantine (§3.1) ────────────────────────────────────────────────


def quarantine_corrupt(path: str | os.PathLike, ts: str | None = None) -> Path:
    """Rename a corrupt record to ``<name>.corrupt-<ts>`` and return the new path."""
    path = Path(path)
    stamp = ts or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    dest = path.with_name(f"{path.name}{CORRUPT_MARKER}{stamp}")
    os.replace(path, dest)
    return dest


def read_record_file(path: str | os.PathLike) -> dict | None:
    """Read a record dict from *path*.

    Returns ``None`` when the file does not exist (pure lookup, §3.1).  A file
    that exists but cannot be parsed is quarantined and ``RecordCorrupt`` is
    raised — never a silent ``{}`` (§3.1).
    """
    path = Path(path)
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        quarantine_corrupt(path)
        raise RecordCorrupt(f"corrupt record quarantined: {path} ({exc})") from exc
    if not isinstance(data, dict):
        quarantine_corrupt(path)
        raise RecordCorrupt(f"corrupt record quarantined: {path} (not a JSON object)")
    return data


# ── Lock (§2.3) ─────────────────────────────────────────────────────────────


@contextlib.contextmanager
def record_lock(
    path: str | os.PathLike,
    timeout: float = DEFAULT_LOCK_TIMEOUT,
    poll_interval: float = DEFAULT_LOCK_POLL,
) -> Iterator[None]:
    """Hold an exclusive lock on the sidecar *path* for the ``with`` body.

    Uses ``fcntl.flock(LOCK_EX)`` polled with a bounded *timeout*; on timeout
    ``RecordLocked`` is raised (fail closed).  Where ``fcntl`` is unavailable
    the lock degrades to a no-op (Windows best-effort, §2.3).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if fcntl is None:  # pragma: no cover - Windows only
        yield
        return

    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RecordLocked(
                        f"could not acquire record lock {path} within {timeout}s"
                    )
                time.sleep(poll_interval)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ── Event-log sidecar (§1.2 / §2.4) ─────────────────────────────────────────


def _append_line(path: Path, line: str) -> None:
    """Append one UTF-8 line to *path* and ``fsync`` it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def append_event_line(path: str | os.PathLike, entry: dict) -> None:
    """Append one JSON event entry (one line) to the sidecar (append-only)."""
    _append_line(Path(path), json.dumps(entry, sort_keys=True) + "\n")


def write_events_sidecar(path: str | os.PathLike, entries: list[dict]) -> None:
    """(Re)write the sidecar with *entries*, one JSON object per line."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def append_migration_line(path: str | os.PathLike, entry: dict) -> None:
    """Append one write-ahead entry to the migrations log and ``fsync`` (§4)."""
    _append_line(Path(path), json.dumps(entry, sort_keys=True) + "\n")


def read_events_sidecar(path: str | os.PathLike) -> list[dict]:
    """Return the event entries from the ``.events.jsonl`` sidecar (§2.4).

    One JSON object per line; blank/unparseable lines are skipped.  Missing
    file yields an empty list (pure lookup).
    """
    path = Path(path)
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:  # pragma: no cover - defensive
            continue
    return out
