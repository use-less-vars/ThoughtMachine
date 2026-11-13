#!/usr/bin/env python3
"""scripts/prove_worker_pid.py -- where does a worker's code actually run?

What this proves
----------------
A worker is only isolated if its code executes somewhere other than the
interpreter that asked.  That is a question about PIDs, and it is answerable
without trusting documentation: ask the worker to stamp its own
``os.getpid()`` on the reply it sends back, and compare it with
``os.getpid()`` here.

Three PIDs are printed, because the runtime has two modes and only one of them
is isolated:

1. the **parent** -- the interpreter running this script;
2. the **worker body** with an explicit ``WORKER_RUNTIME_MODE=thread`` (the
   opt-in in-process mode; the shipped default is ``process``): the same PID,
   because in this mode the agent really does run as a *thread* in this
   process.  Printed as a measured fact, not as an accusation;
3. the **worker body** under ``WORKER_RUNTIME_MODE=process``: a different OS
   PID, corroborated three ways -- the pid the child puts in its ``hello``
   frame, the pid ``multiprocessing`` gave the child, and the pid the child
   stamps on the query reply it sends back.

The reply is the load-bearing half: a hello frame and a ``Process.pid`` are
both *claims about* the child made by the machinery, while the reply comes from
the code path that actually runs the worker body.  If the worker cannot be
asked for that PID the script fails loudly (exit ``2``) -- "I could not get the
PID" is never a pass.

Exit codes
----------
``0``  the worker body was measured in a different OS process (proof).
``1``  measured, and the worker body shares this process (no isolation).
``2``  the worker could not be asked for its PID (hard failure) -- or the PIDs
       it reports contradict each other, which means a claim is lying.

What this does NOT prove
------------------------
Process isolation is not container isolation.  The ``DockerCodeRunner``
backend (``tools/docker_code_runner.py``) never shells out to a ``docker``
CLI: it drives Docker through the Python ``docker`` SDK against the daemon
socket, so a ``docker`` binary on ``PATH`` is neither required by it nor
evidence about it.  What the container layer does need is a reachable
daemon, the ``container`` permission grain and an executor image built from
the vault-managed workspace ``Dockerfile``; this script exercises none of
that and does not pretend otherwise.  The PID namespace of the child is
printed so the claim stays checkable: two processes can share a PID
namespace and still be separate processes.

Usage
-----
    python scripts/prove_worker_pid.py
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.workspace.worker_runtime import (  # noqa: E402 - path fixed above
    MODE_PROCESS,
    MODE_THREAD,
    WORKER_RUNTIME_MODE_ENV,
    WorkerRuntimeHandle,
    attach_thread_frame_sink,
    resolve_runtime_mode,
)

WORKER_NAME = "prove-pid-w"

#: Stamped on the reply twice (prefix and suffix) so a reply that never left
#: this interpreter cannot be mistaken for one that did.
SENTINEL = "prove-worker-pid"

#: How long the probe waits for the worker's answer.
PROOF_TIMEOUT = 60.0

_REPLY_PREFIX = f"{SENTINEL}:pid="
_FIELD_RE = re.compile(r"^([a-z_]+)=(.*)$")

#: Where the child-side factory lives.  A spawned child re-executes this module
#: (as ``__mp_main__``) and resolves ``module:attr`` with
#: ``importlib.import_module``, so the reference has to be the name this module
#: is actually importable under -- ``__main__`` when run as a script, its dotted
#: name when a test imports it.
_FACTORY_REF = (
    f"{'__main__' if __name__ == '__main__' else __name__}:build_child_worker"
)


class WorkerPidUnavailable(RuntimeError):
    """The worker could not be asked for the PID its code runs under.

    Raised -- never swallowed, never turned into a skip -- because "I do not
    know where the worker ran" and "the worker ran elsewhere" are different
    statements and only one of them is a proof.
    """


# ─────────────────────────────────────────────────────────────────────────────
# PID plumbing: measuring, and parsing what the worker says
# ─────────────────────────────────────────────────────────────────────────────


def pid_namespace(pid: Optional[int] = None) -> str:
    """The PID namespace *pid* (or this process) lives in, or ``"unknown"``.

    Printed so the isolation claim stays honest: a distinct PID proves a
    distinct *process*, not a distinct namespace.
    """
    target = "self" if pid is None else str(int(pid))
    try:
        return os.readlink(f"/proc/{target}/ns/pid")
    except OSError:
        return "unknown"


def namespace_token() -> str:
    """The PID namespace inode, colon-free so the envelope stays splittable.

    ``/proc/<pid>/ns/pid`` reads ``pid:[4026531836]``; the brackets and the
    ``pid:`` prefix are noise here and the colon would break the field parser.
    """
    return pid_namespace().rsplit(":", 1)[-1].strip("[]")


def exec_backend_pid() -> Optional[int]:
    """The PID of a subprocess started the way an exec backend starts work.

    Deliberately a plain ``subprocess`` rather than a claimed backend
    invocation: the real ``host_bash`` / ``docker_code_runner`` backends are
    gated (policy, permission grain, and -- for the latter -- a reachable
    Docker daemon and a built executor image, not a ``docker`` CLI) and this
    script must not pretend it drove one it did not.
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-c", "import os; print(os.getpid())"],
            capture_output=True,
            text=True,
            timeout=PROOF_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (completed.stdout or "").strip()
    return int(text) if text.isdigit() else None


def _reply(query: str, *, pid: int, tid: int, exec_pid: Optional[int]) -> str:
    """The envelope the worker stamps: ``SENTINEL:pid=<pid>:<fields>:<query>``."""
    return (
        f"{_REPLY_PREFIX}{pid}"
        f":tid={tid}"
        f":ppid={os.getppid()}"
        f":ns={namespace_token()}"
        f":exec_pid={exec_pid}"
        f":{query}"
    )


def parse_pid_from_reply(reply: str) -> int:
    """The PID the *worker* says it ran under -- the only value the verdict uses.

    Strict on purpose.  A reply that is not the envelope this script asked for
    is not evidence of anything, so it raises rather than defaulting.
    """
    if not isinstance(reply, str) or not reply.startswith(_REPLY_PREFIX):
        raise WorkerPidUnavailable(
            "the worker's reply carries no PID: expected a reply starting with "
            f"{_REPLY_PREFIX!r}, got {reply!r}"
        )
    head = reply[len(_REPLY_PREFIX) :].split(":", 1)[0]
    if not head.isdigit():
        raise WorkerPidUnavailable(
            f"the worker's reply PID is not a decimal number: {reply!r}"
        )
    pid = int(head)
    if pid <= 0:
        raise WorkerPidUnavailable(
            f"the worker's reply PID {pid} is not a real process: {reply!r}"
        )
    return pid


def reply_fields(reply: str) -> Dict[str, str]:
    """The informational ``key=value`` fields the worker stamped (never the verdict)."""
    fields: Dict[str, str] = {}
    if not isinstance(reply, str) or not reply.startswith(_REPLY_PREFIX):
        return fields
    tokens = reply[len(_REPLY_PREFIX) :].split(":")
    fields["pid"] = tokens[0]
    for token in tokens[1:]:
        match = _FIELD_RE.match(token)
        if not match:
            break
        fields[match.group(1)] = match.group(2)
    return fields


def verdict_for(parent_pid: int, worker_pid: int) -> Tuple[bool, str]:
    """``(proven, VERDICT line)`` -- the whole equality/inequality decision.

    Equality is not a proof failure to be hidden; it is the honest answer for
    thread mode, where the worker body IS a thread in this process.
    """
    if int(worker_pid) == int(parent_pid):
        return False, (
            f"worker body PID {worker_pid} == parent PID {parent_pid} -- the "
            "worker's code ran in THIS interpreter (thread mode): no process "
            "isolation"
        )
    return True, (
        f"worker body PID {worker_pid} != parent PID {parent_pid} -- the "
        "worker's code ran in a separate OS process"
    )


# ─────────────────────────────────────────────────────────────────────────────
# The child side: a factory resolved by ``module:attr`` INSIDE the child
# ─────────────────────────────────────────────────────────────────────────────


def build_child_worker(**kwargs: Any) -> Any:
    """Build the stand-in worker *inside* the spawned child process.

    Resolved from the spec's ``worker_factory`` string, so the real
    ``worker_runtime_main`` builds this object in the child -- nothing is
    constructed on this side of the boundary.  The constructor arguments are
    accepted and ignored: the only thing to establish is *where* the answer
    comes from.  The ``(query_id, query, reply_q)`` triple is the one every
    ``WorkerThread`` is fed, and the reply stamps this process's PID.
    """

    class _ChildWorker(threading.Thread):
        def __init__(self) -> None:
            super().__init__(name="prove-pid-child", daemon=True)
            self._input_queue: "queue.Queue[object]" = queue.Queue()
            self._stop = threading.Event()
            self._turn_cancel = threading.Event()
            self._frame_sink = None
            self.status = "ready"
            self.current_task = None
            self.error = None

        def run(self) -> None:
            while True:
                try:
                    item = self._input_queue.get(timeout=0.02)
                except queue.Empty:
                    continue
                if item is None:
                    return
                query_id, query, reply_q = item
                reply_q.put(
                    json.dumps(
                        {
                            "status": "ok",
                            "response": _reply(
                                query,
                                pid=os.getpid(),
                                tid=threading.get_ident(),
                                exec_pid=exec_backend_pid(),
                            ),
                            "query_id": query_id,
                        }
                    )
                )

        # The control surface ``worker_runtime_main`` may reach for on teardown.
        def stop(self) -> None:
            self.status = "detached"

        def detach(self) -> None:
            self.status = "detached"

        def kill(self) -> None:
            self.status = "killed"
            self._stop.set()

    return _ChildWorker()


# ─────────────────────────────────────────────────────────────────────────────
# The supervisor side: one duck-typed worker, one observation per mode
# ─────────────────────────────────────────────────────────────────────────────


class _ProbeWorker:
    """Duck-typed ``WorkerThread``: the identity ``spec_from_worker`` reads.

    In thread mode ``send_query`` *is* the worker body call site, so the PID it
    stamps is this interpreter's -- that is the point of measuring it.  In
    process mode this object never runs anything: the child rebuilds a worker
    from the spec via :data:`_FACTORY_REF`.
    """

    def __init__(self, *, session_id: str, workspace_dir: str) -> None:
        self.worker_name = WORKER_NAME
        self.instance_id = 1
        self.definition: dict = {}
        self.agent_config = {"provider": "openai", "model": "gpt-4"}
        self.workspace_dir = workspace_dir
        self.session_id = session_id
        self._runtime_worker_factory = _FACTORY_REF

    def send_query(self, query: str, timeout: float = 120.0) -> str:
        return _reply(
            query, pid=os.getpid(), tid=threading.get_ident(), exec_pid=None
        )

    def stop(self) -> None:
        return None

    def detach(self) -> None:
        return None


@dataclass
class Observation:
    """One measurement of where a worker body ran."""

    mode: str
    worker_pid: int
    detail: Dict[str, Any] = field(default_factory=dict)


def _dispatch_query(handle: Any, query: str, timeout: float) -> str:
    """One seam, so a test can ask "what if the reply carried no PID?"."""
    return handle.dispatch_query(query, timeout=timeout)


@contextmanager
def _flag_unset() -> Iterator[None]:
    """Run with ``WORKER_RUNTIME_MODE`` absent, then restore it as it was found.

    The shipped default is only observable with the flag out of the way, so the
    *claim* (:func:`_resolve_default_mode`) and the *measurement*
    (:func:`observe_default_mode`) both go through this window instead of each
    inventing its own restore.  A variable that was absent must come back
    absent: writing ``''`` (or ``None``) back is a different environment, and it
    would silently change what the next spawn in this process resolves.
    """

    previous: Optional[str] = os.environ.pop(WORKER_RUNTIME_MODE_ENV, None)
    try:
        yield
    finally:
        if previous is not None:
            os.environ[WORKER_RUNTIME_MODE_ENV] = previous


def _env_flag_state() -> Tuple[bool, Optional[str]]:
    """Whether ``WORKER_RUNTIME_MODE`` is set here, and to what value.

    Printed next to the default because "what the default resolves to" and "what
    this shell asked for" are two different claims and only the first one is
    about the shipped default.
    """

    if WORKER_RUNTIME_MODE_ENV in os.environ:
        return True, os.environ[WORKER_RUNTIME_MODE_ENV]
    return False, None


def _resolve_default_mode() -> str:
    """The mode a spawn resolves while ``WORKER_RUNTIME_MODE`` is absent.

    ``resolve_runtime_mode()`` (``tools/workspace/worker_runtime.py``) is the
    authority: this is the same function the production spawn site
    (``worker_thread._action_spawn`` -> ``attach_thread_frame_sink(thread)``
    with no ``mode=``) reaches, asked the same question, with the flag removed
    only for the length of the call.  Deliberately not the ``MODE_THREAD``
    constant: the constant is a claim *about* the default, and the line this
    replaces kept printing ``'thread'`` no matter what the code resolved.
    """

    with _flag_unset():
        return resolve_runtime_mode()


def observe_thread_mode(workspace_dir: str) -> Observation:
    """Measure the worker body in the explicit in-process ``thread`` mode."""
    worker = _ProbeWorker(session_id="prove-pid-thread", workspace_dir=workspace_dir)
    handle = WorkerRuntimeHandle.spawn(
        worker, environ={WORKER_RUNTIME_MODE_ENV: MODE_THREAD}
    )
    try:
        if handle.mode != MODE_THREAD:
            raise WorkerPidUnavailable(
                f"asked for {MODE_THREAD!r} and got {handle.mode!r}: the mode flag "
                "is not in force, so nothing measured here means anything"
            )
        reply = _dispatch_query(handle, SENTINEL, PROOF_TIMEOUT)
        worker_pid = parse_pid_from_reply(reply)
        fields = reply_fields(reply)
        return Observation(
            mode=MODE_THREAD,
            worker_pid=worker_pid,
            detail={"thread_ident": fields.get("tid"), "in_process": True},
        )
    finally:
        handle.close(grace_ms=2000)


def observe_process_mode(workspace_dir: str) -> Observation:
    """Measure the worker body under ``WORKER_RUNTIME_MODE=process``.

    Corroborates the PID three ways and *fails* if they disagree: a hello frame
    or a ``Process.pid`` that contradicts the reply means one of the three
    claims is lying, and a lying claim is not a proof.
    """
    worker = _ProbeWorker(session_id="prove-pid-process", workspace_dir=workspace_dir)
    handle = WorkerRuntimeHandle.spawn(
        worker, environ={WORKER_RUNTIME_MODE_ENV: MODE_PROCESS}
    )
    try:
        if handle.mode != MODE_PROCESS:
            raise WorkerPidUnavailable(
                f"asked for {MODE_PROCESS!r} and got {handle.mode!r}: the flag is "
                "accepted and then ignored, so process isolation cannot be shown"
            )
        lazy = handle._process_runtime is None
        reply = _dispatch_query(handle, SENTINEL, PROOF_TIMEOUT)
        worker_pid = parse_pid_from_reply(reply)
        fields = reply_fields(reply)

        runtime = handle._process_runtime
        if runtime is None:
            raise WorkerPidUnavailable(
                "the worker answered but no process runtime was built: the reply "
                "did not come over the process boundary"
            )
        hello = getattr(runtime, "hello", None)
        hello_pid = getattr(hello, "pid", None)
        os_pid = getattr(runtime, "pid", None)
        if os_pid is None:
            raise WorkerPidUnavailable(
                "the process runtime exposes no child PID; the worker cannot be "
                "asked where it ran"
            )
        if worker_pid != os_pid:
            raise WorkerPidUnavailable(
                f"the worker says it ran in PID {worker_pid}, but the spawned "
                f"process is PID {os_pid}: one of those claims is false"
            )
        if hello_pid is not None and hello_pid != os_pid:
            raise WorkerPidUnavailable(
                f"the child's hello frame claims PID {hello_pid}, but the spawned "
                f"process is PID {os_pid}: the handshake is not this incarnation"
            )
        return Observation(
            mode=MODE_PROCESS,
            worker_pid=worker_pid,
            detail={
                "hello_pid": hello_pid,
                "runtime_pid": os_pid,
                "lazy_spawn": lazy,
                "thread_ident": fields.get("tid"),
                "ppid": fields.get("ppid"),
                "namespace": fields.get("ns"),
                "exec_pid": fields.get("exec_pid"),
                "in_process": worker_pid == os.getpid(),
            },
        )
    finally:
        handle.close(grace_ms=2000)


def observe_default_mode(workspace_dir: str) -> Observation:
    """Measure the worker body on the SHIPPED default -- flag left unset.

    The other two observers *ask for* a mode; this one asks for nothing and
    reports what the spawn path answers.  The handle is built by
    :func:`attach_thread_frame_sink` -- the very call
    ``worker_thread._action_spawn`` makes, with no ``mode=`` argument -- so the
    mode measured here is the one production resolves rather than one this
    script chose.  Nothing is assumed: the mode is read back off the handle
    (``handle.mode``, filled from ``resolve_runtime_mode()``) and the substrate
    is *measured* -- the PID the worker stamps on its reply, against this
    interpreter's.  Flip the default and this layer moves with it, so the
    printed claim stays true instead of staying tidy.
    """

    with _flag_unset():
        worker = _ProbeWorker(
            session_id="prove-pid-default", workspace_dir=workspace_dir
        )
        handle = attach_thread_frame_sink(worker)
        try:
            reply = _dispatch_query(handle, SENTINEL, PROOF_TIMEOUT)
            worker_pid = parse_pid_from_reply(reply)
            fields = reply_fields(reply)
            in_process = worker_pid == os.getpid()
            return Observation(
                mode=handle.mode,
                worker_pid=worker_pid,
                detail={
                    "resolved_mode": handle.mode,
                    "substrate": (
                        "this interpreter -- the worker body ran on a thread"
                        if in_process
                        else f"a separate OS process (PID {worker_pid})"
                    ),
                    "thread_ident": fields.get("tid"),
                    "in_process": in_process,
                    "process_runtime": handle._process_runtime is not None,
                },
            )
        finally:
            handle.close(grace_ms=2000)


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────


def _print_observation(label: str, observation: Observation) -> None:
    detail = observation.detail
    print(f"{label:<26}: {observation.worker_pid}")
    if observation.mode == MODE_THREAD:
        print(
            f"{'  where':<26}: this interpreter, thread ident "
            f"{detail.get('thread_ident')} (agent runs as a THREAD)"
        )
        return
    print(
        f"{'  corroboration':<26}: reply={observation.worker_pid} "
        f"hello={detail.get('hello_pid')} process={detail.get('runtime_pid')} "
        f"(lazy spawn: {detail.get('lazy_spawn')})"
    )
    print(f"{'  thread ident':<26}: {detail.get('thread_ident')}")
    print(f"{'  parent PID':<26}: {detail.get('ppid')}")
    print(f"{'  pid namespace':<26}: {detail.get('namespace')}")
    print(f"{'  code-exec PID':<26}: {detail.get('exec_pid')} (subprocess of the worker)")


def main(argv: Optional[list] = None) -> int:
    parent_pid = os.getpid()
    print("── layer 1: the caller ─────────────────────────────────────────")
    print(f"{'parent PID':<26}: {parent_pid}")
    print(f"{'interpreter':<26}: {sys.executable}")
    print(f"{'repo root':<26}: {_REPO_ROOT}")
    env_set, env_value = _env_flag_state()
    print(
        f"{'env flag':<26}: {WORKER_RUNTIME_MODE_ENV} "
        + (f"is set to {env_value!r}" if env_set else "is NOT set in this process")
    )
    print(
        f"{'default mode (unset)':<26}: {_resolve_default_mode()!r} "
        f"({WORKER_RUNTIME_MODE_ENV} removed for the call: resolve_runtime_mode() "
        "is the authority -- layer 2 measures it)"
    )

    try:
        default_obs = observe_default_mode(str(_REPO_ROOT))
        print()
        print(f"\u2500\u2500 layer 2: the shipped default ({WORKER_RUNTIME_MODE_ENV} unset) \u2500\u2500\u2500\u2500\u2500\u2500")
        _print_observation("worker body PID", default_obs)
        print(
            f"{'  resolved mode':<26}: {default_obs.detail.get('resolved_mode')!r} "
            "(from resolve_runtime_mode(), not a constant)"
        )
        print(f"{'  substrate':<26}: {default_obs.detail.get('substrate')}")

        thread_obs = observe_thread_mode(str(_REPO_ROOT))
        print()
        print(f"── layer 3: {WORKER_RUNTIME_MODE_ENV}={MODE_THREAD} ────────────────────")
        _print_observation("worker body PID", thread_obs)

        process_obs = observe_process_mode(str(_REPO_ROOT))
        print()
        print(f"── layer 4: {WORKER_RUNTIME_MODE_ENV}={MODE_PROCESS} ───────────────────")
        _print_observation("worker body PID", process_obs)
    except WorkerPidUnavailable as exc:
        print()
        print(f"FAILURE: {exc}", file=sys.stderr)
        print(
            "VERDICT: NOT PROVEN -- the worker could not be asked for the PID "
            "its code runs under (this is a hard failure, not a pass)"
        )
        return 2

    proven, verdict_line = verdict_for(parent_pid, process_obs.worker_pid)
    print()
    print("── limits of this proof ────────────────────────────────────────")
    print(
        f"{'container layer':<26}: NOT exercised here -- DockerCodeRunner "
        "(tools/docker_code_runner.py) drives Docker through the Python "
        "`docker` SDK and the daemon socket, so a `docker` binary on PATH is "
        "neither required by it nor evidence about it"
    )
    print(
        f"{'child pid namespace':<26}: "
        f"{process_obs.detail.get('namespace')} vs {namespace_token()} (this process)"
        f" -- same: {process_obs.detail.get('namespace') == namespace_token()}"
    )
    print()
    print("── verdict ─────────────────────────────────────────────────────")
    print(f"VERDICT: {verdict_line}")
    default_isolated = default_obs.worker_pid != parent_pid
    print(
        f"VERDICT: the shipped default ({WORKER_RUNTIME_MODE_ENV} unset) resolves "
        f"{default_obs.mode!r} and the worker body "
        + (
            f"runs in another OS process (PID {default_obs.worker_pid} != parent "
            f"PID {parent_pid})"
            if default_isolated
            else f"does NOT leave this interpreter -- worker body PID "
            f"{default_obs.worker_pid} == parent PID {parent_pid}; "
            f"{WORKER_RUNTIME_MODE_ENV}={MODE_PROCESS} is what moves it into its own "
            "OS process"
        )
    )
    return 0 if proven else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
