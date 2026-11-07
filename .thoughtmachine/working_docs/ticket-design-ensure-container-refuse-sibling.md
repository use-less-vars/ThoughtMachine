# Ticket: design/ensure-container-refuse-sibling

**Filed:** Gen 45, 2026-10-17, on Main Gen 38's ruling.
**Type:** design mini-spec, then fix.
**Severity:** residual in T1. Not the operator's field bug (already closed).
**Status:** filed, not dispatched.

## Finding

§9 of `design_container_lifecycle_contract.md` governs: `_ensure_container`
on a live workspace-owned container that has drifted must **refuse, do not
destroy.** The name-matched drift path already implements this. The cached
fast path contradicts it — it calls `_reap_container`, hard-coding
`ContainerIntent(kind="reap", explicit=True, reason="ensure_container_recreate")`.
`_decide` step (1) reaps unconditionally. The funnel is consulted and emits
an event, but cannot refuse — the decision is a rubber stamp.

Fact 3 (Lifecycle) holds — one funnel, one decision authority. §9 does not
hold at that call site.

## Why a mini-spec, not a patch

§9's "recreate a new container **alongside** the drifted one" collides with:

1. **Envelope-keyed names.** Container names are
   `agent-exec-persistent-{ws_hash}-{env_hash}`. Sibling creation either
   collides or requires renaming the drifted one.
2. **One record ↔ one `container_id` label.** Which container does the
   record point at after the sibling exists?
3. **Panel display.** Both containers show as the workspace's machine; the
   user cannot tell which is which.
4. **Orphaned job.** The drifted container may be running a long job. It
   cannot be used by this session (envelope mismatch), but it may still be
   doing work. Stopped? Left running? TTL-reaped?

## Read first

- `_ensure_container` in `infra/containers/executor.py` — both paths
- `_reap_container` — docstring, intent, audit key
- `ContainerLifecycle._decide` — steps 1, 10, 11
- `thoughtmachine/container_record/` — record/label identity, how a
  container_id is bound
- `tests/trust/test_toggle_live.py` — three tests pin the contradicting
  behaviour (`test_recreation_when_permissions_change` def 226,
  `test_ask_to_write_recreates_container` def 392,
  `test_write_to_ask_recreates_container` def 453). All three must be
  rewritten to implement §9.
- `wave9a-t1b-spec.md` — `### B1 (~:723) -- fast-path drift -- REFUSE`
  (the original scope; shipped for name-matched path only)
- `design_container_lifecycle_contract.md` — §6 (superseded), §7, §9

## Sequence

After Credential Resource Contract drafting begins; parallel is acceptable
(disjoint — container vs credential). **Elevate** if the fast path fires
during the manual protocol.

## Reference

Recon rounds 1 and 2 at HEAD 586aa2df:
- `.thoughtmachine/working_docs/container-close-residuals.md`
- `.thoughtmachine/working_docs/container-close-recon-2.md`
