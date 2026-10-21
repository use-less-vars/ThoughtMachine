# Ticket: feat/executor-image-provisioning

**Filed:** Gen 44, 2026-10-16, from friend's-machine report (delivery-layer).
**Severity:** HIGH for Delivery — fresh install cannot start containers.
**Status:** filed, not dispatched. Delivery arc. Reference case: friend's Windows machine.

## What's missing

On a machine without a pre-existing `agent-executor` image, no container can start:

1. `ContainerBuildTool` is a no-op there — returns `{"success": false, "error":
   "Docker build completed but no image ID was returned"}` in 1.7s, no build
   log, identical on retry. Its only argument is `tag`; it cannot be pointed at
   a Dockerfile or build context.
2. No vault-managed `<workspace>/Dockerfile` exists → nothing to build.
3. `ContainerStartTool(image="agent-executor")` → registry pull → 404.
4. `ContainerStartTool(no image)` → same 404. Default resolution is
   pull-first; local images are not consulted.

## Why it matters — Layer 5

`machine_model.md` Layer 5 (Delivery) already names this as the sharpest gap:
"A fresh install boots and runs a session" — marked weak, smoke test missing.
This is that gap, exercised on a real machine.

The container subsystem works on the operator's machine only because the
`agent-executor` image is present from an earlier build. There is no
fresh-install path.

## Two questions to answer before a fix shape

1. **Does the platform fall back to a local image when the pull fails, or
   abort?** A single test settles it: build `agent-executor:latest` locally,
   then call `ContainerStartTool` with no image. Succeeds → fallback exists,
   the fix is "seed the image on install." Still 404s → the fix is
   operator-side (publish to the registry the platform pulls from, or patch
   the resolution to be local-first).
2. **What registry is `agent-executor` configured to pull from?** Read the
   provisioning code. If it's a real registry, the friend's fix is a push.
   If it's configured to a name that doesn't exist, that's the config bug.

## Candidate fix shapes (Main rules)

- **(a)** Build on first boot if absent.
- **(b)** Bundle the image in the installer.
- **(c)** Local-first fallback in `ContainerStartTool` (check `docker images`
  before `images/create`).
- **(d)** Publish to a real registry + document the tag.

**(c) is the correct long-term shape** — the Delivery layer should not require
a registry reachable at every boot. But (a) or (b) may be the fastest unblock.

## Windows-specific

Reference machine is Windows. Docker Desktop. Possible extra factors: path
handling in the build context, WSL2 backend image visibility, tag namespace
mismatch. Confirm on-host whether the image exists at all before ruling.

## MUST NOT TOUCH
Standard list.

## Bearing

Arc 1 (Delivery). Opens the Layer 5 proof point that #107 (ci_read) started.
Independent of the container lifecycle work; runs after 9a's tail.
