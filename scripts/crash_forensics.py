#!/usr/bin/env python3
"""Crash forensics for the ThoughtMachine launcher.

WHY THIS EXISTS
---------------
The backend was killed by SIGKILL once and the crash destroyed its own evidence:
``start_thoughtmachine.sh`` launches the backend as
``( exec $1 ) > >(tee "$BACKEND_LOG") 2>&1 &``, and ``tee`` TRUNCATES that file on
every launch, while ``kill_thoughtmachine.sh`` wrote no log at all. The surviving
log began after the death, so the exact second, the memory trajectory and the
question "was the kill switch used?" were all unanswerable.

THE ONE RULE: INSTRUMENTATION, NOT A GATE
-----------------------------------------
Every subcommand here is FAIL-OPEN ON PURPOSE. The launcher calls them best-effort;
a forensic failure logs one warning and startup continues. Nothing in this module
may refuse, delay or crash a boot, because a machine that will not start is a worse
outcome than a machine that cannot explain a crash. ``--strict`` exists ONLY so the
tests can tell a real failure from a swallowed one; the launcher never passes it.

ARTIFACT RULES
--------------
* Append-only: every record is one JSON line appended to a ``*.jsonl`` file. The
  ONE exception is the self-cap trim (see ``_trim``): an append-only file that can
  grow without bound is its own outage, so a JSONL file is trimmed to its newest N
  records and the trim itself is written as a record (``event: "trim"``).
* Never truncated, never moved: logs are COPIED, so the original stays where the
  rest of the tooling expects it. Crash artifacts are never rewritten.
* Never writes to the vault (``~/.thoughtmachine``); everything lands in
  ``logs/crash/`` inside the repo, which is git-ignored (``logs/``, ``*.log``).

SUBCOMMANDS
-----------
archive-log   copy a non-empty log into DIR under a name derived from its mtime
header        append one environment record (pid/argv/host/kernel/mem/sha/grace)
record-exit   decode the launcher's ``$?`` and, on a signal death, snapshot the log
heartbeat     sample the backend pid every interval until it is gone, then stop
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import platform
import shutil
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

UNKNOWN = "UNKNOWN"

# Self-caps: a forensic file must not become the next outage.
RECEIPTS_MAX = 500          # header / record-exit / prune receipts
HEARTBEAT_MAX = 3000        # ~100 min at the launcher's 2 s interval
MIN_INTERVAL = 0.1          # never spin

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULTS_PY = REPO_ROOT / "agent" / "config" / "defaults.py"


# --------------------------------------------------------------------------- util
def _ts() -> str:
    """UTC, second resolution: the field every other record is correlated by."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _stamp(when: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(when))


def _degraded(name: str, exc: BaseException) -> str:
    return f"UNKNOWN: {name} unavailable ({type(exc).__name__}: {exc})"


def _free_name(directory: Path, stem: str, suffix: str = ".log") -> Path:
    """A name that does not exist yet -- crash artifacts are never overwritten."""
    candidate = directory / f"{stem}{suffix}"
    index = 1
    while candidate.exists():
        candidate = directory / f"{stem}-{index}{suffix}"
        index += 1
    return candidate


def _trim(path: Path, max_records: int) -> None:
    """Cap an append-only JSONL file to its newest ``max_records`` lines.

    The trim is itself a record, written as the newest file's oldest line, so a
    reader can see that history was dropped and how much. This is the single
    non-append write in the module and it exists so the append-only files cannot
    grow without bound.
    """
    if max_records <= 1:
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return
    if len(lines) <= max_records:
        return
    keep = max_records - 1                      # one slot goes to the trim record
    dropped = len(lines) - keep
    header = json.dumps(
        {"event": "trim", "ts": _ts(), "dropped": dropped, "kept": keep},
        sort_keys=True, separators=(",", ":"),
    )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join([header] + lines[-keep:]) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, record: Dict[str, Any], max_records: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, separators=(",", ":"))
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    _trim(path, max_records)


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------- 1. archive
def archive_log(log: Path, directory: Path, keep: int) -> Optional[Path]:
    """COPY a non-empty log into ``directory``; never move, never truncate.

    Returns the archive path, or None when the log is absent/empty (a first launch
    has nothing to archive -- that is not a failure).
    """
    if not log.is_file():
        return None
    try:
        stat = log.stat()
    except OSError:
        return None
    if stat.st_size == 0:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    # Name from the log's own mtime: the archive is labelled by when the bytes were
    # last written, not by when we happened to notice them.
    target = _free_name(directory, f"backend-{_stamp(stat.st_mtime)}")
    shutil.copy2(log, target)
    pruned = prune_archives(directory, keep)
    append_jsonl(
        directory / "receipts.jsonl",
        {
            "event": "archive",
            "ts": _ts(),
            "source": str(log),
            "archive": str(target),
            "bytes": stat.st_size,
            "source_mtime": _stamp(stat.st_mtime),
            "pruned": [str(p) for p in pruned],
        },
        RECEIPTS_MAX,
    )
    return target


def prune_archives(directory: Path, keep: int) -> List[Path]:
    """Delete the oldest ``backend-*.log`` archives beyond the newest ``keep``."""
    try:
        archives = sorted(
            (p for p in directory.glob("backend-*.log") if p.is_file()),
            key=lambda p: (p.stat().st_mtime, p.name),
        )
    except OSError:
        return []
    keep = max(1, int(keep))
    doomed = archives[:-keep] if len(archives) > keep else []
    removed = []
    for path in doomed:
        try:
            path.unlink()
            removed.append(path)
        except OSError:
            continue
    return removed


# ----------------------------------------------------------------------- 2. header
def _meminfo() -> Dict[str, int]:
    out: Dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                out[key] = int(rest.strip().split()[0])   # kB
    except Exception:
        return {}
    return out


def _repo_head() -> str:
    """git HEAD sha read straight from the files -- no subprocess, no side effects."""
    git_dir = REPO_ROOT / ".git"
    if git_dir.is_file():                       # worktree: 'gitdir: /path'
        try:
            text = git_dir.read_text().strip()
            if not text.startswith("gitdir:"):
                raise ValueError("unexpected .git file")
            git_dir = (REPO_ROOT / text.split(":", 1)[1].strip()).resolve()
        except Exception as exc:
            raise RuntimeError(f"worktree gitdir unreadable: {exc}") from exc
    head = git_dir / "HEAD"
    text = head.read_text().strip()
    if text.startswith("ref:"):
        ref = text.split(":", 1)[1].strip()
        loose = git_dir / ref
        if loose.is_file():
            return loose.read_text().strip()
        packed = git_dir / "packed-refs"
        for line in packed.read_text().splitlines():
            if line.endswith(" " + ref):
                return line.split()[0]
        raise RuntimeError(f"ref {ref} not found loose or packed")
    return text                                # detached HEAD: the sha itself


def _exec_kill_grace() -> Any:
    """EXEC_KILL_GRACE read from agent/config/defaults.py without importing it.

    Parsed with ``ast`` rather than imported: importing agent.config has side
    effects (it pulls the config layer), and a forensic probe must not perturb the
    thing it is measuring.
    """
    tree = ast.parse(DEFAULTS_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "EXEC_KILL_GRACE":
                    return ast.literal_eval(node.value)
    raise RuntimeError("EXEC_KILL_GRACE not assigned at module level")


def write_header(directory: Path, pid: Optional[int] = None,
                 argv: Optional[str] = None) -> Dict[str, Any]:
    record: Dict[str, Any] = {"event": "header", "ts": _ts()}
    degraded: Dict[str, str] = {}

    def put(name: str, fn: Callable[[], Any]) -> None:
        try:
            record[name] = fn()
        except Exception as exc:                # noqa: BLE001 -- one field, not the boot
            record[name] = UNKNOWN
            degraded[name] = _degraded(name, exc)

    # These two are legitimately absent sometimes: the launcher writes the header
    # before the backend's $! exists, and a caller may omit --argv. Absence is
    # recorded as a NAMED reason instead of raised, so the record still says why.
    if pid is None:
        record["backend_pid"] = UNKNOWN
        degraded["backend_pid"] = "UNKNOWN: header is written before the launch"
    else:
        record["backend_pid"] = int(pid)
    if not argv:
        record["argv"] = UNKNOWN
        degraded["argv"] = "UNKNOWN: launcher passed no --argv"
    else:
        record["argv"] = argv
    put("cwd", lambda: os.getcwd())
    put("host", socket.gethostname)
    put("kernel", lambda: platform.release())
    put("machine", lambda: platform.machine())
    put("python_version", lambda: platform.python_version())
    put("python_executable", lambda: sys.executable)
    put("mem_total_kb", lambda: _meminfo()["MemTotal"])
    put("mem_available_kb", lambda: _meminfo()["MemAvailable"])
    put("repo_head", _repo_head)
    put("exec_kill_grace", _exec_kill_grace)
    put("pid1_cmdline", lambda: Path("/proc/1/cmdline").read_bytes()
        .replace(b"\x00", b" ").decode("utf-8", "replace").strip() or UNKNOWN)
    if degraded:
        record["degraded"] = degraded
    append_jsonl(directory / "receipts.jsonl", record, RECEIPTS_MAX)
    return record


# ------------------------------------------------------------------ 3. record-exit
def decode_rc(rc: int) -> Dict[str, Any]:
    """Decode a wait status.

    bash reports a signal death as 128+N, so 137 -> SIGKILL, 143 -> SIGTERM. The
    encoding is AMBIGUOUS for a process that deliberately calls ``exit 137``: that
    is indistinguishable from SIGKILL (the shell has already collapsed the two into
    one integer). We disambiguate in favour of the signal reading, because 128+N is
    what the shell produces by itself and a deliberate exit-with-137 is rare; the
    raw ``rc`` is kept in every record so a human can re-read it, and the
    ``interpretation`` field says which reading was applied.
    A negative rc (python's subprocess convention, ``-N`` for signal N) is folded
    the same way, so library callers get the same answer as the shell.
    """
    rc = int(rc)
    if rc < 0:
        number = -rc
        return {"rc": rc, "signal": number, "signal_name": _signal_name(number),
                "interpretation": "negative rc is -signal (python subprocess convention)"}
    if rc > 128:
        number = rc - 128
        return {"rc": rc, "signal": number, "signal_name": _signal_name(number),
                "interpretation": "rc>128 read as 128+signal (bash wait convention)"}
    return {"rc": rc, "signal": None, "signal_name": None,
            "interpretation": "ordinary exit code (0, or 1..128)"}


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except Exception:
        return UNKNOWN


def record_exit(rc: int, log: Path, directory: Path) -> Dict[str, Any]:
    """Append the exit receipt and, on a signal death, snapshot the log NOW.

    The snapshot is the point of the whole module: this is the last instant those
    bytes exist before the next launch's ``tee`` truncates them.
    """
    decoded = decode_rc(rc)
    record: Dict[str, Any] = {"event": "exit", "ts": _ts()}
    record.update(decoded)
    record["archived"] = None
    record["archived_reason"] = "no snapshot: ordinary exit" if decoded["signal"] is None \
        else "no snapshot: log absent or empty"
    if decoded["signal"] is not None:
        try:
            if log.is_file() and log.stat().st_size > 0:
                directory.mkdir(parents=True, exist_ok=True)
                target = _free_name(directory, f"crashed-{_stamp(time.time())}")
                shutil.copy2(log, target)
                record["archived"] = str(target)
                record["archived_reason"] = "copied at reaping time"
                record["archived_bytes"] = target.stat().st_size
        except OSError as exc:
            record["archived_reason"] = f"copy failed ({type(exc).__name__}: {exc})"
    append_jsonl(directory / "receipts.jsonl", record, RECEIPTS_MAX)
    return record


# -------------------------------------------------------------------- 4. heartbeat
def _proc_kb(pid: int, field: str) -> Optional[int]:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith(field + ":"):
                return int(line.split()[1])     # kB
    except Exception:
        return None
    return None


def _proc_state(pid: int) -> Optional[str]:
    """The one-letter state from /proc/<pid>/stat ('Z' == zombie), or None.

    Taken from the third field: ``pid (comm) state ...``. ``comm`` may itself
    contain spaces AND parentheses, so the state is read from after the LAST ')'.
    """
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        return text.rsplit(")", 1)[1].split()[0]
    except Exception:
        return None


def _cgroup_memory(pid: int) -> Dict[str, Optional[Any]]:
    """memory.current / memory.max for this process's cgroup, v1 or v2."""
    out: Dict[str, Optional[Any]] = {"cgroup_memory_current": None,
                                     "cgroup_memory_max": None}
    try:
        lines = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    except Exception:
        return out
    rel = ""
    for line in lines:
        parts = line.split(":", 2)
        if len(parts) == 3 and (parts[1] == "memory" or parts[1] == ""):
            rel = parts[2].strip()
            break
    if not rel:
        return out
    for base in (Path("/sys/fs/cgroup") / rel.lstrip("/"),
                 Path("/sys/fs/cgroup/memory") / rel.lstrip("/")):
        current, maximum = base / "memory.current", base / "memory.max"
        if current.is_file() or maximum.is_file():
            for key, path in (("cgroup_memory_current", current),
                              ("cgroup_memory_max", maximum)):
                try:
                    out[key] = path.read_text().strip()
                except Exception:
                    pass
            return out
    return out


def _psi_some_avg10() -> Optional[float]:
    try:
        for line in Path("/proc/pressure/memory").read_text().splitlines():
            if line.startswith("some "):
                for token in line.split():
                    if token.startswith("avg10="):
                        return float(token.split("=", 1)[1])
    except Exception:
        return None
    return None


def heartbeat(pid: int, directory: Path, interval: float,
              max_records: int = HEARTBEAT_MAX) -> Dict[str, Any]:
    """Sample ``pid`` every ``interval`` seconds until it is gone, then stop.

    Lives in the LAUNCHER's process tree, never inside the backend, so it outlives
    the process it measures -- that is the only way the death itself gets recorded.
    It never outlives the backend by more than one interval, and it never spins.
    A process that has exited but not yet been reaped (state 'Z') counts as dead;
    its /proc entry lingers until its parent waits, and that is not a live backend.
    """
    interval = max(MIN_INTERVAL, float(interval))
    path = directory / "heartbeat.jsonl"
    last_seen: Optional[str] = None
    beats = 0
    while True:
        state = _proc_state(pid)
        # A ZOMBIE IS DEAD: /proc/<pid> still exists, but nothing will ever run
        # there again. Waiting for the dentry to vanish would leave this loop
        # running until whoever inherited the process gets around to reaping it.
        alive = os.path.isdir(f"/proc/{str(pid)}") and state != "Z"
        now = _ts()
        if not alive:
            record = {"event": "death", "ts": now, "pid": pid, "state": state,
                      "last_seen_ts": last_seen, "beats": beats,
                      "gap_seconds": None}
            if last_seen is not None:
                try:
                    last = time.mktime(time.strptime(last_seen, "%Y-%m-%dT%H:%M:%SZ"))
                    now_s = time.mktime(time.strptime(now, "%Y-%m-%dT%H:%M:%SZ"))
                    record["gap_seconds"] = round(now_s - last, 3)
                except Exception:
                    record["gap_seconds"] = None
            append_jsonl(path, record, max_records)
            return record
        record = {
            "event": "beat", "ts": now, "pid": pid, "alive": True, "state": state,
            "vmrss_kb": _proc_kb(pid, "VmRSS"),
            "mem_available_kb": _meminfo().get("MemAvailable"),
            "psi_some_avg10": _psi_some_avg10(),
        }
        record.update(_cgroup_memory(pid))
        append_jsonl(path, record, max_records)
        last_seen = now
        beats += 1
        time.sleep(interval)


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--strict", action="store_true",
                        help="TESTS ONLY: let a forensic failure exit non-zero. "
                             "The launcher never passes this -- the default is "
                             "fail-open (warn, exit 0).")
    sub = parser.add_subparsers(dest="command", required=True)

    arch = sub.add_parser("archive-log", help="copy the previous log aside")
    arch.add_argument("--log", required=True)
    arch.add_argument("--dir", required=True)
    arch.add_argument("--keep", type=int, default=10)

    head = sub.add_parser("header", help="append the environment record")
    head.add_argument("--dir", required=True)
    head.add_argument("--pid", type=int, default=None)
    head.add_argument("--argv", default=None)

    exitp = sub.add_parser("record-exit", help="decode rc, snapshot the log")
    exitp.add_argument("--rc", type=int, required=True)
    exitp.add_argument("--log", required=True)
    exitp.add_argument("--dir", required=True)

    beat = sub.add_parser("heartbeat", help="sample the pid until it is gone")
    beat.add_argument("--pid", type=int, required=True)
    beat.add_argument("--dir", required=True)
    beat.add_argument("--interval", type=float, default=2.0)
    beat.add_argument("--max-records", type=int, default=HEARTBEAT_MAX)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "archive-log":
            archive_log(Path(args.log), Path(args.dir), args.keep)
        elif args.command == "header":
            write_header(Path(args.dir), args.pid, args.argv)
        elif args.command == "record-exit":
            record_exit(args.rc, Path(args.log), Path(args.dir))
        elif args.command == "heartbeat":
            heartbeat(args.pid, Path(args.dir), args.interval, args.max_records)
    except Exception as exc:                    # noqa: BLE001 -- fail-open by design
        print(f"WARNING: crash forensics {args.command} failed: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1 if getattr(args, "strict", False) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
