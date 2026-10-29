# You Live in the Workspace Image, Not on the Host

This document states a single hard rule, explains why it is a rule rather than a preference, and records the evidence from a real workspace that made the rule necessary.

It is addressed to whoever writes the system prompt, the workspace configuration, or the bootstrap script for a ThoughtMachine agent.

---

## The rule

> **You live in the Workspace Image, not on the host.**
>
> Host Bash is strictly for bootstrapping (building the image with `docker build`). **Turn it off once the image is built.**

Two clauses, and the second one is the one that gets skipped. Disabling host Bash is not a follow-up task and not a hardening nicety to be scheduled later. It is the second half of the bootstrap, and the bootstrap is not finished until it is done.

## Why this is a rule and not a preference

An agent that has two places to run a command will, under pressure, run it in whichever place answers first. That is not a character flaw; it is correct behaviour for a system optimising for task completion. Therefore the only way to make the contract *"all work happens inside the image"* enforceable is to make the alternative unavailable. A contract that depends on the agent choosing the disciplined option while a faster undisciplined option sits on the same machine is an aspiration, not a contract.

The consequence is stated plainly because it is counter-intuitive: **removing a working capability makes the system more reliable, not less.** The removed capability does not disappear from the task; it reappears, correctly, as an image change.

## The two planes

| Plane | What it is for | What may run there |
| --- | --- | --- |
| **Host** | Image lifecycle only | `docker build`, tagging, pushing the image, and `docker compose` up/down for the agent's own container. Nothing else. |
| **Workspace Image** | All of the actual work | Reading, writing, searching, building, linting, testing, and reporting. Every command whose result is supposed to mean something. |

If a command's *output* is evidence -- a test result, a build artefact, a file's contents -- it must come from the image. Host output is never evidence about the workspace, because the host is not the workspace.

## Why leaving host Bash on teaches the wrong reflex

This is the part that is easy to underestimate. The damage is not that a host command might fail. The damage is what happens *after* it fails.

1. The agent runs a test in the image. It fails for a real reason: a missing binary, a memory limit, a wrong `TMPDIR`.
2. Instead of fixing the image, the agent retries on the host, where the tool happens to exist and memory is plentiful. It passes.
3. The agent reports a green.

That green proves nothing about the workspace. It proves something about the developer's laptop. The bug the image would have exposed -- the missing binary, the OOM at 512 MiB, the `TMPDIR` that does not exist -- is still there, now hidden behind a passing test. Worse, the failure was *informative*, and the workaround destroyed the information.

The correct response to "the image cannot run this" is always the same: **change the image.** Add the binary in the Dockerfile. Raise the memory limit. Set the environment variable in the image, not in a shell prefix the next agent will forget. Then run it again in the image, so the green is a fact about the workspace.

## Field evidence

These are measured observations from a live workspace, not hypotheticals. They are listed because a rule with evidence behind it survives review by a sceptic, and this document is meant to survive review.

- **Host Bash is `banned` in the effective permissions**, enforced at the tool layer rather than by convention. The agent cannot reach the host shell at all, so the discipline holds even when the agent is tired, rushed or wrong.
- **Egress is blocked.** A connectivity probe returns `EGRESS_BLOCKED` with a DNS resolution failure. Anything that reaches for the network dies at the permission layer before it reaches the interesting part of the code, which is a far cheaper and clearer failure than a hung download.
- **Even git writes are refused at the permission layer.** `git push` from inside the workspace returns `Permission denied: Tool requires network:outbound, but session allows network:banned`. The agent can commit locally; it cannot publish. Publishing is a host-side, human-gated act, and the agent must report *"committed, push blocked"* rather than implying the branch went out.
- **The image does not have the full toolbox, and that is on purpose.** It has no `git` binary. It has no Docker daemon. It has no `xxd`. Each absence is a decision about the image, not a fault to be routed around.
- **The container shell is dash-like, not Bash.** `${PIPESTATUS[0]}` is simply unset there, so a pipeline's exit status silently vanishes. The portable idiom is `cmd ; echo "EXIT=$?"`.
- **The memory limit is 512 MiB.** A parallel test runner forked enough workers to exceed it and was OOM-killed with exit code 137. The fix was not "run the tests on the host"; it was `fileParallelism: false` in the runner config, so the suite runs serially inside the limit and passes there. Serial-in-the-image beats parallel-on-the-host every time.
- **`TMPDIR` mattered.** The container defaulted to `TMPDIR=/var/tmp`, and the frontend test runner died with `ENOENT mkdir '/var/tmp/<random>/web'` before collecting a single test. With `TMPDIR=/tmp` it runs clean. This is exactly the class of bug the image is supposed to surface -- which is why it must be surfaced there and fixed there.
- **The suite runs fully offline from the vendored tree.** `node v20.19.2`, `npm 9.2.0`, `vitest 3.2.7`, 234 entries in `node_modules`, no install step: 52 files, 681 tests, 681 passed, 0 failed, exit 0, 240.30 s. Real execution inside the image, reproducible on a CI runner, with no host anywhere in the loop.

## Decision table

When you need something, get it from the right place the first time.

| I need to... | Where it comes from |
| --- | --- |
| Build or rebuild the image | Host, `docker build`. This is the one legitimate host command. |
| Add a tool the image lacks | The Dockerfile. Then rebuild. Never reach for the host. |
| Raise a limit (memory, file handles, `TMPDIR`) | The image's runtime configuration. Then rerun. |
| Run a test, build or linter | Inside the image. Always. |
| Read or modify workspace files | Inside the image, via a tool. |
| Inspect git history | Inside the image, via a git-reading tool -- not a shell command, since there is no `git`. |
| Commit locally | Inside the image, via the git-write tool, on a feature branch. |
| Push a branch or open a PR | Cannot be done from inside the image. Report it as blocked and hand it to a human. |
| Fetch a dependency from the internet | It cannot be fetched at runtime. Bake it into the image or vendor it. |
| Prove something works | Run it in the image and quote the exit code and the numbers. |

## If the image lacks a tool

This is the load-bearing case, so it gets its own section.

The wrong move is to reach around the image. The right move is a two-step change:

1. Add the package to the image definition. One line, reviewed, committed.
2. Rebuild, and rerun the command in the image.

Yes, that is slower than a host shell that already has the binary. That is the point. The slowness is the review, and it is the reason the next agent inherits an image that works. Raising the cost of the shortcut is how the contract is kept under pressure -- which is the entire argument of this document, applied at the smallest scale.

## Bootstrap checklist

When standing up a new workspace, in this order:

1. Write the image definition with everything the agent needs: language runtimes, package managers, test runners, search tools, and the limits and environment variables the workload requires.
2. Build the image on the host: `docker build`.
3. Do a smoke run **inside** the image: interpreter version, package manager version, the actual test command, the actual search tool. Quote the outputs.
4. **Turn host Bash off.** Now the bootstrap is finished.
5. Record what the image does and does not contain -- this document is the precedent.

Step 4 is not optional and not deferrable. An image that is finished but leaves host Bash on is a bootstrap that has not finished, and the evidence above is what happens next.

## Failure modes this prevents

| Failure mode | Symptom without the rule | Symptom with the rule |
| --- | --- | --- |
| Silent environment drift | Tests pass on the host, fail in CI, or the reverse | Tests pass or fail in the one place that counts |
| Phantom green | A result the image could never reproduce | A result the image can reproduce by construction |
| Lost diagnostic signal | The informative failure is routed around | The failure is fixed where it was found |
| Unreproducible state | A file or dependency only the host has | Everything the agent used is in the image |
| Missing tool at the worst moment | Discovered mid-task, worked around permanently | Discovered at bootstrap, fixed in the Dockerfile |

## The one-sentence version

If a command's result is going to be trusted, it has to have run where the work actually lives -- and the only way to guarantee that is to remove the other place.
