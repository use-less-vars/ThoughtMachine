# feat/kill-switch-visible — a security control in a config tab does not exist under load

**Category:** `feat`
**Status:** open — operator-requested
**Nature:** a UI deliverable that is a security deliverable first
**Filed:** 2026-10-24, Gen 49, ruled by Main Gen 39

## 1. The observation

A stop control that lives in a settings tab is not a stop control. Under the conditions in which someone needs to stop the machine — something is running that should not be, and the operator wants it to stop now — the operator is not navigating to a config panel, opening an accordion, and looking for a toggle. If the control is nested, it is not there. Availability is the property that makes it a control; everything else about it is secondary.

## 2. Scope

- The button lives on the **main session screen**, always visible, not nested inside a tab, accordion, modal or drill-down.
- A **single button** that stops all host-side tool execution.
- It stops the **host-execution surface**, not all sessions. The scope is deliberately narrower than 'stop everything': a kill switch whose blast radius is unclear will not be pressed, and an unpressed kill switch is decoration.
- Three states, visible: **enabled**, **fired**, **reset**. A fired switch that looks identical to an enabled one is a state the operator cannot read.
- Firing writes an event to `event_log`. The event is what makes the press auditable and what lets a later reader reconstruct why a session stopped producing output.
- It bypasses per-tool gating entirely. It does not consult `required_categories`, does not consult the permission layer, and does not ask whether the tool in flight was granted.

## 3. Why it must sit at the executor level

The category gate is the thing that fails. `hygiene/tool-import-declaration-match` records two tools in this tree whose reach the gate cannot see, because the gate reads a declaration and the tool exercises an import. A kill switch implemented as another consumer of the same declarations inherits the same blindness and fails at exactly the moment it is needed.

The stop must therefore sit below the declaration layer, at `agent/core/tool_executor.py`, where every tool call actually passes. That is the fourth boundary named in `security_architecture.md:13-20`, tool-call dispatch, and it is the only place a stop can be enforced without trusting a promise made elsewhere.

It does **not** touch the container boundary. Stopping host-side tool execution is not the same as tearing down containers, and conflating the two would give the operator a button with a side effect they did not consent to. Containers are managed by their own surface.

## 4. Timing

Filed now, because naming it is cheap and forgetting it is not. Dispatched after the client-claim red team **and** after Credential W3 and W4 — that is, in the post-security-hardening UI wave. Sequencing matters here: a visible stop button added before the hardenings it is meant to backstop would offer reassurance about a system whose reach is still being measured.

## 5. Not dispatching

This ticket does not schedule the work. It does not assign an owner, does not estimate, does not sketch a component, and does not name a framework or a route. Naming the deliverable is the work this ticket does; building it belongs to the wave that comes after the security work lands.
