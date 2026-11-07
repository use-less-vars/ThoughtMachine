# Delivery Arc Backlog (post-Core-Sprint)

Filed 2026-10-29 (Gen 54 housekeeping) against `dev` @ `adfb5ef6`. **Re-read HEAD before acting on any
line here** — see `working_agreements.md` → `verified-at-a-moving-head`.

## 1. `ci/promote-vitest-gating` — IN FLIGHT (branch exists, local + remote)
**Correction to the operator's framing:** the frontend suite is **already wired** into GitHub Actions
and has been for a while. `.github/workflows/platform-matrix.yml` runs `web-ui-frontend-suite`:
`npm ci` → `npx vitest run` → a JSON-report assertion with a **650-test floor** (measured baseline 681
tests / 52 files) so the suite cannot silently shrink, plus an artifact upload on failure.
**The real gap:** the job carries `continue-on-error: true` and is labelled `(advisory)` — a red
frontend run is reported and then ignored. **Action:** promote to blocking (drop `continue-on-error`,
add to required checks). The floor assertion is what makes promoting it safe; do not promote without
keeping the floor.
**Do not** file a "wire vitest into CI" ticket — that work is done.

## 2. `docs/global-kb-workspace-runtime-guidance` — IN FLIGHT (branch `docs/workspace-runtime-guidance`)
Teach the agent the mental model it keeps getting wrong:
*"Workspace Image = runtime. Host Bash = bootstrapping. Turn `host_bash` off once the image is built."*
Companion doc: `docs/global-kb-workspace-runtime-guidance.md`.

## 3. `feat/workspace-image-rebuild` — NEW
One verb to replace the ~40-command Docker dance for rebuilding a workspace image. Candidate shape:
`ContainerBuildTool` already builds from the vault-managed Dockerfile with a workspace-derived
`agent-executor-<hash>` tag and `nocache`; the missing piece is a first-class *rebuild-and-rebind*
verb (build → stop → remove → start → verify) that reports each stage, rather than requiring the
operator to sequence five tools by hand.

## 4. `design/sysprompt-addendum` — NEW
First-class memory that survives `SummarizeTool` pruning. Today the durable facts live in the KB and
must be re-read each session; the summarizer competes with them for the same window.

## 5. `design/credential-delivery` — NEW (with a live observation)
The secure channel for external credentials. Observed this session: a tool granted
`network: outbound` **still cannot push**, because the git write path requires a credential
(`TM_GIT_ASKPASS_SECRET` via `thoughtmachine.git_auth_env`) and `credential` was banned for the
session — the failure mode is "no git credential is configured", which reads like a network problem
and is not one. Parallel sessions *can* push, so the delivery path works; what is missing is (a) a
truthful error that names the missing grant, and (b) a documented mapping from
`session_permissions['credential']` to the git askpass env.

## 6. `design/rename-tima` — NEW
Rebrand to **Tima** (The Intention Machine). Cosmetic, but wide: package names, CLI verbs, docs,
vault paths, and every KB entry that names the product. Needs a migration note for existing vaults
before any rename lands.

## 7. Cross-reference — `bug/reasoning-block-pop-in` is FIXED but UNMERGED
See `backlog-core-integration.md` §1. The fix is committed on `fix/reasoning-ui-streaming`
(`7c5f1fae`) and the branch has **no remote counterpart**. Push + PR before the next UI change, or it
will be re-implemented by someone reading the old (wrong) diagnosis.
