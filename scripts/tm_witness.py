#!/usr/bin/env python3
"""tm_witness.py -- append-only process witness / supervisor.

Usage:
    tm_witness.py --ledger logs/witness/ledger.jsonl -- <command> [args...]

Why this exists
---------------
The backend dies with no evidence.  This supervisor spawns the backend, records
its birth and death in an append-only JSONL ledger that survives a reboot, and
captures *who* sent a killing signal before it forwards that signal, so the
backend can still shut down gracefully.

Non-negotiable properties
-------------------------
* Append-only: O_APPEND, one write(2) per record, fsync(2) immediately.
  Nothing is ever truncated, rewritten or reordered.
* Signal provenance: SIGTERM/SIGINT/SIGHUP are blocked in the main thread
  *before* the sigwaitinfo worker thread is created (so the worker inherits the
  block).  The signal record is on disk before the signal is forwarded.
* Refusal, not a half-witness: the capabilities this supervisor needs are
  probed, not inferred from a platform name -- `signal.sigwaitinfo` (without it
  there is no sender provenance) and a readable
  `/proc/sys/kernel/random/boot_id` (without it boot identity is unavailable).
  When either is missing the witness REFUSES before the fork and before the
  ledger is opened: stderr names the missing capability, nothing is spawned and
  no ledger is created, and the exit code is 78 (EX_CONFIG).  A witness that
  cannot attribute a death would file its own kills as unattributable signals,
  which is worse than no witness at all.
* fork() happens while the process is still SINGLE-THREADED (the waiter thread
  is started afterwards, with the child pid already published): forking from a
  multi-threaded process raises a DeprecationWarning on 3.12 and can deadlock
  the child.  A signal that arrives before the waiter runs stays pending in the
  kernel and is picked up by sigwaitinfo() when it starts.
* The blocked mask is inherited across execve(), so the child is forked and
  clears it itself with pthread_sigmask(SIG_SETMASK, []).  A preexec_fn is
  deliberately NOT used: it is unsafe in a threaded process.
* The child arms PR_SET_PDEATHSIG(SIGKILL) before exec, so it dies with this
  supervisor even if the supervisor itself is SIGKILLed.  Without that, a
  SIGKILLed witness would leave an orphaned backend holding port 8000.
  If the mask were inherited, the child would never see SIGTERM and the backend
  would hang instead of exiting -- tests/test_tm_witness.py fails in that case.
* Rotation is by size only, archive-by-atomic-rename, and derived archive files
  live in the ledger's own directory.  Paths containing '..' are refused.

Shutdown provenance
-------------------
`dirty_shutdown` used to be `signaled_by is not None`, i.e. every signalled death
was called dirty.  That is wrong for the backend we supervise: uvicorn
(server.py, HANDLED_SIGNALS = (SIGINT, SIGTERM), capture_signals()) restores the
original handlers in a `finally` and then re-raises the captured signal, so a
SIGTERM *we forwarded* produces a signalled death that is the documented
orderly outcome, not a crash.  The exit record therefore carries `shutdown`,
derived from the wait status AND from the signals this witness actually
forwarded (`GRACE_WINDOW_S` before the exit):

    orderly              a SIGTERM/SIGINT we forwarded ended it, and the
                         process took its normal exit path
    clean_exit           exited 0 without being asked
    crash                exited non-zero on its own
    killed               died from SIGKILL, which nobody can forward
    unhandled_signal     died from a SIGHUP we forwarded: uvicorn installs no
                         SIGHUP handler, so death by HUP is what happens, but it
                         is still an unexpected request
    unattributed_signal  died from a signal we NEVER forwarded, i.e. from a
                         killer outside this witness (also the verdict for a
                         forwarded signal older than GRACE_WINDOW_S: a stale
                         forward we cannot prove caused this death)
    unknown              a wait status we cannot read

Only `orderly` and `clean_exit` are non-dirty.

`live_children`
---------------
The death snapshot lists the processes still parented to this witness (pid,
comm, argv from /proc/<pid>/cmdline) after a `SETTLE_WINDOW_S` settle, with
zombies excluded: a pid that merely has not been reaped yet is not a leftover.
`leftover_children` (which only ever reported already-dead pids) is gone.
"""

import argparse
import ctypes
import datetime
import json
import os
import shutil
import signal
import sys
import threading
import time
import uuid

SIGNAL_SET = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
# How long after forwarding a signal a child's death may still be attributed to
# it.  Anything older is not evidence: a stale forward from minutes ago cannot
# explain the death we are looking at now.
GRACE_WINDOW_S = 30.0
# Time given to the kernel to finish tearing the child down before the death
# snapshot enumerates survivors.
SETTLE_WINDOW_S = 0.2
ROTATE_BYTES_DEFAULT = 8 * 1024 * 1024
KEEP_ARCHIVES_DEFAULT = 5
READ_CAP_BYTES = 8 * 1024 * 1024
WITNESS_DIRNAME = "witness"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LEDGER = os.path.join(REPO_ROOT, "logs", WITNESS_DIRNAME, "ledger.jsonl")


# --------------------------------------------------------------------------- #
# small /proc helpers
# --------------------------------------------------------------------------- #
def utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _utc_stamp():
    # Microsecond resolution: second-resolution stamps collided on every
    # rotation inside the same second, and a freed name was then reused, which
    # made name order (and therefore pruning) non-chronological.
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def read_boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id", "r") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


#: Exit code for "this host cannot be supervised at all".  78 is EX_CONFIG from
#: sysexits.h: the machine is misconfigured for this program, the command is
#: not.  The other non-zero codes in this module are runtime outcomes (127 fork
#: failure, the child's own status, 128+signo for a signalled death), so this is
#: deliberately outside them.
EX_UNSUPPORTED = 78


def unsupported_reason():
    """Why this host cannot be supervised, or None when it can.

    Probed by capability, never by a ``sys.platform`` string: what matters is
    that the kernel can report WHO sent a signal (``signal.sigwaitinfo``) and
    that boot identity is readable.  WSL, containers and the BSD/kqueue world
    all sit between the extremes a platform name implies, and a wrong guess here
    is a witness that silently records nothing worth reading.
    """
    if not hasattr(signal, "sigwaitinfo"):
        return (
            "signal.sigwaitinfo is missing, so the kernel cannot report the "
            "sender of a signal"
        )
    if read_boot_id() is None:
        return (
            "/proc/sys/kernel/random/boot_id is unreadable, so boot identity "
            "is unavailable"
        )
    return None


def read_uptime_s():
    try:
        with open("/proc/uptime", "r") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _slurp(path):
    """Whole file as text, or None when it cannot be read."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _is_sha(value):
    """True for a full 40-hex object name and nothing else.

    Deliberately strict: a 7-character abbreviation, a ref name or a truncated
    read must never be recorded as the commit this process was born at.
    """
    return len(value) == 40 and all(ch in "0123456789abcdefABCDEF" for ch in value)


def _resolve_gitdirs(repo_root):
    """(gitdir, commondir) for `repo_root`, or (None, None) if it is not a checkout.

    `.git` is a DIRECTORY in a normal checkout.  In a worktree it is a FILE
    whose first line is `gitdir: <path>`, and that path may be relative to the
    repo root.  A worktree's gitdir then usually names its own `commondir`
    (relative to the gitdir): the per-worktree gitdir holds HEAD and index,
    while refs/ and packed-refs live in the COMMON dir.
    """
    dot_git = os.path.join(repo_root, ".git")
    gitdir = None
    if os.path.isdir(dot_git):
        gitdir = dot_git
    elif os.path.isfile(dot_git):
        text = _slurp(dot_git)
        if text is None:
            return None, None
        lines = text.splitlines()
        first = lines[0].strip() if lines else ""
        if not first.startswith("gitdir:"):
            return None, None
        target = first[len("gitdir:"):].strip()
        if not target:
            return None, None
        gitdir = target if os.path.isabs(target) else os.path.join(repo_root, target)
    if gitdir is None:
        return None, None

    commondir = gitdir
    common_file = os.path.join(gitdir, "commondir")
    if os.path.isfile(common_file):
        text = _slurp(common_file)
        if text is None:
            return None, None
        target = text.strip()
        if not target:
            return None, None
        commondir = target if os.path.isabs(target) else os.path.join(gitdir, target)
    return gitdir, commondir


def _packed_ref(commondir, ref):
    """The sha `ref` is pinned to in <commondir>/packed-refs, or None.

    Peeled tags (`^<sha>`) and comments start with '^' and '#', and are not
    ref records, so they are skipped rather than parsed.
    """
    text = _slurp(os.path.join(commondir, "packed-refs"))
    if text is None:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] in "#^":
            continue
        parts = stripped.split()
        if len(parts) == 2 and parts[1] == ref and _is_sha(parts[0]):
            return parts[0]
    return None


def read_git_head():
    """The commit sha at HEAD, read from git's own files -- no subprocess.

    Read instead of spawned because this module is the launch gate: the only
    process it may create is the supervised child, and asking git for the sha
    would mean one more host process outside host_run()'s chokepoint.

    Handles: `.git` a directory (plain checkout) or a file (`gitdir:` worktree
    pointer, relative or absolute); a worktree gitdir with a `commondir` (refs
    and packed-refs live there); HEAD as `ref: refs/heads/x` with the loose ref
    present, or absent and therefore read out of `packed-refs`; HEAD as a bare
    40-hex sha (detached).  Anything unreadable or unparseable returns None --
    never a guessed or partial sha.
    """
    gitdir, commondir = _resolve_gitdirs(REPO_ROOT)
    if gitdir is None or commondir is None:
        return None
    head = _slurp(os.path.join(gitdir, "HEAD"))
    if head is None:
        return None
    head = head.strip()

    if _is_sha(head):
        return head
    if not head.startswith("ref:"):
        return None
    ref = head[len("ref:"):].strip()
    if not ref:
        return None

    loose = _slurp(os.path.join(commondir, ref))
    if loose is not None:
        value = loose.strip()
        return value if _is_sha(value) else None
    return _packed_ref(commondir, ref)


def new_run_id():
    return "%s-%d-%s" % (_utc_stamp(), os.getpid(), uuid.uuid4().hex[:8])


def signal_name(signo):
    try:
        return signal.Signals(int(signo)).name
    except (ValueError, TypeError):
        return "SIG%d" % int(signo)


def resolve_sender(si_pid):
    """Resolve the sender of a signal from /proc.  si_pid == 0 -> kernel."""
    si_pid = int(si_pid or 0)
    if si_pid <= 0:
        return {"sender_comm": None, "sender_cmdline": None, "origin": "kernel"}
    comm = None
    try:
        with open("/proc/%d/comm" % si_pid, "r") as fh:
            comm = fh.read().strip() or None
    except OSError:
        comm = None
    cmdline = None
    try:
        with open("/proc/%d/cmdline" % si_pid, "rb") as fh:
            raw = fh.read(4096).split(b"\x00")
        parts = [chunk.decode("utf-8", "replace") for chunk in raw if chunk]
        cmdline = " ".join(parts) if parts else None
    except OSError:
        cmdline = None
    return {"sender_comm": comm, "sender_cmdline": cmdline, "origin": "proc"}


def newest_startup_log():
    log_dir = os.path.join(REPO_ROOT, "logs")
    best = None
    try:
        names = os.listdir(log_dir)
    except OSError:
        names = []
    for name in names:
        if not name.startswith("backend_startup") or not name.endswith(".log"):
            continue
        path = os.path.join(log_dir, name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if best is None or stat.st_mtime > best[1]:
            best = (path, stat.st_mtime, stat.st_size)
    if best is None:
        path = os.path.join(log_dir, "backend_startup.log")
        try:
            stat = os.stat(path)
        except OSError:
            return None
        best = (path, stat.st_mtime, stat.st_size)
    return {
        "path": best[0],
        "mtime_iso": datetime.datetime.fromtimestamp(
            best[1], datetime.timezone.utc
        ).isoformat(),
        "size": int(best[2]),
    }


def _parse_pid_list(text):
    pids = []
    for token in str(text).split():
        try:
            pids.append(int(token))
        except ValueError:
            continue
    return pids


def _children_from_task_files(pid):
    """/proc/<pid>/task/<tid>/children: exact, and correct for every thread.

    Returns None when not one children file could be read, so the caller can
    fall back to a /proc/*/stat scan (that file needs CONFIG_PROC_CHILDREN).
    """
    pid = int(pid)
    found = []
    readable = False
    try:
        tids = os.listdir("/proc/%d/task" % pid)
    except OSError:
        return None
    for tid in tids:
        try:
            with open("/proc/%d/task/%s/children" % (pid, tid), "r") as fh:
                text = fh.read()
        except OSError:
            continue
        readable = True
        found.extend(_parse_pid_list(text))
    return found if readable else None


def _children_from_stat_scan(pid):
    """/proc/*/stat fallback: the children are the pids whose ppid is `pid`."""
    pid = int(pid)
    found = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return found
    for name in names:
        if not name.isdigit():
            continue
        state_and_ppid = _stat_tail(int(name))
        if state_and_ppid is None:
            continue
        try:
            if int(state_and_ppid[1]) == pid:
                found.append(int(name))
        except (IndexError, ValueError):
            continue
    return found


def _stat_tail(pid):
    """Fields of /proc/<pid>/stat after the comm field: [state, ppid, ...].

    comm is parenthesised and may itself contain ')' or spaces, so the split is
    on the LAST ')' -- never on the first one.
    """
    try:
        with open("/proc/%d/stat" % int(pid), "r") as fh:
            data = fh.read()
    except OSError:
        return None
    tail = data.rsplit(")", 1)[-1].split()
    return tail or None


def _proc_state(pid):
    tail = _stat_tail(pid)
    return tail[0] if tail else None


def describe_process(pid):
    """pid/comm/cmdline for a live process, or None when it is already gone."""
    pid = int(pid)
    try:
        with open("/proc/%d/comm" % pid, "r") as fh:
            comm = fh.read().strip() or None
    except OSError:
        return None
    cmdline = None
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            raw = fh.read(4096)
        parts = [chunk.decode("utf-8", "replace") for chunk in raw.split(b"\x00") if chunk]
        cmdline = parts or None
    except OSError:
        cmdline = None
    return {"pid": pid, "comm": comm, "cmdline": cmdline}


def enumerate_children(pid=None):
    """Which processes are STILL parented to `pid` (default: this process).

    Zombies are skipped: they have already exited and only await a waitpid, so
    reporting them would call every reaped-late child a survivor.  A pid that
    vanished between the listing and the /proc read is dropped too.
    """
    pid = os.getpid() if pid is None else int(pid)
    candidates = _children_from_task_files(pid)
    if candidates is None:
        candidates = _children_from_stat_scan(pid)
    live = []
    for child_pid in sorted(set(int(child) for child in candidates)):
        if _proc_state(child_pid) == "Z":
            continue
        info = describe_process(child_pid)
        if info is not None:
            live.append(info)
    return live


def reap_exited_children(limit=16):
    """Bounded WNOHANG reap: collect exit statuses nobody else has taken."""
    reaped = []
    for _ in range(int(limit)):
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except (ChildProcessError, OSError):
            break
        if pid == 0:
            break
        reaped.append(int(pid))
    return reaped


# --------------------------------------------------------------------------- #
# append-only ledger
# --------------------------------------------------------------------------- #
class Ledger(object):
    def __init__(self, path=DEFAULT_LEDGER, rotate_bytes=ROTATE_BYTES_DEFAULT,
                 keep_archives=KEEP_ARCHIVES_DEFAULT):
        if ".." in os.path.normpath(path).split(os.sep):
            raise SystemExit("tm_witness: refusing ledger path containing '..': %r" % (path,))
        self.path = os.path.abspath(path)
        self.directory = os.path.dirname(self.path)
        self.rotate_bytes = int(rotate_bytes)
        self.keep_archives = int(keep_archives)
        self._lock = threading.Lock()
        os.makedirs(self.directory, exist_ok=True)
        self.fd = self._open()

    def _open(self):
        # O_APPEND: every write lands at the current end of file. Never O_TRUNC.
        return os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)

    def append(self, record):
        blob = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            os.write(self.fd, blob)
            os.fsync(self.fd)
            self._maybe_rotate()
        return len(blob)

    def _size(self):
        try:
            return os.fstat(self.fd).st_size
        except OSError:
            return 0

    def _archive_parts(self):
        name = os.path.basename(self.path)
        stem, ext = os.path.splitext(name)
        return stem, (ext or ".jsonl")

    def _archives(self):
        stem, ext = self._archive_parts()
        live = os.path.basename(self.path)
        found = []
        try:
            names = os.listdir(self.directory)
        except OSError:
            return found
        for name in names:
            if name == live or not name.endswith(ext):
                continue
            if not name.startswith(stem + "."):
                continue
            found.append(os.path.join(self.directory, name))
        found.sort()  # UTC timestamps in the name sort chronologically
        return found

    def _maybe_rotate(self):
        if self.rotate_bytes <= 0 or self._size() < self.rotate_bytes:
            return None
        return self.rotate()

    def rotate(self):
        """Archive the current file by atomic rename (no truncation, no data loss,
        the newest record stays inside the archive)."""
        stem, ext = self._archive_parts()
        stamp = _utc_stamp()
        target = os.path.join(self.directory, "%s.%s%s" % (stem, stamp, ext))
        serial = 1
        while os.path.exists(target):
            # '_' sorts after '.', so name order stays chronological
            # zero padded so _0002 sorts before _0010
            target = os.path.join(self.directory, "%s.%s_%04d%s" % (stem, stamp, serial, ext))
            serial += 1
        os.close(self.fd)
        os.replace(self.path, target)
        self.fd = self._open()
        self._prune_archives(protect=target)
        return target

    def _prune_archives(self, protect=None):
        if self.keep_archives <= 0:
            return
        archives = self._archives()
        protected = os.path.abspath(protect) if protect else None
        for stale in archives[: max(0, len(archives) - self.keep_archives)]:
            if protected is not None and os.path.abspath(stale) == protected:
                continue  # never drop the archive we just created
            try:
                os.unlink(stale)
            except OSError:
                pass

    def close(self):
        with self._lock:
            try:
                os.close(self.fd)
            except OSError:
                pass


def load_records(path, cap=READ_CAP_BYTES):
    """Read ledger records (tail-capped for bounded work)."""
    records = []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            truncated = size > cap
            if truncated:
                fh.seek(size - cap)
            data = fh.read()
    except OSError:
        return records
    lines = data.split(b"\n")
    if truncated and lines:
        lines = lines[1:]  # first line is probably partial
    for raw in lines:
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def evaluate_previous_run(records, current_boot_id, run_id=None):
    """Post-mortem of the most recent run in `records`.

    Returns a verdict record dict, or None when the previous run ended cleanly.
    - birth, no exit  -> "reboot" if the boot_id differs, else "hard_kill_no_signal"
    - exit killed by a signal we never witnessed -> "hard_kill_no_signal"
    """
    birth_index = None
    for index in range(len(records) - 1, -1, -1):
        if records[index].get("event") == "birth":
            birth_index = index
            break
    if birth_index is None:
        return None
    birth = records[birth_index]
    prior_run_id = birth.get("run_id")
    prior_boot_id = birth.get("boot_id")

    exit_record = None
    seen_signos = set()
    for record in records[birth_index + 1:]:
        if record.get("run_id") not in (prior_run_id, None):
            continue
        event = record.get("event")
        if event == "exit" and exit_record is None:
            exit_record = record
        elif event == "signal":
            seen_signos.add(record.get("signo"))

    if current_boot_id:
        same_boot = (prior_boot_id == current_boot_id)
    else:
        same_boot = None

    verdict = None
    evidence = None
    if exit_record is None:
        if same_boot is False:
            verdict = "reboot"
            evidence = ("run %s has a birth record and no exit record; boot_id %r is not "
                        "the current boot_id %r" % (prior_run_id, prior_boot_id, current_boot_id))
        elif same_boot is True:
            verdict = "hard_kill_no_signal"
            evidence = ("run %s has a birth record and no exit record on the same boot_id "
                        "%r: the witness never observed the death" % (prior_run_id, prior_boot_id))
        else:
            verdict = "unknown"
            evidence = ("run %s has a birth record and no exit record; current boot_id "
                        "unavailable" % (prior_run_id,))
    else:
        killed_by = exit_record.get("signaled_by")
        if killed_by is not None and killed_by not in seen_signos:
            verdict = "hard_kill_no_signal"
            evidence = ("run %s exit record says killed by %s (signo %r) but no signal record "
                        "with that signo was witnessed: nobody we could see asked it to die"
                        % (prior_run_id, signal_name(killed_by), killed_by))
    if verdict is None:
        return None
    return {
        "event": "verdict",
        "ts": utc_now_iso(),
        "run_id": run_id,
        "prior_run_id": prior_run_id,
        "verdict": verdict,
        "evidence": evidence,
    }


# --------------------------------------------------------------------------- #
# supervisor
# --------------------------------------------------------------------------- #
def _forward(pid, signo):
    try:
        os.kill(pid, signo)
        return True
    except OSError:
        return False


def _record_forward(state, pid, signo):
    """Forward `signo` to `pid` and remember WHEN, so the exit record can tell a
    death we asked for from a death somebody outside this witness caused."""
    if not _forward(pid, signo):
        return False
    with state["lock"]:
        state.setdefault("forwarded", []).append((int(signo), time.monotonic()))
    return True


def _forwarded_within(forwarded, signos, exited_at, grace_s):
    """Did we forward one of `signos` at most `grace_s` before `exited_at`?"""
    if exited_at is None:
        return False
    for entry in forwarded or ():
        try:
            signo, forwarded_at = entry
            signo = int(signo)
            forwarded_at = float(forwarded_at)
        except (TypeError, ValueError):
            continue
        if signo not in signos:
            continue
        delta = float(exited_at) - forwarded_at
        if 0.0 <= delta <= float(grace_s):
            return True
    return False


def classify_shutdown(status, forwarded, exited_at, grace_s=None):
    """Name the shutdown; return (shutdown, dirty).

    `forwarded` is the [(signo, monotonic)] list this witness delivered, so the
    classification rests on provenance rather than on the bare wait status.
    """
    grace = GRACE_WINDOW_S if grace_s is None else float(grace_s)
    forwarded = list(forwarded or ())
    term_like = (int(signal.SIGTERM), int(signal.SIGINT))
    if os.WIFSIGNALED(status):
        signo = int(os.WTERMSIG(status))
        if signo == int(signal.SIGKILL):
            # nobody can forward SIGKILL: the witness can never be the cause
            return ("killed", True)
        if signo in term_like:
            if _forwarded_within(forwarded, term_like, exited_at, grace):
                return ("orderly", False)
            return ("unattributed_signal", True)
        if signo == int(signal.SIGHUP):
            if _forwarded_within(forwarded, (int(signal.SIGHUP),), exited_at, grace):
                return ("unhandled_signal", True)
            return ("unattributed_signal", True)
        return ("unattributed_signal", True)
    if os.WIFEXITED(status):
        code = int(os.WEXITSTATUS(status))
        if code == 0:
            if _forwarded_within(forwarded, term_like, exited_at, grace):
                return ("orderly", False)
            return ("clean_exit", False)
        return ("crash", True)
    return ("unknown", True)


def _fatal_waiter_failure(detail):
    """A stopped waiter is fatal, never a silent ``return``.

    SIGTERM/SIGINT/SIGHUP are blocked in every thread here and the waiter is the
    only consumer of that mask: a waiter that stops leaves the block installed
    with nobody to read it, so this process can neither forward a signal to the
    child nor be stopped by one, while the ledger goes on looking healthy.  A
    thread cannot return an exit status, so this exits the way the pre-fork
    refusal does -- loud, on stderr, with EX_CONFIG.  PDEATHSIG then takes the
    child down with us instead of leaving an unsupervised backend behind.
    """
    sys.stderr.write(
        "tm_witness: fatal: the sigwaitinfo waiter stopped (%s).  A signal "
        "could only be parked, never attributed and never forwarded, so this "
        "witness refuses to keep supervising; exit %d (EX_CONFIG).\n"
        % (detail, EX_UNSUPPORTED)
    )
    sys.stderr.flush()
    os._exit(EX_UNSUPPORTED)


def _signal_waiter(ledger, run_id, mask, state):
    """Blocking sigwaitinfo loop.  Record first, forward second.

    Every escape is fatal (see :func:`_fatal_waiter_failure`) -- including the
    capability itself being absent, which the pre-fork refusal is supposed to
    have caught already.
    """
    try:
        _signal_waiter_loop(ledger, run_id, mask, state)
    except BaseException as exc:  # noqa: BLE001 -- deliberate, see above
        _fatal_waiter_failure(repr(exc))


def _signal_waiter_loop(ledger, run_id, mask, state):
    while True:
        try:
            info = signal.sigwaitinfo(mask)
        except InterruptedError:
            continue
        except (OSError, ValueError) as exc:
            _fatal_waiter_failure(repr(exc))
        # struct_siginfo IS a tuple subclass (fields: si_signo, si_code, si_errno,
        # si_pid, si_uid, ...), so attributes must be preferred over indexing.
        si_signo = getattr(info, "si_signo", None)
        if si_signo is None and isinstance(info, tuple) and info:
            si_signo = info[0]
        signo = int(si_signo or 0)
        si_pid = int(getattr(info, "si_pid", 0) or 0)
        si_uid = int(getattr(info, "si_uid", 0) or 0)
        if not signo:
            continue
        sender = resolve_sender(si_pid)
        record = {
            "event": "signal",
            "ts": utc_now_iso(),
            "run_id": run_id,
            "signo": signo,
            "signame": signal_name(signo),
            "si_pid": si_pid,
            "si_uid": si_uid,
            "sender_comm": sender["sender_comm"],
            "sender_cmdline": sender["sender_cmdline"],
            "origin": sender["origin"],
        }
        try:
            ledger.append(record)
        except OSError as exc:  # never let a write failure hang the child
            sys.stderr.write("tm_witness: ledger append failed: %s\n" % (exc,))
        with state["lock"]:
            child = state["child_pid"]
            if child is None:
                state["pending"].append(signo)
        if child is not None:
            _record_forward(state, child, signo)


PR_SET_PDEATHSIG = 1


def _exec_child(command, executable, parent_pid, ledger_fd):
    """Body of the forked child: async-signal-safe work only, then execv.

    Never returns.
    """
    try:
        # 1. clear the blocking mask inherited from the supervisor (after fork
        #    the child has a single thread, so this covers the whole process)
        signal.pthread_sigmask(signal.SIG_SETMASK, [])
        # 2. die with the supervisor, even when the supervisor is SIGKILLed
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.prctl(PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0) != 0:
            os.write(2, b"tm_witness: PR_SET_PDEATHSIG failed; refusing to run unguarded\n")
            os._exit(127)
        # 3. close the classic PDEATHSIG race: the supervisor may have died
        #    between fork() and the prctl() above, in which case no signal is
        #    coming for us
        if os.getppid() != parent_pid:
            os._exit(127)
        # 4. do not leak the ledger fd into the supervised process
        if ledger_fd is not None:
            try:
                os.close(ledger_fd)
            except OSError:
                pass
        os.execv(executable, command)
    except BaseException:
        pass
    os._exit(127)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="tm_witness.py",
        description="Append-only witness: record birth/death/signal provenance of a child.",
    )
    parser.add_argument("--ledger", default=DEFAULT_LEDGER,
                        help="ledger path (default: %s)" % DEFAULT_LEDGER)
    parser.add_argument("--rotate-bytes", type=int, default=ROTATE_BYTES_DEFAULT,
                        help="rotate when the ledger exceeds this size (default 8 MiB)")
    parser.add_argument("--keep-archives", type=int, default=KEEP_ARCHIVES_DEFAULT,
                        help="number of rotated archives to keep (default 5)")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="-- <command> [args...]")
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("no command given; usage: tm_witness.py --ledger PATH -- <command> [args...]")

    # Refuse BEFORE the fork and BEFORE the ledger exists: a host that cannot
    # attribute a signal must leave no half-written run behind for the next boot
    # to read as evidence.
    unsupported = unsupported_reason()
    if unsupported is not None:
        sys.stderr.write(
            "tm_witness: refusing to supervise: %s.  Sender provenance is "
            "impossible without it, so a death this witness caused could not be "
            "told apart from one it merely observed.  Nothing was forked and no "
            "ledger was opened.  Exit %d (EX_CONFIG).\n"
            % (unsupported, EX_UNSUPPORTED)
        )
        sys.stderr.flush()
        return EX_UNSUPPORTED

    ledger = Ledger(args.ledger, args.rotate_bytes, args.keep_archives)
    run_id = new_run_id()
    boot_id = read_boot_id()

    verdict = evaluate_previous_run(load_records(ledger.path), boot_id, run_id)
    if verdict is not None:
        ledger.append(verdict)

    # 1. block the signals in the MAIN thread FIRST, so that the forked child
    #    inherits a blocked mask and the waiter thread has something to wait on.
    mask = {int(signo) for signo in SIGNAL_SET}
    signal.pthread_sigmask(signal.SIG_BLOCK, mask)
    state = {"lock": threading.Lock(), "child_pid": None, "pending": [], "forwarded": []}

    executable = command[0]
    if os.sep not in executable and not os.path.exists(executable):
        executable = shutil.which(command[0]) or command[0]

    started = time.monotonic()
    parent_pid = os.getpid()
    try:
        # 2. fork + exec (not posix_spawn) so the child can arm PR_SET_PDEATHSIG.
        #    fork() happens while the process is still SINGLE-THREADED: starting
        #    the waiter thread before fork() makes Python 3.12 emit a
        #    DeprecationWarning and leaves the child with a forked copy of a
        #    half-initialised threading state.
        child_pid = os.fork()
    except OSError as exc:
        sys.stderr.write("tm_witness: fork failed for %r: %s\n" % (command, exc))
        ledger.close()
        return 127
    if child_pid == 0:
        # child: clears the inherited sigmask, arms PDEATHSIG, then execv()s
        _exec_child(command, executable, parent_pid, ledger.fd)
        os._exit(127)  # not reached

    def _safe(fn):
        try:
            return int(fn())
        except OSError:
            return None

    ledger.append({
        "event": "birth",
        "ts": utc_now_iso(),
        "run_id": run_id,
        "boot_id": boot_id,
        "pid": int(child_pid),
        "ppid": os.getpid(),
        "sid": _safe(lambda: os.getsid(child_pid)),
        "pgid": _safe(lambda: os.getpgid(child_pid)),
        "argv": list(command),
        "cwd": os.getcwd(),
        "git_head": read_git_head(),
        "python": sys.version.split()[0],
        "uptime_s": read_uptime_s(),
    })

    # 3. publish the child pid, THEN start the waiter: any signal that arrived
    #    while we were still single-threaded is still pending in the kernel and
    #    sigwaitinfo() picks it up, so it is forwarded instead of queued.
    with state["lock"]:
        state["child_pid"] = child_pid
        pending = state["pending"]
        state["pending"] = []
    waiter = threading.Thread(
        target=_signal_waiter, args=(ledger, run_id, mask, state),
        name="tm-witness-sigwait", daemon=True,
    )
    # Belt and braces: the pre-fork refusal already covers a host without the
    # capability, so this only fires if it vanished mid-run.  Neither starting a
    # waiter that cannot attribute a sender nor carrying on without one is
    # acceptable.
    if not hasattr(signal, "sigwaitinfo"):
        _fatal_waiter_failure(
            "signal.sigwaitinfo disappeared after the pre-fork capability check"
        )
    try:
        waiter.start()
    except BaseException as exc:  # noqa: BLE001 -- a waiter that will not start is fatal
        _fatal_waiter_failure(repr(exc))
    for signo in pending:
        _record_forward(state, child_pid, signo)

    _pid, status = os.waitpid(child_pid, 0)
    exited_at = time.monotonic()
    duration = exited_at - started

    if os.WIFEXITED(status):
        returncode = os.WEXITSTATUS(status)
        signaled_by = None
    elif os.WIFSIGNALED(status):
        signaled_by = int(os.WTERMSIG(status))
        returncode = -signaled_by
    else:
        returncode = None
        signaled_by = None

    shutdown, dirty_shutdown = classify_shutdown(
        status, list(state.get("forwarded") or ()), exited_at,
    )
    ledger.append({
        "event": "exit",
        "ts": utc_now_iso(),
        "run_id": run_id,
        "returncode": returncode,
        "signaled_by": signaled_by,
        "duration_s": round(duration, 3),
        "shutdown": shutdown,
        "dirty_shutdown": dirty_shutdown,
    })
    # Let the kernel finish tearing the child down, then report who is STILL
    # alive: a process that has exited but not been reaped is not a leftover.
    time.sleep(SETTLE_WINDOW_S)
    live_children = enumerate_children()
    reap_exited_children()
    ledger.append({
        "event": "snapshot",
        "ts": utc_now_iso(),
        "run_id": run_id,
        "returncode": returncode,
        "signaled_by": signaled_by,
        "duration_s": round(duration, 3),
        "live_children": live_children,
        "backend_startup_log": newest_startup_log(),
    })
    ledger.close()

    if returncode is None:
        return 1
    if returncode < 0:
        return 128 - returncode
    return returncode


if __name__ == "__main__":
    sys.exit(main())
