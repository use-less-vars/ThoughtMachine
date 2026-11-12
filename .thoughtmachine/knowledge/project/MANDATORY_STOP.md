# MANDATORY STOP — read on entry

Read-on-entry for every new Main engineer. Reviewed at every handoff.
Items promoted from observations → design decisions → migration strands → closed get moved to an "archive" section, not deleted.

1. Read Gen 26's handoff, the baseline doc, the current-state doc.
1a. Read `security_architecture.md` §Escape-to-host. The four boundaries are named there; the "security boundary held" sentence is the bug class at the narrative layer.
1c. Before touching host execution, read `docs/host-port.md` §1, §4, §9. §1 is the claim and its two qualifications (including that CI enforces only the honest case). §4 says the grain is a *category name*. §9 says what the port does not cover, including the named exclusions (git's `_exec_host_raw`, container git).
1d. Read `architecture_invariants.md`. The rules that must not be re-derived: the container permission envelope, and the **four** fail-closed routes to a read-only mount (check `workspace_id` first — it is the unifier); and host access as one chokepoint checked at the point of action (the killswitch is fail-closed on `allow_host_resources`, the grain is a category name, and §9.3's two named exclusions mean git and container-git are *outside* the port's coverage — do not claim otherwise).
2. Read `operator_observations.md` — the operator's repeated points, the things that keep getting lost through migration cycles.
3. Read `working_agreements.md`.
4. Read the design decisions (1 and 2 when 2 lands).
5. Read the recon map.
6. Verify live state: dev HEAD, branch protection, open branches, open strands.
7. Report to operator: what is current, what is blocked, what is next.

## Stop rules

Not reading items. Each is a place where the obvious local fix *is* the bug. Provenance is given so
the rule can be checked rather than believed; the full narrative is `docs/ARCHITECTURE_MACHINE_MODEL.md`
§9.

- **Never add a tool that forwards to another tool class.** No "safe wrapper". The gate evaluates
the categories of the tool the *call* names; a wrapper names one tool and runs another, so its
declared categories describe nothing that happens. Tried in three steps and abandoned: `8aa17e0d`
(contain the context fields), `0c125856` (re-gate inside `execute()`), `4a4bd692` (delete
`PaginateTool` — 223 lines, 18 tests retired with it). Class rule: `hygiene/no-tool-wrapping`.
If your fix is a wrapper, stop.
- **Never re-declare authority below a gate.** Gate inputs come from the chain
(`docs/ARCHITECTURE_MACHINE_MODEL.md` §2), never from the caller's payload. Identity is not
context: `_CONTEXT_FIELDS` was written as a list of *paths* and omitted `workspace_id`, which is how
a containment list became an attribution defect. `_NEVER_FORWARD = ('workspace_id',)` records the
correction.
- **A structural blocklist is not a call-time gate.** `_NEVER_PAGINATE` was necessary because
several blocklisted tools declare no categories at all, so no categories gate can see them. If your
safety argument is "X is not in the list", name what enforces the list and when.
- **Never `--no-verify`; never disable, edit or "temporarily" move the host hook.** Not a style
rule. The host git path neutralizes hooks by construction (`core.hooksPath=/dev/null` plus
`--no-verify`) and *refuses* a commit when `.githooks/pre-commit` exists but cannot be enforced.
A commit timeout is not a pass: the hook ran and was killed with paths staged and HEAD unmoved.
Use container mode or fix the hook. Pinned by `tests/security/test_git_hardening.py`.
- **"Dormant" is a claim about one shell, not about the machine.** `.githooks/pre-commit` was
deleted in `85035605` because `core.hooksPath` read as unset from a bare shell while the container
path sets it explicitly. Before removing anything on the strength of a default value, establish
which of the three commit paths you are standing on.

<!-- Housekeeping note (Gen 48): the header above advertises an archive section that does not exist. Not added this pass; tracked for a later turn. -->
