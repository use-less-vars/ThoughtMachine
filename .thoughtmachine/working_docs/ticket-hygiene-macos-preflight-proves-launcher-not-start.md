# Ticket: hygiene/macos-preflight-proves-launcher-not-start

**Filed:** 2026-10-22, bookkeeping chunk C. Line numbers read from `.github/workflows/cross-platform-smoke.yml` (358 lines), `.github/workflows/platform-matrix.yml` (467 lines) and `scripts/smoke_windows.ps1` at time of writing.
**Verified at:** the workflow files themselves, read in full for this ticket. No CI run was inspected; every claim below is a claim about what the YAML asserts, checked against the YAML.
**Source:** cross-platform-smoke.yml header comment (L21-24) and the `macos-launcher` job (L252-358); the `windows-smoke` job (L123-231); platform-matrix.yml macOS jobs (L222-467) and `linux-install-smoke` (L92-122).
**Severity:** MEDIUM — process, not product. The cost is that the only gating macOS signal cannot observe whether the application starts, so a macOS start-up regression has no RED to land on.
**Status:** filed, not dispatched.

## Defect

The gating macOS job asserts the *launcher* class of macOS code paths. It does not start the application.

`macos-launcher` (`cross-platform-smoke.yml:252`) is named "macOS launcher preflight (gating)" (`:253`), runs on `macos-latest` (`:254`), and carries no `if:` and no `needs:` — nothing about it is conditional. Its own comment block says "GATING: deliberately NO continue-on-error" (`:234`). It has three steps:

- `:266-295` `[macOS launcher] port check uses the lsof fallback` — asserts `ss` is absent (`:272`) and `lsof` present (`:273`), runs `python3 scripts/doctor_checks.py --check-port "$PORT"` (`:274`) against a random port in 49152-65535 (`:271`), and asserts the detail text neither says "cannot verify" (`:285`) nor "Neither ss nor lsof" (`:286`), names the port (`:287`), and reads `port N is free` or starts `N is in use by ` (`:288`).
- `:307-337` `[macOS launcher] Darwin skip for Linux-only tool requirements fires` — `doctor_checks.py --check-tools` (`:310`), asserting `ok is False` (`:315`) with `"ss" in critical_missing` (`:316`), then `bash start_thoughtmachine.sh --check-only` in the background (`:322`) polled 60×1s for the marker `macOS detected - skipping Linux-only tool requirements.` (`:320`, `:325-329`).
- `:342-358` `[macOS launcher] doctor names Docker unusable in its own terms` — `doctor_checks.py --check-docker` (`:345`); if `ok` it must read `docker info succeeded`, otherwise the `reason` must be one of `permission_denied`, `daemon_down`, `lib_missing`, `other` with a non-empty detail.

None of the three starts a server. There is no `uvicorn`, no `web_ui.backend.server`, no `curl`, no `/api/health`, no model call anywhere in the job. The mechanism that decides this is `--check-only`, defined at `:70-73` as the "Full pre-launch gate **without starting anything**".

So the gated macOS signal answers "can this launcher reach its pre-launch gate and name its Docker verdict in its own terms". It cannot answer "does the app start and serve".

## Mechanism — why the gap is deliberate and still a gap

1. The omission is documented, not accidental. `cross-platform-smoke.yml:247-250` states the reason a fourth assertion was left out:

   > A fourth assertion (iv) - `--check-only` reaching [8/8] with rc 0 - is OMITTED on purpose: it hinges on the [1/8] venv, [5/8] node, [6/8] vault and [7/8] locale checks, which cannot be proven deterministically from code alone, so shipping it GATING would risk a flaky RED.

   That is an honest trade. It also means the one gating macOS job stops one step short of the thing it is a preflight *for*.

2. The file header names two classes of macOS code path — "the LAUNCHER class … the SUBSTRATE class (Docker Desktop + VirtioFS host integration) is a NAMED GAP, not asserted" (`:21-24`). Both classes are about whether the launcher can run on the host. Neither is about whether the application serves a request.

3. Start-and-probe coverage for macOS does exist — in a different file, in an advisory lane. `macos-install-smoke` (`platform-matrix.yml:301`) does start the backend: `.venv/bin/python -m uvicorn web_ui.backend.server:app --host 127.0.0.1 --port "${PORT}"` (`:350`), then curls `/api/health` (`:356`) and `/api/health/containers` (`:368`) before killing it (`:374`). But the job carries `continue-on-error: true` (`:304`), under the file's own header "All macOS jobs are advisory (continue-on-error: true)" (`:30`). `macos-suite` (`:222`, advisory at `:225`) and `macos-docker-probe` (`:423`, advisory at `:426`) are the same shape. So the coverage is non-gating, and it drives `uvicorn …:app` directly rather than the launcher, so it does not test the launch path either.

4. The other two platforms are shaped the other way round. Windows starts the app and polls it: `windows-smoke` (`cross-platform-smoke.yml:123`) runs `powershell -File scripts\smoke_windows.ps1` (`:228-231`), which polls `http://127.0.0.1:8000/api/health` (`scripts/smoke_windows.ps1:177`) for a 200 (`:178`) over a 60×1s window (`:175-185`). Linux does the same inside `cross-platform-smoke.yml`: `linux-install` (`:43`) starts `.venv/bin/python -m web_ui.backend.server` (`:92`) and polls `/api/health` up to 60s (`:96-113`). Linux additionally runs a blocking install-and-start smoke in `platform-matrix.yml:92-122`, which its comment describes as driving "the real install.sh and the real start_thoughtmachine.sh … then makes one live turn through the running backend" (`:86-89`) — the comment `:89-90` is explicit that it is "NOT advisory: a failure fails the workflow (no continue-on-error)".

Net consequence: on macOS the clone → install → start → answer path has no gating RED available to it. The gated signal covers the launcher's pre-launch gate, not the start.

## Fix options

**(a) Finish the preflight inside the gating job.** Extend `macos-launcher` so `--check-only` runs to completion and a `rc 0` is asserted, instead of the omission at `:247-250`. The stated objection there is that assertions (iv) depends on the `[1/8]` venv, `[5/8]` node, `[6/8]` vault and `[7/8]` locale checks, which are not provable from code alone. That objection is about *how much of the gate* can be asserted deterministically; it does not make a start assertion impossible. (a) still stops short of serving a request.

**(b) Add a macOS job that mirrors the Windows probe.** Poll `/api/health` on `127.0.0.1` after the launcher has run, the way `scripts/smoke_windows.ps1:177` and `cross-platform-smoke.yml:96-113` already do, and make it gating. This is the smallest change that puts a start signal on the gated path. It would supersede nothing in (a).

**(c) Full answer smoke on macOS.** Start the app and make one live turn, as `linux-install-smoke` does per `platform-matrix.yml:86-89`. This is the only option that covers the class actually shipped. It needs a model and a credential, so it may not be appropriate on every PR — that is a fact about the option, not a cost estimate, and no runner-minute figure is invented here.

**Recommendation: undecided — left to the operator.** See TODO below; the three options differ in what they cost in flakiness and in secrets, and that is the operator's call to make, not this ticket's.

## TODO(operator)

Choose between (a), (b) and (c) above. The decision is a trade between flake risk on the gated path (a, b) and credential and runtime requirements (c). No recommendation is recorded here on purpose. If (b) or (c) is taken, decide whether `macos-install-smoke`'s advisory start-and-probe (`platform-matrix.yml:347-374`) should then be retired rather than left to drift alongside a gating successor.

## Bearing

MEDIUM, and it is a coverage claim rather than a live breakage. Nothing is broken today; what is broken is the inference from "macOS is gated" to "the macOS ship path is tested". Those are two different statements, and only the first is true while `macos-launcher` is the sole gating macOS job.

Separate from `ticket-hygiene-macos-advisory-hides-real-failures.md`. That ticket is about an advisory lane hiding real failures. This one is about a *gating* lane whose assertions stop before the application starts — a false-confidence defect in the opposite direction. Fixing one does not fix the other.
