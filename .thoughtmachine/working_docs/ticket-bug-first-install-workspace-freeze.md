# ticket: `bug/first-install-workspace-freeze`

**Filed:** 2026-10-29 (Gen 54 delivery arc)
**Source:** operator report, live use on a fresh install. **FILE ONLY — not fixed by this ticket.**
**Status:** filed, not investigated
**Severity:** high for a first-run experience — the first thing a new install asks you to do is the thing that hangs

## Symptom

On a **fresh install**, creating the **first** workspace freezes the web UI. The page stops
responding; **restarting ThoughtMachine clears it**, after which the workspace appears to exist
and the install proceeds.

So the failure is not "the workspace is corrupt" — a restart recovers without a repair step.
It is the UI (or the backend serving it) getting stuck while the first workspace is created,
and the stuck state not surviving a restart.

## Evidence

Operator report only. This ticket is a **capture task first**: no reproduction, no log excerpt
and no stack dump exist yet. Nothing in the 187 `ticket-*.md` files covers it (checked
2026-10-29).

What can already be said from the tree — the fresh-install path is *not* the normal
workspace-create path, which is the first thing to test:

- First-install creation runs through the onboarding wizard:
  `web_ui/frontend/src/components/OnboardingWizard.jsx` calls `POST /api/browse/create` twice
  (`:258`, `:269` — create `~/workspaces`, then the chosen directory), then
  `POST /api/workspace/resolve` (`:280`), then `POST /api/onboarding/complete` (`:299`);
  `POST /api/onboarding/test-connection` at `:196`.
- The wizard's own header documents that order: *"POST /api/browse/create (ensuring
  ~/workspaces exists first, ...) registers it via POST /api/workspace/resolve → screen 4"*
  (`OnboardingWizard.jsx:20-23`).
- **Registration by resolve, not by create.** `POST /api/workspace/resolve` is
  `web_ui/backend/workspace_routes.py:115`; the ordinary create is `POST /api/workspace`
  (`workspace_routes.py:1770`, `status_code=201`). Two different registration paths — whether
  the freeze lives in one, the other, or in the vault write they share is **unestablished**.
- Supporting routes: `POST /api/browse/create` = `web_ui/backend/server.py:3967`
  (`GET /api/browse` = `:3886`); onboarding status/complete =
  `web_ui/backend/onboarding_routes.py:5-6`.

The "restart clears it" fingerprint is the strongest clue and should drive the first read: it
points at **in-process state** — a registry/cache/lock built at import or in the server
`lifespan` (the same pattern as `_SERVER_REVISION`, captured at import in
`web_ui/backend/server.py`) — rather than at on-disk corruption. A blocking call on the event
loop (sync `subprocess`, `time.sleep`, `.result()` on an awaitable) or a lock re-acquired
non-reentrantly would produce exactly this shape.

## Options (needs a ruling; do not implement blind)

1. **Capture first (recommended; no code change).** Reproduce on a throwaway vault with the
   backend log attached and a stack dump of the hung process; compare `/api/workspace/resolve`
   against `POST /api/workspace`. This is a filed-not-diagnosed bug — fixing it before it is
   reproduced repeats the pattern the container tickets already warn about ("needs one live
   repro").
2. **Bound it defensively.** Put a timeout on the first-workspace path so the UI shows an error
   instead of freezing. Does not diagnose anything; makes the failure honest and keeps the
   operator unblocked. Can ship before the root cause is known.
3. **Remove the asymmetry.** Make first install use `POST /api/workspace`, the same route every
   later workspace uses, and delete the browse/create + resolve sequence from the wizard. This
   is a plausible root cause (two paths, one exercised only once per install) but it is not
   proven, and it rewrites onboarding — needs the ruling before touching it.
4. **Document it.** "Restart after first install" in the installation guide. Cheap; leaves a
   first-run hang in a shipped product.

## Acceptance criteria

- Reproduced from a clean vault with the log captured — **or** a definite statement that it does
  not reproduce on a named build, with the build named.
- The blocking call is named (file + symbol), not described as "the UI froze".
- On a fresh install, creating the first workspace either completes or fails with a visible
  error. Never an unresponsive UI.
- A regression test creates the first workspace on a temporary vault and asserts the API
  responds within a bounded time.

## Related

- `ticket-workspace-create-missing-root-write.md` — same code path (workspace creation), different
  symptom: API-created workspaces never get `cfg["root"]` and stay host-bash-ish. Read first; a
  half-registered workspace may leave the in-memory state inconsistent.
- `ticket-bug-create-session-drops-workspace-id.md` — workspace attribution defects on the
  session side.
- `ticket-hygiene-vault-writer-audit.md` — the vault write path used by registration.
- `ticket-feat-workspace-deletion.md` — the inverse operation; if create needs a timeout/ack, so
  does delete.
