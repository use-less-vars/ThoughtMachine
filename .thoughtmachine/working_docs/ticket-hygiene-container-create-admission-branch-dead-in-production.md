# Ticket: hygiene/container-create-admission-branch-dead-in-production

**Filed:** Gen 45, 2026-10-21, from the `docs/container_aggregate_envelope.md` §4.1 v0.1.2 correction.
**Severity:** medium — hygiene. NOT a security hole: every production create is gated, just upstream.
**Status:** open; not dispatched. Disposition choice is the operator's.

## Finding

`infra/container_create.py` carries its own admission gate, and in production that gate cannot
execute. The guard is `if admission is not None:` at `infra/container_create.py:387`; the gate
itself is `admit(...)` at `:407`. Nothing in production ever passes an `admission`.

Meanwhile each of the four hot paths gates *before* it creates, independently, with its own
hand-built `AdmissionRequest`. So the tree holds four copies of one gate and one gate that never
runs. The funnel looks gated and is not: any create path added through
`create_hardened_container(...)` in future inherits no backstop at all.

## Evidence

1. Guard and gate in the primitive: `infra/container_create.py:387` (`if admission is not None:`),
   gate at `:407`, `AdmissionDenied` at `:409`.

2. All four call sites of the **primitive** `create_hardened_container(...)` pass `admission=None`:
   `infra/container_manager.py:2116`, `infra/container_registry.py:339`,
   `infra/resource_container_manager.py:1211`, `infra/containers/executor.py:1065`.

   **Two functions carry this name — do not conflate them.**
   - `infra/container_registry.py:231` is the **wrapper** ("THE single hardened create path").
     It **owns** the admission gate (`admit(...)` at `:303`) plus the root-host `uid 0` guard
     (`:270-275`), and only gates when `workspace_id is not None` (`:281`).
   - `infra/container_create.py:351` is the **primitive**. Its `admission` parameter is
     optional and every production caller declines it.

   `container_registry.py:339` is the **wrapper delegating** to the primitive
   (`admission=None, record=False`; `record=False` because the caller owns the record attach).
   It is therefore *not* a hot path declining admission — the wrapper has already gated at
   `:303` before it reaches that line. The genuine hot-path call into the wrapper, which is
   the one that exercises the gate, is `infra/container_registry.py:714`.

3. `AdmissionContext(` has zero construction sites in production. The only three in the tree are
   in tests: `tests/test_container_create_spec.py:159`, `:294`, `:314`.

4. Therefore `infra/container_create.py:407` is unreachable in production.

5. Each module instead gates on its own, upstream of the primitive:
   - `infra/container_manager.py:2267` — `_fresh_start()` builds a full `AdmissionRequest` at
     `:2245-2266` and calls `admit(_admission_request, probes=ClientProbes(self.client))` at
     `:2267`, returning an error dict on `Deny` (`:2268-2269`).
   - `infra/container_registry.py:303`.
   - `infra/containers/executor.py:994`.
   - `infra/resource_container_manager.py:1127`.

6. Consequence: four duplicated gates, four duplicated `AdmissionRequest` constructions, four
   places to keep in step — the audit's "one fact, N sites" pattern — plus a primitive whose
   signature advertises admission that the callers all decline.

## What this is not

Not a live hole. Every production create passes a gate; the admission step simply lives in the
caller, not in the primitive. The only creates in the tree outside any gate are
`live_smoke_docker.py:233` and `:244`: a standalone, host-runnable smoke script that calls
`client.containers.run(...)` directly, passes no `mem_limit`, is imported by nothing, and is not a
security boundary. Do not "fix" the smoke script; it is a dev harness.

## History — the false premise this ticket was hiding behind

An earlier reading of this tree claimed `tools/docker_code_runner.py` created containers and
"never calls `admit`", and treated that file as the admission bypass. That claim was refuted by
direct read:

- `tools/docker_code_runner.py:394-402` is a `ContainerManager(` **constructor** call. The
  `mem_limit=` at `:400` is a ctor kwarg, not a `containers.run` kwarg.
- The file contains no `containers.run(` at all.
- The create runs through `manager.start(...)` (`:411-417`) → `infra/container_manager.py:2068` →
  `_fresh_start()` (`:2129`) → `admit(...)` at `:2267`.

The earlier reading was a grep artefact: the file imports no admission symbol because it never
reaches the create primitive. With the phantom bypass removed, the real defect next door became
visible — this ticket. Corrected in `docs/container_aggregate_envelope.md` v0.1.2; §4.1, §4.2,
§4.3, §5(ii), §6(6), §7 were amended in place.

## Disposition options

**A (recommended) — wire `AdmissionContext` through the four callers and delete the four
per-module gates.** One gate, in one place: the primitive. Matches
`docs/container_subsystem_target_architecture.md:22-24` ("one admission gate"), and a new create
path gets the gate by construction rather than by the author's diligence. Cost: `AdmissionContext`
becomes a real type crossing four module boundaries; `Probes` construction moves to the callers or
into the passed context.

**B (defensible) — delete the dead branch plus `AdmissionContext` and its tests, keep the four
upstream gates.** Cheaper, zero behavioural risk, and honest about how the tree actually works.
The objection: it keeps "one fact, N sites" as the design and re-opens this exact failure mode for
the fifth create path.

Record both; the operator picks. Either way the current state — four gates plus a fifth that never
fires — should not survive the decision.

## MUST NOT TOUCH

No behaviour change under option B beyond deleting unreachable code and its tests. Under option A,
the denial contract (`security/admission_gate.py:1-8` — never raises, fail-closed) and the reason
codes (`:93-105`) must not change.

## Correction (Gen 47, 2026-10-21)

Re-read against the live tree on `fix/ws-dispatch-ownership`. **The conclusion stands; two
supporting claims did not.**

**What was wrong.** Evidence item 2 conflated two different functions that share the name
`create_hardened_container`. `infra/container_registry.py:231` is the wrapper that **owns** the
gate (`:303`) and the root-host `uid 0` guard (`:270-275`, inside `if workspace_id is not None:`
`:281`); `infra/container_create.py:351` is the primitive whose `admission` parameter every
production caller declines. `container_registry.py:339` — listed above as a hot path declining
admission — is the wrapper *delegating* to the primitive (`admission=None, record=False`), so it
is downstream of the gate, not beside it. The hot-path call that actually exercises the wrapper's
gate is `container_registry.py:714`, which passes `workspace_id`, so the gate runs.

**What still holds.** This is **not** a wrong-envelope hole. The envelope SSOT is
`security_gate.resolve_container_config`, reached from `infra/container_manager.py:4810`; it is
fail-closed: an exception yields `("none", "ro")` at `:4818`, a non-`ContainerConfig` return
yields the same at `:4825`, and `strict=True` re-raises at `:4817`/`:4821`. Envelope correctness
does not depend on the admission branch.

**What the defect actually is.** `admit(` is live at exactly four sites — `container_registry.py:303`,
`container_manager.py:2268`, `resource_container_manager.py:1127`, `containers/executor.py:995` —
and the primitive's own gate at `container_create.py:407` is fifth but unreachable. So the
guarantee is real but **upstream and per-caller**, not by construction: the fifth create path
author inherits nothing. The `Transform` narrowing at `container_create.py:410-411` can never fire
centrally — a `Transform` returned to any caller is handled by that caller, not by the primitive.

**Verdict:** Medium, hygiene. Unchanged. Option A (wire `AdmissionContext` through, delete the four
per-module gates) remains the recommendation; Option B remains defensible.

**Line-number drift — re-derive before dispatch.** The ticket's two gate sites are off by one
against the live tree: `container_manager.py:2267` is the closing paren of the `AdmissionRequest`
(the `admit(` call is `:2268`), and `containers/executor.py:994` is the `_admission_wsid` line
(the `admit(` call is `:995`). The call-site lines were also shifted by the same span:
`container_manager.py:2115`→`:2116`, `containers/executor.py:1064`→`:1065`. Item 2 above has
been corrected in place; items 5's two lines were left as filed and are corrected here.

## Reference

- `docs/container_aggregate_envelope.md` §4.1 ("The real defect next door — a dead admission
  branch"), §5(ii), §6 Q6, §7, §8 v0.1.2 row.
- `docs/container_subsystem_target_architecture.md:22-24`, `:164-166`.
