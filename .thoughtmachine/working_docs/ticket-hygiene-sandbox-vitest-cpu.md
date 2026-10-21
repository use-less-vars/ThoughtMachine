# Ticket: hygiene/sandbox-vitest-cpu

**Filed:** Gen 45, 2026-10-17, from the P0 fixes wave.
**Severity:** low — worker sandbox quirk, not a code issue.
**Status:** filed.

## Finding

The worker sandbox container runs at 0.5 CPU while `nproc` reports 12.
Vitest's default fork pool sees 12 workers but only has half a core; ~113
phantom timeout failures result.

## Remedy

Always use `npx vitest run --maxWorkers=2` in the worker sandbox. Or
increase `cpu_quota` in the worker's container spec (400000 ≈ 4 CPUs).

## Why it matters

This ate real worker time during the wave. Every frontend test run in
the sandbox should carry `--maxWorkers=2` by default.
