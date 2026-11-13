"""C2 — cross-workspace confinement at the process boundary. NOT RUNNABLE YET.

Two tests were specified for this file:

  * ``test_worker_in_workspace_a_cannot_read_workspace_b_directory``
        a worker in workspace A tries ``os.listdir(<workspace B>)`` through the
        child runtime and must fail closed;
  * ``test_worker_in_workspace_a_cannot_git_workspace_b``
        the same worker tries ``git -C <workspace B> ...`` through the child
        runtime and must fail closed.

Neither can be written against the engine as it stands, which is the finding.
The brief for this work says the child must attempt the operation "through
whatever tool verb the child runtime actually exposes ... do not assume", and
that if no such verb exists the correct response is to stop and report rather
than invent one. There is no such verb.

What ``tools/workspace/worker_runtime_main.py`` actually accepts is exactly six
supervisor->runtime frame kinds: ``dispatch_query``, ``cancel``, ``control``,
``shutdown``, ``config_update``, ``ping`` (plus the runtime->supervisor kinds
``hello``, ``query_accepted``, ``reply``, ``event``, ``heartbeat``,
``worker_state``, ``cancel_ack``, ``control_ack``, ``config_ack``, ``fatal``,
``bye``, ``pong`` — eighteen in total). None of them is a tool, bash, git, read
or list verb: the process boundary carries *queries*, not tool invocations.

Tool execution lives strictly one layer down, inside
``WorkerThread._run_tool_loop``, where the agent decides to call a tool and the
tool implementation runs in-process. Reaching it from a test therefore requires
a real agent (a live model provider) *and* Docker, because the tools that could
perform ``os.listdir`` / ``git -C`` are the container-backed ones. That is not a
test of the process boundary, and it is not hermetic.

There is a second, independent gap. Even if a tool verb existed, there is no
vocabulary to express the expected refusal: neither
``worker_runtime_main.py`` nor ``worker_runtime_protocol.py`` carries any
``confinement`` / ``error_kind`` field or constant. A confinement test needs an
outcome to assert on — a structured refusal code, not a free-text error — and
none exists.

So this module skips, explicitly and with its reason visible in the run output.
It is deliberately not a vacuous pass: no test body is defined that could go
green without proving anything. The file exists so that a two-file pytest
invocation naming it does not abort with exit code 4 before the C1 tests in
``tests/test_worker_runtime_process.py`` have run.

Delete this skip and write both tests the moment the boundary grows a tool (or
list/git) verb carrying a structured refusal.
"""

from __future__ import annotations

import pytest

pytest.skip(
    "C2 not writable: the worker/runtime process boundary exposes no tool, bash, "
    "git, read or list verb -- its 18 frame kinds carry queries, not tool calls "
    "(dispatch_query, cancel, control, shutdown, config_update, ping, hello, "
    "query_accepted, reply, event, heartbeat, worker_state, cancel_ack, "
    "control_ack, config_ack, fatal, bye, pong). Tool execution happens only "
    "inside WorkerThread._run_tool_loop and needs a real agent plus Docker. "
    "There is also no confinement/error_kind vocabulary to assert a refusal on. "
    "Neither test_worker_in_workspace_a_cannot_read_workspace_b_directory nor "
    "test_worker_in_workspace_a_cannot_git_workspace_b can be written without "
    "inventing a verb, which the brief forbids. This is the C2 finding.",
    allow_module_level=True,
)
