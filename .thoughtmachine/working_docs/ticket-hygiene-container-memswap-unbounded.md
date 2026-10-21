# Ticket: hygiene/container-memswap-unbounded

**Filed:** Gen 45, 2026-10-21, from the `docs/container_aggregate_envelope.md` §2.4 read.
**Severity:** high on impact — swap is the axis the memory incident actually exhausted.
**Status:** FIXED, awaiting merge. Branch `fix/container-default-mem-limit`, commit `fed55868`.

## Finding

No create path in the tree ever set `memswap_limit`. Only `mem_limit` was passed, at the single
create site (`client.containers.run(...)` in `infra/container_create.py:425-441`). Docker's
implicit default for a container created with a memory limit but no swap limit is swap equal to
the memory limit *in addition to it* — i.e. every container could occupy up to 2x its
`mem_limit` in RAM+swap before the kernel contained it.

The incident this came out of is quoted verbatim in the envelope ticket: "9 containers x 1 GB held
at 92% **swap** -> host freeze", followed by 8 kernel OOM kills attributed to `CONSTRAINT_MEMCG`.
Every one of those containers was inside its declared `mem_limit` (the per-container envelope
works and is enforced); what was uncapped was the pool it could spill into. So the per-container
limit was doing its job exactly as designed, and the swap analogue of that limit did not exist.

## Evidence

- Unbounded swap on this branch: `memswap_limit` / `memory_swap` occur zero times anywhere in
  `infra/`. The only `psutil.swap_memory` / `/proc/meminfo` reference in the app is logging
  (`agent/logging/__init__.py:601-614`).
- The single create site and its arguments: `infra/container_create.py:425-441`. Before the fix
  `mem_limit=` (`:439`) was the only memory argument passed; no swap argument appeared anywhere
  in the call or in the spec.
- Containers run with `oom_score_adj=1000` (`infra/container_manager.py:2112`, `:2257`) — they
  volunteer as the kernel's preferred victims, which protects the host *in principle* but, as the
  incident shows, not before swap is exhausted.

## Fix

Branch `fix/container-default-mem-limit`, commit `fed55868`: sets `memswap_limit=spec.mem_limit`
immediately after `mem_limit` at `infra/container_create.py:439-440`.

Policy chosen: **zero swap headroom** (equal, not the Docker-default 2x). The container is already
the designated OOM victim via `oom_score_adj=1000`, and an OOM kill surfaces as exit 137 /
`container.command_oom_killed` (`infra/container_manager.py:2519-2525`,
`_EXEC_OOM_EVENT = "container.command_oom_killed"`). The deliberate trade is "the container dies
fast and observably" over "the host thrashes while the kernel decides".

`memswap_limit` is **derived from** `mem_limit`, and deliberately not added as a spec field or a
record field. The invariant therefore holds by construction, and no new persisted state exists to
drift out of agreement with `mem_limit`.

## Tests

`tests/test_container_create_spec.py`, `tests/test_container_registry.py`,
`tests/test_container_start_drift.py`, `tests/test_container_record_permission_drift.py` now
assert the new kwarg.

Trap for anyone adding a further create kwarg:
`tests/test_container_record_permission_drift.py` pins the exact create-kwarg **set**
(`set(run_kwargs) == set(expected) | {"labels"}`) plus byte-exact values. A new kwarg must be added
to `expected` — adding an extra `assert` alongside is not enough, the set comparison will fail
first.

Full container suite on the fix branch: `python -m pytest tests/test_container_*.py -q` ->
**862 passed, 1 skipped**.

## Residual — do not read this as "swap is solved"

This bounds **per-container** swap only. The **aggregate** swap pool across all containers on the
host is still unbounded — nothing in the tree sums per-container commitments or measures host swap
headroom at admission. That is the envelope ticket's territory
(`docs/container_aggregate_envelope.md`, options B/C/D), not this one's, and closing this ticket
must not be used to argue the incident class is closed.

## Reference

- `docs/container_aggregate_envelope.md` §2.4 (authoritative statement + the fix's provenance),
  §4.2, §4.3, §5(i), §6 Q2, §7 — every `memswap_limit` mention there carries a pointer to §2.4.
- Branch `fix/container-default-mem-limit`, commit `fed55868`.
