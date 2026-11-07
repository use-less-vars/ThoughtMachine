# Ticket: escalation-reword (post admit() coverage read)

**Filed:** Gen 45, 2026-10-17, correcting the audit's framing.

## The audit's framing was wrong

Audit Q7 said "escalation is not airtight." The admit() coverage read
confirmed:

- ALL agent-reachable create paths call `admit()`. Zero skips.
- The primitives that pass `admission=None` are gated by their callers.
  No `tools/` caller exists.
- The `admission=None` opt-in hole is latent, not live.

## What actually remains

Gates run; the axes are vacuous:

1. `ContainerSpec(image=None)` → admit's image-allowlist is a no-op on
   the agent `image` path and `docker_code_runner.image` path.
2. `_narrow` never constrains mem/cpu/oom. No memory or CPU ceiling
   exists on any path.
3. Ephemeral containers are uncapped (ceiling counts persistent only).
4. Two independent counters may disagree (see separate ticket).

## Fix shape

Not a security P0 wave. A design pass: define the axes (mem ceiling,
cpu ceiling, image allowlist wiring) and close them in one pass. Sequence
after the container close and Credential contract start.

## Reference

`.thoughtmachine/working_docs/admit-coverage-read.md` §C
