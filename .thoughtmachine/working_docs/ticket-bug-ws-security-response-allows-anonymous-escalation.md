# Ticket: bug/ws-security-response-allows-anonymous-escalation

**DO NOT FIX IN THIS WAVE.** Filed as a pointer only.

**Filed:** 2026-10-21, noticed while reading the `security_response` branch during the WebSocket command roster pass at `9d5ed869`.
**Severity:** HIGH — the human-consent gate for tool permissions can be satisfied by the same unauthenticated client that triggered the prompt, and `remember: true` makes the widening permanent.
**Status:** open; verified by read. The narrow version of the claim ("anyone can approve anything") is partly refuted below — request ids are UUIDv4 — but the gate is self-service, which is worse than weak authentication.

## Finding

The handler is twelve lines with no identity in them (`web_ui/backend/server.py:2887-2905`):

```python
elif command == "security_response":
    """Handle user response to a security prompt."""
    request_id = msg.get("request_id", "")
    approved = msg.get("approved", False)
    remember = msg.get("remember", False)

    if not request_id:
        ... continue

    try:
        from thoughtmachine.security import resolve_security_prompt
        resolve_security_prompt(request_id, approved, remember)
```

Nothing in that branch asks who is answering. There is no check that the connection owns the session that raised the prompt, no `key=id(ws)` binding of the prompt to the socket that received it, no session comparison of the kind `bridge.py:392` uses when *delivering* the prompt, and no record of which connection was asked. `resolve_security_prompt` (`thoughtmachine/security.py:278-290`) then pops the id out of the process-global `_pending_security_requests` dict (`:48`) and puts the verdict on that queue — so resolution is scoped to the process, not to the connection or the session that asked.

Refutation first, because it constrains the claim: request ids are `str(uuid.uuid4())` (`thoughtmachine/security.py:924`), so blind forgery is not practical, and prompts are only forwarded to bridges whose `session_id` matches (`web_ui/backend/bridge.py:392`), so a random second tab does not passively see another session's prompts. The escalations that survive are these. (a) **Self-approval.** Any client can `start_session` and then answer its own `security_prompt` with `approved: true, remember: true`; the dialog that exists to require a human is answered by the party asking. If the threat model is "a local process or a hijacked page can drive this socket" — which is precisely the threat model of `ticket-bug-ws-commands-unauthenticated.md` — then the consent gate offers no resistance to it at all. (b) **Cross-session resolution.** `load_session` (`web_ui/backend/server.py:2102`) accepts any `session_id` with no ownership check, so a second connection can attach to a session it does not own, receive that session's `security_prompt` events, and answer them — including with `remember: true`. (c) **Persistence.** `remember` is not a UI hint: at `security.py:1000-1006` it writes the decision into the security configuration, so a single approved-and-remembered answer permanently converts an `ask` policy into an `allow` one.

## Candidate evidence

- `web_ui/backend/server.py:2887-2905` — the whole handler; no authorisation, no connection binding.
- `thoughtmachine/security.py:48` — `_pending_security_requests` is module-global, keyed by request id only.
- `thoughtmachine/security.py:278-290` — `resolve_security_prompt` resolves whatever id it is handed.
- `thoughtmachine/security.py:924,928-929` — ids are UUIDv4 and stored in that global dict (limits blind forgery).
- `thoughtmachine/security.py:1000-1006` — `remember` updates security config.
- `web_ui/backend/bridge.py:392` — delivery is session-filtered; resolution is not.
- `web_ui/backend/server.py:2102` — `load_session` takes an arbitrary `session_id` with no ownership check.
- `web_ui/backend/server.py:1125-1127` — the socket is accepted unconditionally, so "a client" means "anyone who can open a socket".

## What this is not

- Not a claim that an attacker can approve a prompt it has never seen: ids are UUIDv4, and delivery is filtered by session id. Blind forgery is out; self-approval and cross-session attachment are in.
- Not a duplicate of `ticket-bug-ws-commands-unauthenticated.md` — that ticket is the class ("no command is authenticated"); this one is the specific consequence that the permission-escalation gate is among the commands, and that its answer is indistinguishable from the human's.
- Not a claim that the dialog is useless. It is a real check against a model that goes rogue inside its own turn; it is not a check against a client, because clients are unauthenticated and can answer for themselves.
- Not an assertion that `remember: true` is exploitable without a prompt — it needs a live pending id, which the same client can create.

## Disposition

Above this ticket: decide whether the approver of a security prompt is a *connection* (then bind the prompt to `id(ws)` at issue time and reject answers from any other socket) or a *process-level operator* (then it needs authentication, which does not exist). Until that is decided, the honest reading is that `approve` carries exactly as much authority as `start_session` — which is to say, whatever the transport grants, and the transport grants everything.

Cross-reference: F30, `ticket-bug-unauthenticated-routes-beyond-health.md`, `ticket-bug-ws-commands-unauthenticated.md`, `ticket-bug-ws-set-project-bypasses-vault-confinement.md`.
