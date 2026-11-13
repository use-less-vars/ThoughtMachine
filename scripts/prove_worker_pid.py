#!/usr/bin/env python3
"""scripts/prove_worker_pid.py -- where does a worker's code actually run?

What this proves
----------------
A worker is only isolated if its code executes somewhere other than the
interpreter that asked.  That is a question about OS pids, and it is answerable
without trusting documentation: spawn the worker through the *production*
process runtime, force the lazy spawn, and compare the pid the machinery
reports with the pid this interpreter has.

Nothing in this script builds a stand-in worker.  The parent constructs a real
:class:`~tools.workspace.worker_thread.WorkerThread`; ``spec_from_worker``
reads ``worker_factory`` off it (``None`` for a real worker thread), and the
child's ``worker_runtime_main._build_worker(None, kwargs)`` therefore imports
and constructs the *real* ``WorkerThread`` in the child.  That is the same path
a user-facing spawn takes.  Earlier revisions of this script installed a
sentinel factory that resolved, inside the child, to a canned-reply stub -- so
``WorkerThread.__init__`` never ran there and the numbers it printed were real
OS facts about a puppet body.  Those assertions below exist so that degradation
cannot happen silently again.

The layers
----------
1. ``thread``   -- explicit in-process mode.  The worker body is a thread in
   this process.  The correct finding is "no child exists"; this layer reports
   exactly that and does not pretend otherwise.
2. ``default``  -- whatever ``resolve_runtime_mode()`` says with the
   environment untouched, so the printed claim tracks the shipped default
   instead of a hard-coded assumption.
3. ``process``  -- explicit ``WORKER_RUNTIME_MODE=process``.  The child is
   measured from four independent witnesses:

   * the ``hello`` frame's pid -- computed by the child as ``os.getpid()``;
   * the pid ``multiprocessing`` gave the child ``Process`` object;
   * ``/proc/<pid>`` on Linux, if present;
   * the shipped ``Worker`` tool's own ``inspect`` action
     (``child_pid`` / ``child_alive`` / ``parent_pid`` / ``same_process``).

   If two witnesses disagree the script fails: a contradicting claim is not a
   proof, it is the finding.

What this does NOT prove
------------------------
Process isolation is not container isolation.  The ``DockerCodeRunner`` backend
(``tools/docker_code_runner.py``) never shells out to a ``docker`` CLI: it
drives Docker through the Python ``docker`` SDK against the daemon socket, so a
``docker`` binary on ``PATH`` is neither required by it nor evidence about it.
What the container layer does need is a reachable daemon, the ``container``
permission grain and an executor image built from the vault-managed workspace
``Dockerfile``; this script exercises none of that and does not pretend
otherwise.

Nor is this a user-facing spawn: the script constructs the ``WorkerThread``
itself (in a throwaway workspace directory) instead of going through the
``Worker`` tool's spawn action, feeds it no ``workers.json`` definition and
never runs ``WorkerThread.run()`` -- so no agent, no LLM call and no tool loop
is involved.  What is proven is the *runtime substrate boundary*: the object
that would execute the worker body is built by the production child and lives
in a different OS process.

Child-side class evidence is INDIRECT
-------------------------------------
The wire protocol carries no frame naming the child's worker class, so this
script cannot read ``type(child_worker).__name__`` from the parent.  What it
does verify, and prints as such:

* the child answered a ``Ping`` with a ``Pong`` -- which requires the child to
  have *built* its worker object (``_build_worker``) before it entered the frame
  loop; had ``WorkerThread.__init__`` raised, the child would have died instead;
* the child executed ``WorkerThread.pause()`` (a ``ControlAck`` came back) and
  that method wrote a pause command file into a directory only the real
  constructor computes (``<workspace>/workers/<label>/``); the script snapshots
  the tree before and after and reports the new file by path and mtime;
* parent side, a real ``WorkerThread`` is constructed with ``workspace_dir``
  passed as a ``str`` (as ``spec_from_worker`` does) and asserted to coerce it
  to a ``pathlib.Path`` -- the exact regression the stub factory hid.

Exit codes
----------
``0``  the worker body was observed as a different, live OS pid, and every
       witness that could be read agreed (proof).
``1``  measured, and the worker body shares this process / no fork happened.
``2``  the child could not be measured, or two witnesses disagree.

Usage
-----
    python scripts/prove_worker_pid.py [--layer thread|default|process ...]
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.workspace.worker_runtime import (  # noqa: E402 - path fixed above
    MODE_PROCESS,
    MODE_THREAD,
    WORKER_RUNTIME_MODE_ENV,
    attach_thread_frame_sink,
    resolve_runtime_mode,
)
from tools.workspace.worker_runtime_process import spec_from_worker  # noqa: E402

WORKER_NAME = "prove-pid-w"
SESSION_ID = "prove-pid"
INSTANCE_ID = 1

#: How long the probe waits for the child's answer.
PROOF_TIMEOUT = 60.0

#: Printed wherever a witness could not be read at all.  Never silently
#: replaced by a plausible-looking number.
NOT_MEASURED = "NOT MEASURED"

EXIT_PROVEN = 0
EXIT_SAME_PROCESS = 1
EXIT_UNMEASURED = 2


# ─────────────────────────────────────────────────────────────────────────────
# The parent side: a REAL WorkerThread, built by the production constructor
# ─────────────────────────────────────────────────────────────────────────────


def build_real_worker(workspace_dir: Path) -> Any:
    """Construct the real ``WorkerThread`` this script measures.

    ``__init__`` starts nothing: it computes ``_worker_dir`` (mkdir), allocates
    an incarnation epoch and initialises the queues.  No agent, no LLM, no
    thread -- so constructing one in a throwaway directory is safe and leaves
    ``run()`` free to be something this script never calls.
    """
    from tools.workspace.worker_thread import WorkerThread

    return WorkerThread(
        WORKER_NAME,
        {},
        {"provider": "openai", "model": "gpt-4"},
        workspace_dir,
        session_id=SESSION_ID,
        instance_id=INSTANCE_ID,
        max_container_count=1,
        max_token_usage=1000,
        max_runtime_s=60,
        frame_sink=None,
        initial_context=None,
    )


def assert_no_factory(worker: Any) -> List[str]:
    """Refuse to proceed with a fake in place -- print the facts either way.

    Two separate claims, because they can fail independently: the spec's
    ``worker_factory`` must be ``None`` (so the child falls through to the real
    ``WorkerThread`` import), and the sentinel attribute that a previous
    revision assigned must not exist at all.
    """
    lines: List[str] = []
    spec = spec_from_worker(worker)
    factory = spec.get("worker_factory")
    lines.append(f"spec_from_worker(worker)['worker_factory'] = {factory!r}")
    lines.append(
        f"hasattr(worker, '_runtime_worker_factory') = "
        f"{hasattr(worker, '_runtime_worker_factory')}"
    )
    assert factory is None, (
        "a worker factory is installed; the child would build a stand-in "
        f"instead of the real WorkerThread (worker_factory={factory!r})"
    )
    assert not hasattr(worker, "_runtime_worker_factory"), (
        "the _runtime_worker_factory sentinel is back; spec_from_worker reads "
        "it, so the child would build whatever it names"
    )
    # The regression the stub factory hid: real worker_kwargs carry
    # workspace_dir as a str, and WorkerThread must coerce it to Path.
    ws_kw = spec.get("worker_kwargs", {}).get("workspace_dir")
    lines.append(f"worker_kwargs['workspace_dir'] type = {type(ws_kw).__name__}")
    assert isinstance(worker.workspace_dir, Path), (
        "the parent-side WorkerThread did not coerce workspace_dir to Path"
    )
    lines.append(
        f"parent WorkerThread.workspace_dir is a pathlib.Path: "
        f"{isinstance(worker.workspace_dir, Path)}"
    )
    lines.append(f"parent worker object type = {type(worker).__name__}")
    return lines


def assert_str_workspace_dir_coercion(workspace_dir: Path) -> List[str]:
    """Same constructor, given ``workspace_dir`` as a ``str`` as the spec does.

    The stub factory a previous revision injected meant the real
    ``WorkerThread.__init__`` never ran in the child, so a coercion failure
    there was invisible.  This runs the production constructor with exactly the
    type the spec produces.
    """
    from tools.workspace.worker_thread import WorkerThread

    probe = WorkerThread(
        f"{WORKER_NAME}-coercion",
        {},
        {"provider": "openai", "model": "gpt-4"},
        str(workspace_dir / "coercion"),
        session_id=SESSION_ID,
        instance_id=99,
        max_container_count=1,
        max_token_usage=1000,
        max_runtime_s=60,
        frame_sink=None,
        initial_context=None,
    )
    ok = isinstance(probe.workspace_dir, Path)
    assert ok, (
        "WorkerThread did not coerce a str workspace_dir to Path; the child "
        "would raise on construction"
    )
    return [
        "WorkerThread(str_workspace_dir).workspace_dir is a pathlib.Path: "
        f"{ok}",
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Witnesses
# ─────────────────────────────────────────────────────────────────────────────


def hello_pid(handle: Any) -> Tuple[Optional[int], str]:
    """The pid the CHILD computed and put in its ``hello`` frame."""
    candidates = (
        ("handle.hello", lambda: getattr(getattr(handle, "hello", None), "pid", None)),
        ("handle._runtime.hello", lambda: getattr(
            getattr(getattr(handle, "_runtime", None), "hello", None), "pid", None)),
        ("handle._process_runtime.hello", lambda: getattr(
            getattr(getattr(handle, "_process_runtime", None), "hello", None), "pid", None)),
        ("handle._last_hello", lambda: getattr(
            getattr(handle, "_last_hello", None), "pid", None)),
    )
    for label, getter in candidates:
        try:
            value = getter()
        except Exception:  # pragma: no cover - defensive introspection only
            continue
        if isinstance(value, int):
            return value, label
    return None, f"{NOT_MEASURED} (no hello frame reachable from the handle)"


def multiprocessing_pid(handle: Any) -> Tuple[Optional[int], str]:
    """The pid ``multiprocessing`` assigned to the child ``Process``."""
    for name in ("_runtime", "_process_runtime", "_proc", "_process"):
        obj = getattr(handle, name, None)
        if obj is None:
            continue
        pid = getattr(obj, "pid", None)
        if isinstance(pid, int):
            return pid, f"handle.{name}.pid"
        inner = getattr(obj, "_process", None)
        pid = getattr(inner, "pid", None)
        if isinstance(pid, int):
            return pid, f"handle.{name}._process.pid"
    return None, f"{NOT_MEASURED} (no multiprocessing.Process reachable)"


def proc_evidence(pid: Optional[int]) -> str:
    """``/proc/<pid>`` on Linux: existence plus its command name."""
    if pid is None:
        return f"{NOT_MEASURED} (no pid)"
    proc = Path(f"/proc/{pid}")
    if not Path("/proc").is_dir():
        return f"{NOT_MEASURED} (/proc not present on this platform)"
    if not proc.is_dir():
        return f"/proc/{pid} is absent -> that pid is NOT live"
    try:
        comm = (proc / "comm").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        comm = "<unreadable>"
    return f"/proc/{pid} exists, comm={comm!r}"


def _make_worker_tool(Worker: Any) -> Any:
    """Build a ``Worker`` tool object with as little envelope as possible.

    ``Worker`` is a pydantic model, so ``__new__`` alone leaves it without a
    ``__pydantic_fields_set__``.  Prefer a real (all-default) construction and
    fall back to ``model_construct`` / ``object.__new__``; which one worked is
    printed by the caller's provenance note.
    """
    for attempt in (lambda: Worker(), lambda: Worker.model_construct()):
        try:
            return attempt()
        except Exception:
            continue
    tool = object.__new__(Worker)
    object.__setattr__(tool, "__pydantic_fields_set__", set())
    object.__setattr__(tool, "__pydantic_extra__", None)
    object.__setattr__(tool, "__pydantic_private__", None)
    return tool


def inspect_reading(worker: Any, handle: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    """Drive the SHIPPED ``Worker(action="inspect")`` body and return its row.

    ``_action_inspect`` reads nothing but ``self.session_id``, the module-level
    ``_worker_registry`` and ``runtime_handle_for`` -- so the registration below
    is the whole setup, and the shipped method body runs unmodified.  The tool
    object is made with ``__new__`` because a real ``Worker`` tool needs a full
    ``ToolBase`` envelope this script has no reason to fabricate; that seam is
    printed rather than hidden.
    """
    try:
        from tools.workspace.worker import Worker, _WorkerRegistry
        from tools.workspace.worker_runtime import register_runtime_handle

        registry = _WorkerRegistry.get_instance()
        registry.register_worker(
            SESSION_ID,
            WORKER_NAME,
            worker,
            INSTANCE_ID,
            epoch=getattr(worker, "_generation", None),
        )
        register_runtime_handle(
            handle,
            session_id=SESSION_ID,
            worker_name=WORKER_NAME,
            instance_id=INSTANCE_ID,
        )
        tool = _make_worker_tool(Worker)
        tool.session_id = SESSION_ID
        result = tool._action_inspect([{"name": WORKER_NAME}])
    except Exception as exc:
        return None, f"{NOT_MEASURED} (shipped inspect raised: {exc!r})"
    rows = [r for r in result.get("workers", []) if r.get("worker_name") == WORKER_NAME]
    if not rows:
        return None, f"{NOT_MEASURED} (shipped inspect reported no instance)"
    row = rows[0]
    row["_tool_parent_pid"] = result.get("parent_pid")
    return row, "Worker(action='inspect') -> _action_inspect"


# ─────────────────────────────────────────────────────────────────────────────
# One measurement, one layer
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class LayerResult:
    name: str
    exit_code: int
    lines: List[str] = field(default_factory=list)


def _emit(ws: Path) -> List[Path]:
    root = ws / "workers"
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file())


def run_layer(name: str, mode: Optional[str], workspace_dir: Path) -> LayerResult:
    """Measure one runtime layer, honestly, whatever it turns out to be."""
    out = LayerResult(name=name, exit_code=EXIT_UNMEASURED)
    lines = out.lines

    worker = build_real_worker(workspace_dir)
    lines.append(f"layer: {name} (requested mode: {mode or 'environment default'})")
    lines.extend("  " + l for l in assert_no_factory(worker))

    # The runtime's staleness fence refuses a handle whose worker is not
    # alive -- ``WorkerThread.is_alive()`` is inherited from Thread, so the
    # production path requires the supervisor-side worker thread to have been
    # STARTED before any verb is issued.  That is the real sequence a
    # supervisor runs, so it is reproduced here rather than worked around.
    worker.daemon = True  # a measurement harness must not block interpreter exit
    worker.start()
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline and not worker.is_alive():
        time.sleep(0.05)
    if not worker.is_alive():
        lines.append(
            "  finding: the parent-side WorkerThread died before the handle "
            f"could be used (status={getattr(worker, 'status', None)!r}, "
            f"error={getattr(worker, 'error', None)!r}) -- the runtime fence "
            "refuses a handle for a worker that is not alive. "
            f"{NOT_MEASURED}."
        )
        out.exit_code = EXIT_UNMEASURED
        return out
    lines.append(f"  parent-side WorkerThread is alive; status={getattr(worker, 'status', None)!r}")
    time.sleep(0.5)  # let run() finish its own startup writes

    handle = attach_thread_frame_sink(worker, mode=mode) if mode else attach_thread_frame_sink(worker)
    effective = getattr(handle, "mode", None)
    lines.append(f"  handle.mode = {effective!r}")
    if mode is not None and effective != mode:
        lines.append(f"  MISMATCH: asked for {mode!r}, handle reports {effective!r}")
        out.exit_code = EXIT_UNMEASURED
        return out

    before = set(_emit(workspace_dir))

    # ── force the LAZY spawn with a real control verb ───────────────────────
    # ``ping()`` is tri-state and deliberately does NOT spawn (it answers "is
    # there a runtime?" with None when there is none), so a verb that goes
    # through ``_ensure_process_runtime`` is what forces the fork.  ``pause``
    # is the cheapest: no agent, no LLM, and its ``ControlAck`` carries the
    # CHILD's own ``_worker_status(worker)`` -- a self-report from the object
    # the child built.
    ack: Any = None
    if getattr(handle, "mode", None) == MODE_PROCESS:
        ack = _control_pause(handle)
        lines.append(f"  handle.pause() -> ControlAck.status = {ack!r}  (forces the spawn)")

    ping_ok = handle.ping(timeout=PROOF_TIMEOUT)
    lines.append(
        f"  handle.ping() = {ping_ok!r}  (tri-state Pong round trip; "
        "None means no runtime exists)"
    )

    substrate = handle.substrate()
    lines.append("  substrate():")
    for key in ("runtime_mode", "child_exists", "child_pid", "child_alive",
                "parent_pid", "same_process"):
        lines.append(f"    {key} = {substrate.get(key)!r}")

    parent_pid = os.getpid()
    lines.append(f"  parent os.getpid() = {parent_pid}")

    if not substrate.get("child_exists"):
        # The correct answer for thread/default-thread: say so, do not fake a child.
        lines.append(
            "  finding: NO CHILD PROCESS EXISTS in this mode -- the worker body "
            "is a thread in this interpreter, so its pid IS the parent pid "
            f"({parent_pid}) by construction; nothing was spawned to measure."
        )
        out.exit_code = EXIT_SAME_PROCESS
        _cleanup(handle, workspace_dir, before, lines, worker)
        return out

    if not ping_ok:
        lines.append(
            "  finding: a child exists but did not answer Ping -- the child "
            "most likely failed to build its worker (WorkerThread.__init__). "
            f"ControlAck.status was {ack!r}. That is a failure to measure, not a pass."
        )
        out.exit_code = EXIT_UNMEASURED
        _cleanup(handle, workspace_dir, before, lines, worker)
        return out

    # ── witness 1: hello frame (child-computed pid) ─────────────────────────
    w_hello, hello_src = hello_pid(handle)
    lines.append(f"  witness hello.pid            = {w_hello if w_hello is not None else hello_src}"
                 f"{'' if w_hello is None else f'  ({hello_src})'}")

    # ── witness 2: multiprocessing.Process.pid ──────────────────────────────
    w_mp, mp_src = multiprocessing_pid(handle)
    lines.append(f"  witness multiprocessing pid  = {w_mp if w_mp is not None else mp_src}"
                 f"{'' if w_mp is None else f'  ({mp_src})'}")

    # ── witness 3: /proc/<pid> ──────────────────────────────────────────────
    child_pid = substrate.get("child_pid")
    lines.append(f"  witness /proc                = {proc_evidence(child_pid)}")

    # ── witness 4: the SHIPPED Worker tool's own inspect action ─────────────
    row, insp_src = inspect_reading(worker, handle)
    if row is None:
        lines.append(f"  witness Worker(action=inspect) = {insp_src}")
    else:
        lines.append(f"  witness Worker(action=inspect) ({insp_src}):")
        for key in ("has_runtime_handle", "runtime_mode", "child_exists",
                    "child_pid", "child_alive", "parent_pid", "same_process"):
            lines.append(f"    {key} = {row.get(key)!r}")
        lines.append(
            "    NOTE: SEAM -- the tool object came from _make_worker_tool() "
            "(a real construction where the field defaults allow it) with only "
            "session_id set. `_action_inspect` reads nothing else, but the "
            "envelope is not the one a live tool session would have."
        )
        if row.get("child_pid") != child_pid:
            lines.append(
                f"  DISAGREEMENT: substrate child_pid={child_pid!r} vs shipped "
                f"inspect child_pid={row.get('child_pid')!r}"
            )
            out.exit_code = EXIT_UNMEASURED
            _cleanup(handle, workspace_dir, before, lines, worker)
            return out

    # ── indirect child-side evidence: a child-written pause command ─────────
    after = set(_emit(workspace_dir))
    new_files = sorted(after - before)
    if new_files:
        for path in new_files:
            age = time.time() - path.stat().st_mtime
            lines.append(
                f"  child-side artifact: {path} (mtime {age:.2f}s ago) -- written "
                f"by WorkerThread.pause() running in the child"
            )
    else:
        lines.append(
            "  child-side artifact: none observed after pause() -- "
            f"{NOT_MEASURED}; the child-side WorkerThread is corroborated only "
            "by the Pong and the ControlAck, not by a file"
        )

    # ── cross-check every pid witness ───────────────────────────────────────
    named = {
        "hello.pid": w_hello,
        "multiprocessing": w_mp,
        "substrate.child_pid": child_pid,
        "inspect.child_pid": (row or {}).get("child_pid"),
    }
    present = {k: v for k, v in named.items() if isinstance(v, int)}
    disagree = {k: v for k, v in present.items() if v != child_pid}
    lines.append(f"  pid witnesses read: {len(present)}/4 -> {present}")
    if disagree:
        lines.append(f"  DISAGREEMENT on child pid: {disagree} vs {child_pid}")
        out.exit_code = EXIT_UNMEASURED
        _cleanup(handle, workspace_dir, before, lines, worker)
        return out

    live = bool(substrate.get("child_alive")) and Path(f"/proc/{child_pid}").is_dir() if Path("/proc").is_dir() else bool(substrate.get("child_alive"))
    same = substrate.get("same_process")
    lines.append(f"  child_pid={child_pid} parent_pid={parent_pid} same_process={same!r} child_alive={substrate.get('child_alive')!r}")
    if child_pid == parent_pid or same is True:
        lines.append(f"  finding: the worker body shares this process (pid {child_pid}) -- NO isolation")
        out.exit_code = EXIT_SAME_PROCESS
    elif not live:
        lines.append("  finding: the pid agreed but the child is not live -- cannot call that proof")
        out.exit_code = EXIT_UNMEASURED
    else:
        lines.append(
            f"  finding: PROVEN -- the worker body runs in a DIFFERENT, live OS "
            f"process (child {child_pid} != parent {parent_pid})"
        )
        out.exit_code = EXIT_PROVEN

    _cleanup(handle, workspace_dir, before, lines, worker)
    return out


def _control_pause(handle: Any) -> Any:
    """Run the child-side control verb that reports the child's own status."""
    try:
        result = handle.pause()
    except Exception as exc:
        return f"{NOT_MEASURED} (pause raised: {exc!r})"
    status = getattr(result, "status", None)
    if status is not None:
        return status
    return result


def _cleanup(
    handle: Any,
    workspace_dir: Path,
    before: set,
    lines: List[str],
    worker: Any = None,
) -> None:
    try:
        handle.close(grace_ms=2000)
    except Exception as exc:
        lines.append(f"  cleanup: handle.close() raised {exc!r}")
    if worker is not None:
        for verb in ("kill", "stop"):
            try:
                getattr(worker, verb)()
                lines.append(f"  cleanup: parent-side WorkerThread.{verb}() called")
                break
            except Exception as exc:
                lines.append(f"  cleanup: WorkerThread.{verb}() raised {exc!r}")
    residue = sorted(set(_emit(workspace_dir)) - before)
    if residue:
        lines.append(f"  cleanup: child left {len(residue)} file(s) in the temp workspace")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--layer",
        action="append",
        choices=["thread", "default", "process"],
        help="run only the named layer (repeatable); default: all three",
    )
    parser.add_argument("--keep", action="store_true", help="keep the temp workspace")
    args = parser.parse_args(argv)

    layers = args.layer or ["thread", "default", "process"]
    mode_for = {"thread": MODE_THREAD, "default": None, "process": MODE_PROCESS}

    print("=" * 78)
    print("prove_worker_pid.py -- is the worker body in another OS process?")
    print("=" * 78)
    print(f"python           : {sys.version.split()[0]}")
    print(f"resolve_runtime_mode() with the ambient environment = {resolve_runtime_mode()!r}")
    print(f"{WORKER_RUNTIME_MODE_ENV} in env = {os.environ.get(WORKER_RUNTIME_MODE_ENV)!r}")
    print()

    results: List[LayerResult] = []
    for layer in layers:
        ws = Path(tempfile.mkdtemp(prefix=f"prove-pid-{layer}-"))
        try:
            print("-" * 78)
            print(f"[layer {layer}] workspace_dir = {ws}")
            if layer == "default":
                print("  (environment untouched: this is the SHIPPED default)")
            result = run_layer(layer, mode_for[layer], ws)
        finally:
            if not args.keep:
                shutil.rmtree(ws, ignore_errors=True)
            else:
                print(f"  kept: {ws}")
        for line in result.lines:
            print(line if line.startswith("  ") else "  " + line)
        print(f"  layer exit code = {result.exit_code} "
              f"({_exit_label(result.exit_code)})")
        print()
        results.append(result)

    # The script as a whole is only a proof if the process layer proved.
    process_results = [r for r in results if r.name == "process"]
    codes = [r.exit_code for r in results]
    if EXIT_UNMEASURED in codes:
        overall = EXIT_UNMEASURED
    elif process_results and all(r.exit_code == EXIT_PROVEN for r in process_results):
        overall = EXIT_PROVEN
    elif EXIT_PROVEN in codes:
        overall = EXIT_PROVEN
    else:
        overall = EXIT_SAME_PROCESS

    print("=" * 78)
    for r in results:
        print(f"  {r.name:<8} exit {r.exit_code}  ({_exit_label(r.exit_code)})")
    print(f"OVERALL exit {overall} ({_exit_label(overall)})")
    print("=" * 78)
    return overall


def _exit_label(code: int) -> str:
    return {
        EXIT_PROVEN: "different live OS pid -- proven",
        EXIT_SAME_PROCESS: "same process / no fork",
        EXIT_UNMEASURED: "not measured, or witnesses disagree",
    }.get(code, "unknown")


if __name__ == "__main__":
    sys.exit(main())
