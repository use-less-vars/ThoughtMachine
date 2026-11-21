---
# Handoff -- Infra 67 -> next generation

Dated 2026-11-21. Supersedes nothing; supersedes are made by editing THIS file and re-pinning it.

## 0. How to read this file
Every claim is tagged by root. `[M]` = measured in-repo this generation, root given (path:line or a commit sha you can go and check). `[R]` = a ruling: a decision, not a measurement. `[A]` = attributed to the outgoing seat, NOT verified in-repo -- treat as a hypothesis until you measure it. If a claim here disagrees with what you measure, the measurement wins and you should correct this file.

## 1. State pin (verify before you use it)
- `dev` = a82f07f1 -- #435 squashed: "the display must not out-claim the gate". [M]
- Open: #433 @ 8db9e269 (19/20 checks), #434 @ f0cec69b (20/21 checks; Contract Gate green). [M]
- Local, unpushed: `fix/envelope-status` @ 2884b1dc (tri-state `envelope_status`), `fix/caps-display-closed` @ fee94066 (two display capability fallbacks now resolve through the gate). Both based on cd64dbca, i.e. BEFORE the vocabulary fix -- rebase before pushing. [M]
- Working tree carries: modified `QueryBar.jsx` + `SessionTab.jsx` (a pasted-images frontend half with no backend), and untracked `tests/test_pasted_images_workspace.py` which MEASURES 8 failed / 0 passed (needs `server._ingest_turn_images` and `_is_registered_workspace`, neither exists). Do not commit it. [M]

## 2. Rulings
- R1 A denial is expressed in the AXIS'S OWN VOCABULARY. Level-typed axes deny as `"banned"`, never as boolean `False`; `container` is the only genuinely boolean axis. Merged in a82f07f1. [R]
- R2 One word per state in the UI. An effective permission renders as an OUTCOME (Disabled / Enabled / em-dash); a ceiling or a stored grant renders the raw token, because a cap is a statement about what is permitted, not about what you got. Merged in a82f07f1. [R]
- R3 Every human-visible producer resolves capabilities through `security.security_gate.get_workspace_capabilities` (which fails CLOSED), never through `load_workspace_capabilities(...) or WorkspaceCapabilities.default()` (which fails OPEN). Applies to display and reporting paths. [R]
- R4 `unknown` is a third state, never absence. A control that could not check must say so; rendering nothing is how the display claimed clean. [R]
- R5 DEFERRED WITH A MEASURED REASON: `web_ui/backend/config_manager.py` `resolve_effective_permissions` (~:1090) still uses the permissive default. The swap is NOT shape-preserving -- a boolean `False` capability renders as boolean `False` where the level vocabulary wants `"banned"` -- and the measured blast radius was 58 passed -> 11 failed. It needs its own red tests and a wire-contract decision. Do not "just fix" it. [M]
- R6 Merge rule: required subset of green, not all-green. Nothing merges without a green on its OWN head. A green on the wrong tree is not a pass, and a green check on an empty diff is not a pass. [R]
- R7 F2 (`--ext-diff` argv injection) -- treat `_git_diff` and `_git_show` as INDEPENDENT defects; measure `_git_show` first because it is the smaller surface. [R]
- R8 Mutation-proof contract gate is three separate items, not one: (c1) the harness, (c2) Contract 3's self-documented vacuity, (c3) CI wiring. Do not scope it as a single change. [A]
- R9 Claim Ledger direction: extend, do not convert. [A]
- R10 The C35/C36 collision is a standing ruling: no cleanup. [A]
- R11 The ruleset ticket is re-scoped to "align-to-11-or-retract-18". [A] The "11 unique contexts" figure is NOT verified in-repo.
- R12 SOU 05 is owned by Infra 67. [A]

## 3. What is true about the guarantee layer (all [M], re-measure before relying)
- The only sha256 tripwires in the repo pin the BYTES of a small set of onboarding documents. Coverage of `agent/ session/ infra/ tools/ web_ui/backend/`: 0 of 199 files content-pinned. It is a docs-staleness mechanism, NOT a security mechanism.
- Both numbered architecture invariants (container envelope; host access chokepoint) were enforced and tested but NOT merge-blocking: no contract named them and none of their detecting tests was in the contract gate.
- The contract gate is a hand-enumerated file list with ONE-WAY parity (`cited subset of run`). It catches a deleted test; it never catches an unwritten one. A new contract citing no test passes.
- `verify_doc_citations.py` checks that a cited line EXISTS AND IS IN BOUNDS, not that it is the RIGHT line. Measured rot it passes today: `architecture_invariants.md` cites `security_gate.py:1360-1361` where the code is at :1580, and `container_manager.py:2249-2252` where it is at :2260.

## 4. The security surface, located not fixed
- `_git_diff` appends caller-supplied `commit1`/`commit2` to argv with NO `_validate_git_ref`, and they arrive AFTER the hardening flags, so last-flag-wins gives `--ext-diff` (host code execution via a repo-local diff driver plus a crafted `.gitattributes` -- the `-c diff.external=` pin does not cover it), `--no-index -- /etc/passwd` (arbitrary host read) and `--output=` (arbitrary host write). `_git_show` is the same class. [M, via worker read of cited lines]
- `tests/security/test_git_hardening.py` is an ESCAPE suite, not a flag-presence suite: exactly one test pins argv and it pins the CONTAINER path. The host `hardened_args` list and the host `--no-verify` insertion are never argv-pinned. The escape it does not attempt is the one that works. [M]
- The kill switch is real and fail-closed (an unbound workspace DENIES) but it sits TWO FUNCTIONS ABOVE the spawn -- in `_host_execution_denied_reason` and inside `SandboxedExecution.run`. `_exec_host_raw` never consults `allow_host_resources`, so any direct caller steps around it. [M]
- Two further host git spawns are outside the audit and are NOT named as exclusions: `tools/ci_read_tool.py:260` (plain `subprocess.run`, FULL `os.environ`, no env strip, no audit) and `web_ui/backend/server.py:322/344/363` (runs at MODULE IMPORT, no gate). [M]
- The repo's named exclusions for the host port live in `docs/host-port.md:340-354`, not in `MANDATORY_STOP.md` (that is a vault file, not a repo file). [M]

## 5. Traps (paid for already -- do not pay again)
- A display fix that makes a fail-closed path REACHABLE will expose every consumer that assumed the fail-open one. The vocabulary bug was latent and green on `dev`; it went red on four Python versions the moment fail-closed became reachable in the permission routes. Expect the same the moment `unknown` becomes reachable.
- `unknown` conflated with a violation is a lie in the safe direction but still a lie. Conflating it with `within` is the original bug.
- A test can PIN an over-claim. `test_list_containers_session_scoping` asserted that an unreadable envelope produced no reason -- it asserted the silence was correct. When you find such a test, invert it and say why in the docstring.
- When a fixture lacks the store the production code now reads, fix the FIXTURE (axis isolation). Never re-pin permissive behaviour and never weaken an assertion to get green.
- Split implementation dispatch from verification dispatch. Implementation jobs in this repo burn 300+ tool calls and hit the token ceiling mid-report, losing execution before the final run. Verify-only jobs cost 20-36 calls and produce every number worth trusting.
- Demand the NEGATIVE CONTROL: have the verifier reintroduce the bug in a scratch plugin under /tmp and prove the new tests go red ON THE ASSERTION. That is what turns "47 passed" into "these tests bite".
- Two instruments on every destructive edit. When a worker edits an existing test to reach green, read the diff yourself AND have a second instrument read it.
- The worker sandbox has no `git` binary and a hard 600s ceiling; the full suite gets killed near 50%, so verification must be chunked and targeted. Any full-suite claim from a worker is a rumour until re-measured.

## 6. Work order for the next generation
1. Rebase and push `fix/envelope-status` and `fix/caps-display-closed` onto the current `dev`; they must not spend CI minutes on a stale base.
2. Diagnose the one red on #433 and the one red on #434 -- by REBASING onto a82f07f1 and re-running CI first, because a red on a stale base can be nothing but the base.
3. The F2 argv fix, as its own branch: validate `commit1`/`commit2`/`commit` as refs, allowlist `format`, add ARGV-PINNING tests on the HOST path, and move the host-resource gate down into `_exec_host_raw` so the kill switch cannot be stepped around.
4. #436's frontend half: `ContainerListPanel.jsx` still renders `envelope_status: "unknown"` as nothing. Silence is the bug wearing a hat.
5. Contract 4 (host chokepoint plus the git exclusions) -- barred until #434 lands, because #434 owns `docs/CONTRACTS.md`.

## 7. The metric
The metric is not "did we land more PRs than the last seat". It is: **can the next agent read the pack, find it true, and be productive within a day?**
---
