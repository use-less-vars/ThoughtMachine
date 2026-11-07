# Ticket: hygiene/container-image-size-compiler-overhead

**Filed:** Gen 47, 2026-10-21
**Source:** gen-47 pen-test run — finding relayed by the operator (F16).
**Severity:** low — decision input, not a defect.
**Status:** filed, not dispatched.
**Report cross-ref:** There is no report on this filesystem containing F11–F22. The finding was relayed by the operator from the gen-47 pen-test run; the cited report is not on this disk, so no report line numbers are citable.

## Finding

Adding `gcc libc6-dev make` to the vault Dockerfile so the runtime can compile costs roughly **+195 MB per image** (`agent-executor:latest` now ~723 MB). Every executor image and every resource overlay built on that base inherits it.

## Why it is a ticket and not a fix

Nobody has decided whether a compile-capable runtime is the intended product. If it is, the size is the price and this closes as accepted. If it is not, the compiler belongs in an opt-in image variant. Record the number so the decision has a number attached.

## Bearing

Interacts with `ticket-bug-resource-overlay-not-rebuilt-with-runtime.md` — the overlay images are the ones that silently missed the change, so the size delta currently applies unevenly across the fleet: some images carry the compiler, some do not, and the operator cannot see which.
