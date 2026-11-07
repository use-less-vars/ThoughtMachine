# Ticket: hygiene/credential-regex-collision-git-askpass

**Filed:** 2026-10-24, from the W4 proposal-gate finding
**Category:** hygiene
**Severity:** HIGH — blocks the port migration
**Status:** filed, not fixed

## Summary

W3's `CREDENTIAL_SHAPED` regex refuses two of W3's own builder keys. The
credential env that `build_git_auth_env` produces cannot pass through
`host_run`'s `credential_inject=False` default. The only escape is
`credential_inject=True`, which disables the F-A inherited-env strip —
handing the git child the full parent environment. (That hole used to be
disclosed at `docs/host-port.md` §7; it is closed in the default path since
W3a, so the citation this summary originally carried no longer says what it
said — corrected in the W4 doc-alignment commit, 2026-10-25. The toggle still
turns the strip off, which is the point here.)

Confirmed by execution against the shipped regex:

    GIT_ASKPASS              passes
    GIT_CONFIG_COUNT         passes
    GIT_CONFIG_VALUE_0       passes
    GIT_TERMINAL_PROMPT      passes
    TM_GIT_ASKPASS_SECRET    REFUSED
    GIT_CONFIG_KEY_0         REFUSED

## Root cause

Two key-shape problems, distinct:

1. `TM_GIT_ASKPASS_SECRET` — the builder named its own value-key with a
   `_SECRET` suffix. `SECRET` matches the regex as a `_`-delimited segment.
   The strip was correct; the builder was wrong about what "secret" means.
   The value the askpass script echoes is a credential the parent holds
   and passes; it is not a secret from the child's perspective — the child
   exists to receive it. Rename to `TM_GIT_ASKPASS_VALUE`.

2. `GIT_CONFIG_KEY_0` — a git-standard env var name. Git reads it (paired
   with `GIT_CONFIG_VALUE_0` under `GIT_CONFIG_COUNT`). `KEY` between `_`
   delimiters matches the regex. The strip's taxonomy must distinguish
   semantic credential names (`GITHUB_TOKEN`, `API_SECRET`) from git
   config-surface names (`GIT_CONFIG_KEY_0`).

## Default vs extra_config scope (amendment 2026-10-24)

Correction to the earlier finding, from the W4 proposal gate:

- Default `build_git_auth_env` emits **four** keys: GIT_ASKPASS,
  GIT_TERMINAL_PROMPT, GIT_CONFIG_COUNT, TM_GIT_ASKPASS_SECRET.
- A fifth, GIT_CONFIG_KEY_0, appears **only when a caller passes
  extra_config**.

So the collision is "one of four refused for default callers" not
"two of six." The finding stands — `TM_GIT_ASKPASS_SECRET` is refused
for every caller, and that is the load-bearing key. `GIT_CONFIG_KEY_0`
is refused only for extra_config callers. Both refusals are real;
the counts were off by one.

## Four shapes, one recommended

- **Shape 1:** widen `AUTHORIZED_VALUES[grain]` to include the credential
  env value names. Wrong layer — the strip is about env-var semantics, not
  grain.
- **Shape 2:** remove `SECRET` and `KEY` from the regex. Reopens the class
  the regex prevents.
- **Shape 3:** add git-standard keys to an allowlist the strip ignores.
  First allowlist in the strip = first exception. Slippery.
- **Shape 4 (recommended):** rename the builder key + refine regex
  segment-matching. `TM_GIT_ASKPASS_SECRET` → `TM_GIT_ASKPASS_VALUE`.
  For `GIT_CONFIG_KEY_0`: `KEY`/`SECRET` must match as standalone segment
  or suffix, not any `_`-delimited segment. `GIT_CONFIG_KEY_0` has `_0`
  after `KEY` (segment-prefix) → allowed. `API_KEY` still matches
  (standalone suffix). `TM_GIT_ASKPASS_SECRET` still matches (standalone
  suffix). `GIT_CONFIG_KEY_0` stops matching.

## Two-direction test corpus — MANDATORY for any future regex edit

Any change to `CREDENTIAL_SHAPED` needs both directions:

- **Over-match tests** — `PATH`, `PYTHONPATH`, `HOMEPATH`, `PATHEXT`,
  `KEYBOARD_LAYOUT`, `TOKENIZERS_PARALLELISM`, `MONKEY` must pass the strip
  (not stripped). The W3 PATH bug is what over-match looks like.
- **Under-match tests** — `APIKEY`, `GITHUBTOKEN` (no separator) must be
  stripped. The F8 residual is what under-match looks like.
- **Cross-direction** — `GIT_CONFIG_KEY_0`, `TM_GIT_ASKPASS_SECRET`,
  `TM_GIT_ASKPASS_VALUE` — the askpass keys, per this ticket.

The corpus is the fix, not the regex.

## The workaround is structurally less guarded (amendment 2026-10-24)

Reframed by the W4 proposal-gate finding, verbatim:

> The only env channel that can carry TM_GIT_ASKPASS_SECRET is the one
> with no credential filter (SandboxedExecution); host_run refuses both
> TM_GIT_ASKPASS_SECRET and GIT_CONFIG_KEY_0. The collision ticket is not
> merely a naming issue: the escaping route is the unguarded one.

This reframes the ticket: the workaround for the collision (route around
host_run via SandboxedExecution) is not a clean fix with a workaround.
It is a route through the less-guarded path. The fix must be in the
regex's segment-matching (Shape 4) — not in which path the caller takes.

## Not fixed in W4

W4-minimal ships push on `SandboxedExecution` with `set_git_environment`
providing the askpass keys via `agent_config["git_environment"]`. This
ticket's fix lands in a wave that migrates `_exec_host_raw` to `host_run`.

## Sibling

`hygiene/credential-injection-incompatible-as-shipped` — the state ticket.

## Filed

2026-10-24, Gen 49, from the W4 proposal-gate finding, ruled by Main Gen 39.
