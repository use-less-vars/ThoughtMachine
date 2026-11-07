# design/auth-layer — a name for the decision, and its preconditions

**Category:** `design`
**Status:** open — placeholder. Not dispatched. Do not start this.
**Dispatched:** after the client-claim red team, which runs after the security filings of Gen 49
**Filed:** 2026-10-24, Gen 49, ruled by Main Gen 39

## 1. What this ticket is

A name for a decision, and the preconditions that must hold before the decision can be made. It contains no design. That is deliberate and is the whole point of filing it: the decision is real, it is coming, and it must not be made early by whoever happens to be holding the keyboard.

## 2. The two ruled options

Two options are already ruled as the live candidates. They are not alternatives to be weighed now; they are the shape of the decision to be taken later.

**(a) A token per router entry.** A bearer or session token minted server-side at startup, one per FastAPI router entry, written to somewhere only the operator's browser can read, and injected into requests by the frontend. This makes every entry point authenticated rather than assumed-trusted, and it costs a minting step, a storage decision and a frontend change.

**(b) Formalise localhost-only.** Keep the existing assumption — localhost is trusted, the peer on the loopback interface is the operator — and make it explicit by adding a refusal test at every entry point that fails when the peer is not loopback. The assumption already governs the design; today it governs it silently. This option does not add authentication, it pins the assumption so that a future change which breaks it fails a test rather than a review.

## 3. Prerequisites

Two, both already satisfied or ruled before this ticket may be dispatched.

- **The client-claim red team must have run.** `bug/workspace-id-validation` is filed and verified on disk, which is the precondition the red team was waiting on. Its findings decide which option in section 2 applies to which surface. This ticket cannot choose the shape before those findings exist, because the findings are the input that distinguishes the surfaces.
- **`--allow-remote-bind` must be ruled.** It is ruled: keep the flag, default it off, and correct the documentation that describes it. That ruling is a prerequisite rather than a detail because option (b) is incoherent while a remote bind is reachable by configuration.

## 4. Do not pre-decide the shape

No design content belongs in this file. Not a diagram, not a schema, not a recommendation, not a preference, not a 'probably (a) for the API and (b) for the UI'. A seat that opens this ticket early and finds a preferred answer will implement the preferred answer, and the red team will then be reading a system that already has its conclusion. The correct state of this file until dispatch is exactly what it is now.

## 5. Not in scope

No code. No tests. No changes to the security gate, the permission layer or the container boundary. The auth layer sits above all four boundaries named in `security_architecture.md` and touching any of them from here would be the collapse that section forbids.

## 6. Dispatch condition

The client-claim red team has reported, and its findings have been read. Until then this ticket is a bookmark.
