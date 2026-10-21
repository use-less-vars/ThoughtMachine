# Ticket: bug/ws-commands-unauthenticated

**DO NOT FIX IN THIS WAVE.** Filed as a pointer only.

**Filed:** 2026-10-21, noticed while enumerating the `/ws` command dispatch after the CORS and `set_project` reads at `9d5ed869`.
**Severity:** HIGH — 27 commands, 16 of them mutation-capable, behind a handshake that accepts unconditionally; and WebSocket handshakes are not covered by the CORS policy, so no browser-level control applies either.
**Status:** open; roster enumerated by read at `9d5ed869`, counts are of `command ==` branches, not of reachable states.

## Finding

`/ws` accepts first and asks nothing afterwards (`web_ui/backend/server.py:1125-1127`):

```python
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, project: Optional[str] = None):
    await ws.accept()
```

There is no token, no cookie check, no `Origin`/`Referer` validation, no session binding, and no per-command authorisation anywhere in the dispatch loop (`:1201-2987`). The only inputs that gate a command are its own argument checks — `request_id` non-empty, `session_id` non-empty, `project` is a directory — which are shape checks, not authority checks.

The distinction that matters against `ticket-bug-unauthenticated-routes-beyond-health.md` is that this is not "another unauthenticated route". A route is a function of request arguments; this socket is a *stateful handle*: it owns a `WebAgentBridge` (created at `:1144` in `set_project`, similarly at connect), it can spawn an agent session and drive it, it can attach to sessions it did not create, and it can answer security prompts (see `ticket-bug-ws-security-response-allows-anonymous-escalation.md`). One connect buys persistent control of the machine's agent, not one request. The CORS policy in `web_ui/backend/server.py:1106-1112` does not enter into it at all: browser same-origin rules exempt `ws://` handshakes, so a page on any origin can open this socket and start issuing commands — the `Origin` header the browser *does* send is never read. That is the cross-site WebSocket hijacking shape, minus even the usual mitigation.

The mutation set below is what a connected-but-untrusted client can change: configuration including the global default, provider profiles, session lifecycle and contents, the workspace root (see `ticket-bug-ws-set-project-bypasses-vault-confinement.md`), workspace directories, container images, and permission grants. Persistence is the theme — `apply_config`, `set_default_config`, `save_provider`, `delete_provider`, `save_session`, `delete_session`, `rename_session`, `bootstrap_workspace`, `rebuild_container` all write to disk or Docker, so the effects outlive the connection.

## Command roster (`web_ui/backend/server.py`, HEAD `9d5ed869`)

16 mutation-capable, marked **M**; 8 reads marked **R**; 3 state-only, unmarked.

- **M** `start_session` `:1206` — spawns a session and drives the agent.
- **M** `continue_session` `:1250` — continues the session with new input.
- — `pause_session` `:1333` — state only.
- — `resume_session` `:1338` — state only.
- — `stop_session` `:1343` — state only (kills the run).
- **R** `get_config` `:1348` — effective config, permissions, workspace identity.
- **R** `get_conversation` `:1370` — full conversation history.
- **M** `apply_config` `:1381` — persists config; `workspace_path` switches the workspace root at `:1455`.
- **M** `set_default_config` `:1785` — writes the **global** default config.
- **R** `get_providers` `:1866` — provider profiles ("safe fields only").
- **M** `save_provider` `:1908` — adds/updates a provider profile (credential-bearing).
- **M** `delete_provider` `:1966` — deletes a provider profile.
- **R** `get_available_tools` `:2016` — tool inventory for a mode.
- **R** `list_sessions` `:2049` — enumerates every session in the store.
- **M** `save_session` `:2071` — writes the current session.
- **M** `load_session` `:2102` — attaches to an arbitrary `session_id`; no ownership check.
- **R** `load_more_messages` `:2359` — pages another session's history.
- **M** `delete_session` `:2378` — destructive.
- **M** `rename_session` `:2409` — writes.
- **R** `get_open_sessions` `:2477` — session metadata batch.
- **M** `close_session` `:2499` — closes sessions by id.
- **M** `new_session` `:2552` — tears down the bridge and creates a session.
- **M** `set_project` `:2706` — changes the workspace root; `register_by_root` at `:2766`.
- **M** `security_response` `:2887` — grants/denies a pending permission prompt, with `remember` persistence.
- **R** `get_workspace_capabilities` `:2921` — returns unrestricted `WorkspaceCapabilities.default()` when no file exists (`:2936`).
- **M** `bootstrap_workspace` `:2949` — `ensure_workspace_dirs(workspace_id)` creates directories.
- **M** `rebuild_container` `:2971` — invokes `infra.containers.executor.rebuild_container` on a client-supplied workspace path.

Every entry above was read at `9d5ed869`; line numbers drift, resolve by command name. The three state-only branches (`pause`/`resume`/`stop`) are counted as non-mutating because they do not persist, but they are still unauthorised control of a running agent.

## What this is not

- Not a duplicate of `ticket-bug-unauthenticated-routes-beyond-health.md` — that ticket maps the HTTP surface and names `/ws` as one bullet in a list; this is the socket's actual authority, enumerated.
- Not a duplicate of `ticket-bug-ws-set-project-bypasses-vault-confinement.md` or `ticket-bug-ws-security-response-allows-anonymous-escalation.md` — those are two consequences that deserve their own decisions; this is the class.
- Not a claim that all 27 branches are equally dangerous, or that every one is reachable from a hostile page without further argument. `rebuild_container` needs a path that resolves to a workspace; `save_provider` needs credentials to be interesting. The point is that none of them check who is asking.
- Not a claim about remote hosts. The default bind is loopback; the reachable attacker is a local process or the user's own browser.

## Disposition

Decide whether "the Web UI process is running" is supposed to mean "the agent is open to anything that can open a socket". If not, the socket needs the same primitive the HTTP side needs — an authenticated identity, checked once at accept and carried into the dispatch loop, with `Origin` validation as the cheap first step for the browser case. That is a design decision above this ticket; the alternative (per-command allowlisting) will drift, as `apply_config` already demonstrates by growing a workspace-switch branch that duplicates `set_project`.

Cross-reference: F30, `ticket-bug-unauthenticated-routes-beyond-health.md`, `ticket-bug-cors-wildcard-exposes-api-to-browser-origin.md`, `ticket-bug-ws-set-project-bypasses-vault-confinement.md`, `ticket-bug-ws-security-response-allows-anonymous-escalation.md`.
