# MANDATORY STOP — read on entry

Read-on-entry for every new Main engineer. Reviewed at every handoff.
Items promoted from observations → design decisions → migration strands → closed get moved to an "archive" section, not deleted.

1. Read Gen 26's handoff, the baseline doc, the current-state doc.
1a. Read `security_architecture.md` §Escape-to-host. The four boundaries are named there; the "security boundary held" sentence is the bug class at the narrative layer.
1c. Before touching host execution, read `docs/host-port.md` §1, §4, §9. §1 is the claim and its two qualifications (including that CI enforces only the honest case). §4 says the grain is a *category name*. §9 says what the port does not cover, including the named exclusions (git's `_exec_host_raw`, container git).
2. Read `operator_observations.md` — the operator's repeated points, the things that keep getting lost through migration cycles.
3. Read `working_agreements.md`.
4. Read the design decisions (1 and 2 when 2 lands).
5. Read the recon map.
6. Verify live state: dev HEAD, branch protection, open branches, open strands.
7. Report to operator: what is current, what is blocked, what is next.
<!-- Housekeeping note (Gen 48): the header above advertises an archive section that does not exist. Not added this pass; tracked for a later turn. -->
