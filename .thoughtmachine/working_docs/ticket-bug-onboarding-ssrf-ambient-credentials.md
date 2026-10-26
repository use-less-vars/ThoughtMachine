# Ticket: bug/onboarding-ssrf-ambient-credentials

**Filed:** 2026-10-25, from the Gen 50 client-claim red team pass; findings drawn from `redteam-surface-complete.md` in this corpus.
**Severity:** CRITICAL
**Status:** draft; verified by read — the read is the code path, the request-on-the-wire is inferred, not observed — `UNRUN: no live session`.
**Sibling:** none — the credential-in-ambient-use class is not separately ticketed.

## Finding

`POST /api/onboarding/test-connection` takes a caller-supplied `base_url` and passes it through `ProviderFactory` into the provider SDK. No scheme check, no host check, no private-range check anywhere on that path. Omitting `api_key` makes the call fall back to the server's own `OPENAI_API_KEY` environment variable, so the server signs a request **with its own credential** against a URL the caller chose.

## Bearing

The caller controls the destination and the server attaches the credential. Two consequences follow from that one fact: the response is a reachability oracle for anything the host can route to, including internal addresses the caller cannot otherwise address; and the provider credential is spent against third-party infrastructure of the caller's choosing.

Located by the surface inventory (row 82 area of `redteam-surface-complete.md`; also recorded in `## New findings (out of wave scope)` there) — not found by the red team's U-groups, so treat the inventory as the source of record.

## Blast radius

Anything the host can reach on the network, addressed by an unauthenticated caller, with the deployment's own provider credential as the payment. Severity CRITICAL on the combination; either half alone would be HIGH.

## What this is not

- Not path traversal.
- Not the same class as F1-F7 — those are workspace-scoping defects and this is an outbound-request defect; do not fold it into that wave.
- Not a credential leak *to* the caller: the key is not returned, it is *spent* and its validity is observable.

## Fix shape

Scheme allowlist, host resolution + private/link-local/loopback range refusal, and refusing the ambient-key fallback for a caller-supplied `base_url` — the last of these is the one that makes the other two more than defence in depth.
