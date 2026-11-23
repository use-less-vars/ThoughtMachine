"""The declared mutations the gate runs -- data, not code.

``scripts/mutation_gate.py`` imports this module, reads ``MUTATIONS``, and does
the rest.  Everything that makes a mutation *mean* something lives here, in one
place a reviewer can read: the file, the exact substring to break, the tests
that ``docs/CONTRACTS.md`` claims will die, and the contract the claim belongs
to.  The harness owns the mechanics (apply, run, restore, verdict); this file
owns the claims.

The registry is EMPTY in this commit on purpose
-----------------------------------------------
A gate that reports "0 mutations, 0 survived" is the same vacuous green the gate
exists to catch, so the harness treats an empty registry as a refusal, not a
pass: ``MUTATIONS_DECLARED=0 - a sweep of zero mutations is not a pass`` and a
non-zero exit.  The first real subject (Contract 3's ``DEFAULT_RUNTIME_MODE``
literal, the claim Known gap 1 says is already false) arrives in the next
commit; this one only has to prove the harness can tell KILLED from SURVIVED
from REFUSED before anything is pointed at production code.

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
                # Illustrative only: a node id that does not exist is REFUSED by
                # the gate, never SURVIVED.  The real entry names real tests.
                "tests/test_worker_is_a_real_process.py::test_the_default_mode",
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

MUTATIONS: list[Mutation] = []
