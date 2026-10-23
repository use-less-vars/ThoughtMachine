# design/tool-resource-contract

## Cluster

This ticket is one of three passes over the **same** problem, taken from three angles:

- **WHAT runs** — a *Resource*: a typed envelope, no shell. This file, `ticket-design-tool-resource-contract.md`.
- **HOW it returns** — a *Job*: a handle, not a result. `ticket-design-worker-job-lifecycle.md`.
- **WHO calls it** — a *Role*: plan-shaped Control verbs. `ticket-design-agent-role-control.md`.

The convergence requirement is explicit and binding: these three passes must land on **one** handle/state/timeout model, not three parallel ones. A Resource without the Job's handle contract still blocks the caller; a Job model that does not type its payload still admits a shell; a Role split that invents its own control vocabulary reintroduces the same tangle at the agent layer. Each pass must be reviewable against the other two, and any divergence must be resolved *into a single model* rather than papered over per layer.

## Origin

The driver is a concrete project, not an abstraction. An **EE project** requires a **C++ solver** to run on the host: it is a compiled binary, it needs real CPU and real files, and it produces artefacts on disk. It cannot run through `host_bash` — `host_bash` takes a command line, and a command line is exactly the wrong interface for this: the arguments are structured data, the correctness of the call depends on the shape of that data, and shell semantics (quoting, word splitting, metacharacters) sit between the caller's intent and the binary's actual behaviour. What the solver needs is a **typed envelope**: a job spec that is validated before anything executes.

**KiCad** (EDA/PCB) is the second instance of the same shape, which is what makes this a pattern rather than a one-off. An **FIT solver** is the first and earliest driver — it is where the shape was first felt, before it had a name.

## The pattern

Every tool that "runs on the host but is not `host_bash`" becomes a **Resource**. The test is not "does it execute something on the host" — many things do. The test is whether the call can be expressed as a typed request against a declared binary with declared capabilities, and whether the caller should never be handed shell semantics at all. When that is true, "a tool that shells out" is the wrong container for it: it makes every call an instance of the general string-interpolation problem, and it makes the permission story a story about a shell rather than about a capability.

## Five facts

### 1. Existence

A Resource declares that it exists before anyone calls it: the **binary path**, the **version**, and its **declared capabilities**. Existence is discoverable and cheap to check, so a missing or wrong-versioned binary fails as a configuration fact — not as a mysterious mid-run error, and not as a zero-exit-code run that silently produced nothing. Declared capabilities are what the caller is allowed to ask for; anything outside them is not a runtime failure to be handled hopefully, it is a request that should not have been accepted.

### 2. Envelope

The call is a **typed job spec**, serialised as **JSON on stdin**, with **no shell** anywhere in the path. This is the fact that does the security work. It is what makes the RCE surface narrow: there is no command string to interpolate into, no quoting rule to get wrong, no `$(...)`, no argument that becomes two arguments, no metacharacter that a string comparison in a gate does not see. The spec is validated as data and the binary is invoked as an argv list against a fixed, declared executable. Every argument the caller can influence is a field in a structure with a known type, and every field the caller cannot influence is simply not in the structure.

### 3. Lifecycle

A submission has a state machine, and the state machine is short and public: **submit → run → done / failed / timed-out / cancelled**. Four terminal states, each named. "Done" is not the only success shape and "not done" is not one undifferentiated failure: a timed-out run and an errored run are different facts, and the caller must be able to tell them apart without reading logs. This is where the Resource contract meets the Job contract in `ticket-design-worker-job-lifecycle.md`: these states are the Job's states, and they must be spelled the same way in both tickets.

### 4. Control

The verb set is **submit, cancel, read result** — and it is deliberately closed. **There is no shell verb.** Not a disabled one, not a gated one, not an escape hatch: the contract has no verb that takes a command line, because the moment such a verb exists, the typed envelope is decorative — every caller under time pressure routes around the envelope and the RCE surface is back at full width. Cancellation is a first-class verb rather than killing a process group from outside, and reading the result is a first-class verb rather than scraping stdout, because stdout is not a result.

### 5. Observability

Three channels, each with one job. **Progress on stderr** — human-readable, cheap, unbounded in principle, never parsed for correctness. **The result written to an output directory** — the artefact, addressable, survive-the-call, the thing the caller actually wanted. **An event emitted** — the machine-readable signal that the run reached a state, so the caller (and anything observing the session) learns *what happened* without polling the filesystem and guessing. Splitting these three keeps each one honest: progress chatter cannot corrupt the result, and the event does not have to carry the payload.

## The shape

The thing being built is a **gated typed request**, not a **command line**. That distinction is the whole design, and it buys four concrete things:

1. **A narrow RCE surface.** Validation happens on a structure, not on a string. There is nothing to escape out of.
2. **A permission story about capability, not about command text.** The gate can reason about *which resource, which declared capability* instead of pattern-matching a command line and hoping the pattern is complete.
3. **Errors that name the layer.** A malformed request is rejected before execution; a refused capability is refused at the gate; a failed run is a lifecycle state. Three different failures, three different messages, none of them "exit 1, look at stderr".
4. **Composability.** A typed request can be constructed, logged, replayed and asserted on by a test. A command line can only be diffed as a string.

## The generalization

Every future **host-resource** — a **hardware probe**, a **license-locked binary**, a **benchmark harness** — follows this same five-fact contract: declared existence, typed envelope, four-state lifecycle, the closed control verb set, three observability channels. Nothing about the contract is EE-specific; the EE C++ solver is simply where it is being paid for first, with KiCad as the confirming second case and the FIT solver as the historical first.

State this plainly, because the confusion is available and expensive: **Resource is NOT a synonym for `host_bash`.** `host_bash` is one specific, deliberately narrow, deliberately gated capability to run a shell on the host. A Resource is the general shape for "a host-side capability with a typed interface". Collapsing the two would either make `host_bash` look like the recommended path for structured work (it is not) or make every Resource inherit the shell's threat model (it must not).

## Filed

Filed 2026-10-23 by the main agent at the operator's directive. Base: dev @ bc8c7700.
