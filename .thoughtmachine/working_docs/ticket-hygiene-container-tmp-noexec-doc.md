# Ticket: hygiene/container-tmp-noexec-doc

**Filed:** 2026-10-20
**Source:** gen-47 container-ops run — dispatched finding F13 (orchestrator summary).
**Severity:** low — documentation only; behaviour is intentional and correct.
**Status:** filed, not dispatched.
**Report cross-ref:** `.thoughtmachine/working_docs/report-gen47-container-ops-run-2026-10-20.md` — read in full (384 lines); `## Findings` (L318) has **F1–F10 only**, no Appendix A/B, no F11–F22. F13 has **no citable report line numbers**; content is the dispatch summary.

## Finding

`/tmp` inside executor containers is mounted as a hardened tmpfs with `noexec`. Confirmed at HEAD: `agent/config/defaults.py:198` — `EXEC_TMPFS_OPTIONS = "rw,noexec,nosuid,size=512m"`, applied at `infra/container_manager.py:2171`, `infra/containers/executor.py:929`, `infra/resource_container_manager.py:1089`.

Consequence: `gcc -o /tmp/a.out && /tmp/a.out` compiles successfully and then fails `Permission denied` on exec. Nothing in the failure names the mount option, so the natural reading is "the toolchain in this image is broken" — an agent or operator then spends turns on gcc/compiler/`PATH` rather than on the one-command workaround (`-o ./a.out` in the workspace mount, or `-o ~/a.out`).

The mount is deliberate (writable tmpfs, no `setuid`, no exec from world-writable space). The gap is that the property is invisible at the point of use: it is documented nowhere an agent reads, and it surfaces only as an errno.

## Fix shape (not written)

Document the `/tmp` contract where container capabilities are described (tool docstrings / the executor description an agent sees): `/tmp` is writable, `noexec`, `nosuid`, 512 MB — compile and link products must be written to the workspace mount. Optionally have the exec path recognise `EACCES` on a `/tmp/*` argv[0] and append a one-line hint. No change to the mount options.

## Bearing

Zero risk, recurring cost: every run that compiles anything is one wrong turn away from this. It is the cheapest possible entry in the "the sandbox is not a normal host" documentation set, alongside `ticket-hygiene-displayed-enforced-container-display-bugs.md`.
