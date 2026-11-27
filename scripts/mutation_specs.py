"""The declared mutations the gate runs -- data, not code.

``scripts/mutation_gate.py`` imports this module, reads ``MUTATIONS``, and does
the rest.  Everything that makes a mutation *mean* something lives here, in one
place a reviewer can read: the file, the exact substring to break, the tests
that ``docs/CONTRACTS.md`` claims will die, and the contract the claim belongs
to.  The harness owns the mechanics (apply, run, restore, verdict); this file
owns the claims.

A gate that reports "0 mutations, 0 survived" is the same vacuous green the gate
exists to catch, so the harness treats an empty registry as a refusal, not a
pass: ``MUTATIONS_DECLARED=0 - a sweep of zero mutations is not a pass`` and a
non-zero exit.  The first real subject is declared below: Contract 3's
``DEFAULT_RUNTIME_MODE`` literal, the claim Known gap 1 said was unproven.

Adding a subject
----------------
Append to ``MUTATIONS``; the gate runs them in declaration order::

    MUTATIONS: list[Mutation] = [
        Mutation(
            name="contract-3-default-mode",
            path="tools/workspace/worker_runtime.py",
            old="DEFAULT_RUNTIME_MODE = MODE_PROCESS",
            new="DEFAULT_RUNTIME_MODE = MODE_THREAD",
            tests=[
                # A node id that does not exist is REFUSED by the gate, never
                # SURVIVED -- so these have to name real tests.
                "tests/test_worker_is_a_real_process.py",
            ],
            contract="Contract 3 -- the default runtime mode is the process substrate",
        ),
    ]

What the harness enforces on every entry
----------------------------------------
* ``old`` is an exact substring, never a line number, and must occur EXACTLY
  ONCE.  Zero occurrences is ``REFUSED anchor-absent``; two is ``REFUSED
  anchor-ambiguous``.  Neither is a best guess, and neither writes a file.
* ``tests`` are pytest node ids relative to the repository root.  The gate runs
  exactly those, with ``-x``.  If they are not collectable (pytest exit 2, 3, 4
  or 5) the verdict is REFUSED, never "passed".
* ``name`` is unique: it is the handle ``--only NAME`` uses and the key that
  makes one line of output traceable to one claim.
"""

from mutation_gate import Mutation

MUTATIONS: list[Mutation] = [
    Mutation(
        name="contract-3-default-mode",
        path="tools/workspace/worker_runtime.py",
        old="DEFAULT_RUNTIME_MODE = MODE_PROCESS",
        new="DEFAULT_RUNTIME_MODE = MODE_THREAD",
        tests=["tests/test_worker_is_a_real_process.py"],
        contract="Contract 3 -- the default runtime mode is the process substrate",
    ),
    Mutation(
        name="contract-8-pause-announced-as-stop",
        path="agent/core/tool_executor.py",
        old="if stop_signal == 'PAUSED':",
        new="if stop_signal is True:",
        tests=[
            "tests/test_tool_executor.py::TestPauseIsNotAnnouncedAsAStop"
            "::test_a_paused_signal_is_reported_as_paused",
        ],
        contract="Contract 8 -- a pause is not a stop",
    ),
    Mutation(
        name="contract-8-stop-softened-by-a-stale-pause-reason",
        path="agent/core/tool_executor.py",
        old="if stop_signal == 'PAUSED':",
        new=(
            "if stop_signal == 'PAUSED'"
            " or getattr(self.state, 'restriction_reason', None) == 'pause':"
        ),
        tests=[
            "tests/test_tool_executor.py::TestPauseIsNotAnnouncedAsAStop"
            "::test_a_real_stop_is_not_softened_by_a_stale_pause_reason",
        ],
        contract="Contract 8 -- a pause is not a stop",
    ),
]
