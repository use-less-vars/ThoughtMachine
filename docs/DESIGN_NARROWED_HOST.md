# DESIGN — a narrowed host execution surface

## 1. Status and scope

Design note only. **Nothing here is implemented** and there is no patch — parameter names, an allowlist schema, a table of refusal messages and the laws governing them, so a replacement can be reviewed before code exists.

Today's control is one category at one level: `HostBashTool` declares `required_categories: ClassVar[List[str]] = ["host_bash:allow"]` (`DISK tools/host_bash_tool.py:89`) and the per-grain vocabulary for that grain is `banned|ask|allow` (`DISK security/resource_catalog.py:39-47`). The grant the user makes is "this workspace may run anything, or nothing" — nowhere in the schema can they say *which command*, *in which path*, *with which credentials*.

Consumers: **step 5** (two-axis permissions), **step 7** (workspace-scoped file tool), **step 9** (credentials bound per resource), **step 11** (resource/egress envelope). §10 states what each is handed.

Evidence base: `.scratch/host-primitives.md` (12,825 B). Every measurement cites the layer it was taken at. Two were re-taken on the **host** by the operator on 2026-11-27 because the container could not see them, and those two decide the design.

## 2. The primitive landscape (measured)

| capability | measured value | root | layer |
|---|---|---|---|
| kernel release | `7.0.0-43-generic` | PROC `/proc/version` | host kernel (shared with container) |
| arch | `x86_64` | `uname -m` | host kernel |
| active LSM stack | `lockdown,capability,landlock,yama,apparmor,bpf,ima,evm` — **Landlock is in the stack** | SYS `/sys/kernel/security/lsm` | **host** (operator, 2026-11-27) |
| Landlock ABI | `8` (`landlock_abi_ret=8`, errno 0, syscall 444 NULL/0/flags=VERSION) | PROBE syscall 444 | host kernel |
| Landlock ruleset create | fd=3, errno 0, full `handled_fs_mask=0x7fff` | PROBE | usable |
| Landlock `restrict_self` | rc=0, errno 0 | PROBE | usable at `CapEff 0` |
| Landlock deny-by-default | allowlisted read OK; non-allowlisted `/etc/hostname` → errno 13 EACCES | PROBE | container |
| Landlock partial mask | with only `READ_FILE` set: `/etc` enumeration **LEAKED**, `/tmp` write **SUCCEEDED** | PROBE | container |
| seccomp | `Seccomp: 2` (FILTER), `Seccomp_filters: 1` | PROC `/proc/self/status` | container runtime |
| capabilities | `CapEff: 0000000000000000`, `CapPrm: 0`, `CapBnd: 0`, `NoNewPrivs: 1` | PROC `/proc/self/status` | container runtime |
| cgroup | v2; `cpuset cpu io memory hugetlb pids rdma misc dmem`; mounted `ro,nosuid,nodev,noexec` | SYS `/sys/fs/cgroup/`, PROC `/proc/self/mountinfo` | host kernel / runtime |
| userns sysctls | `max_user_namespaces 55323`, `unprivileged_userns_clone 1` | PROC `/proc/sys/...` | host sysctl |
| unprivileged userns, actually usable | **DENIED** — `unshare: write failed /proc/self/uid_map: Operation not permitted` | HOST `unshare -Ur true` | **host** (operator, 2026-11-27) |
| AppArmor | enabled `Y`; live profile `docker-default (enforce)` | SYS `/sys/module/apparmor/parameters/enabled`, PROC `/proc/self/attr/current` | host kernel / runtime |
| helper binaries | `bwrap`, `firejail`, `nsjail`, `systemd-run`, `capsh` **ABSENT**; `unshare`, `setpriv`, `prlimit`, `chroot` present (util-linux `2.41.5`) | container rootfs | **container image — not host truth** |
| python | `3.12.15` | PROBE | container image |

Two layer facts govern how the rest of this note reads. **First: the container could not see the LSM stack** — inside it `/sys/kernel/security/` is empty, reading `lsm` gives `No such file or directory`, so the evidence file records the list as **UNAVAILABLE** (`SYS`, container), while on the host it is the nine-entry stack above. A control must **name the layer it was measured at**: one answer is silence, the other is the stack, and they are not the same answer. §3 therefore draws conclusions only from rows measured where the question can be answered.

**Second: binary presence is image truth, not host truth.** `bwrap` missing from the container rootfs says nothing about the host. This design is justified by "namespaces are denied" — measured on the host, a kernel/policy fact — not by "we don't have bwrap", a packaging accident.

## 3. Q1 — what the OS actually gives us

**Landlock — the answer.**
- Enforces filesystem access per-path and per-operation on a process and its descendants; ABI `8` (`PROBE` syscall 444) and present in the active stack (`SYS /sys/kernel/security/lsm`, host).
- Usable unprivileged: ruleset + `restrict_self` rc=0 at `CapEff 0000000000000000`, `NoNewPrivs: 1` (`PROBE`, `PROC /proc/self/status`) — **no capability, no setuid helper, no namespace required**.
- Cannot see a command, a credential, an environment variable or a socket; it sees paths and filesystem operations, and is not a glob engine (§4.2).
- **Verdict: the only primitive on this machine that is unprivileged, per-process and path-scoped. It is the mechanism.**

**seccomp — available in principle, unmeasured here.**
- Syscall filter on a process and its descendants; a child can install one on itself before `execve`, unprivileged — the same fork-restrict-exec shape as Landlock, so no helper either.
- Cannot see paths: it sees `openat`, not `/home/jojo/secrets`.
- **Verdict: this note rests no claim on it.** Installation was not probed on this host, so using it begins with a probe; the container's own `Seccomp: 2` (`PROC /proc/self/status`) is the runtime's filter, not a measurement of ours.

**AppArmor — root policy, not agent policy.**
- Path and capability rules per profile; the container runs under `docker-default (enforce)` (`PROC /proc/self/attr/current`).
- Loading or replacing a profile needs privilege the agent does not have and must not be given: an agent that can write its own confinement profile is unconfined.
- **Verdict: a backstop the operator sets, never the grant-level mechanism.**

**Namespaces — ruled out, by measurement.**
- The sysctls permit unprivileged userns (`max_user_namespaces 55323`, `unprivileged_userns_clone 1`, `PROC /proc/sys/...`); the probe contradicted them: `unshare -Ur true` fails `unshare: write failed /proc/self/uid_map: Operation not permitted` (`HOST`, operator 2026-11-27). The sysctl is not the control; the policy is.
- No user namespace ⇒ no mount namespace ⇒ **no bwrap/nsjail/pivot_root path on this machine**; those binaries are absent from the image anyway (container rootfs — moot given the denial).
- Corollary §7 depends on: **without a network namespace we cannot remove network access from the child**, so egress cannot be closed by this design.

**cgroups — accounting, not access control.**
- cgroup v2 present with the expected controllers (`SYS /sys/fs/cgroup/cgroup.controllers`) but mounted `ro,nosuid,nodev,noexec` (`PROC /proc/self/mountinfo`), so the agent cannot create a sub-cgroup to attach a child to.
- Even if it could: a cgroup limits consumption, not which paths a process opens. A path allowlist expressed as a cgroup is not a thing.
- **Verdict: not a candidate for this control.**

## 4. Q2 — the interface

Four options, each with its trade.

**A. `host_exec` installs a Landlock domain in the child before `execve`.** The allowlist becomes kernel state and the child physically cannot open a path outside it. Trade: refusals surface as `EACCES` from inside an unfamiliar program, which reads as a bug to agent and user alike unless we translate it (§4.4).

**B. Python-side path validation only, reusing `validate_path`.** `thoughtmachine/security.py:427` already does `abspath` (`:464`), workspace `realpath` (`:553`), `relpath` containment (`:557`, `:575`) and a **second** containment pass after `realpath` so a symlink escaping the root is caught (`:623`, `:632`, `:661`), plus a component-aware deny list re-checked post-resolution (`:596`, `:686`). Trade: it validates the paths *we* name; it cannot constrain what a program we exec does with its own `openat`. A promise, not a boundary.

**C. Both.** B for the declared surface and for messages a human can read; A for the confinement. **Recommended** — B alone is a promise, A alone produces refusals nobody can act on. B decides whether the call is *admissible* and says why not; A makes admissibility true when the program misbehaves.

**D. Namespace + bwrap.** **RULED OUT by measurement** — `HOST unshare -Ur true` → `Operation not permitted`; no user namespace, therefore no mount namespace, therefore no bwrap. Do not design around it and do not add a fallback that tries it.

### 4.1 Interface sketch

`host_exec(argv, cwd, allowed_read, allowed_write, always_denied, timeout_s, env_passthrough, credential_ref)`

| parameter | type | meaning | refusal if violated |
|---|---|---|---|
| `argv` | `list[str]` | program and arguments; **no shell string form** | `argv_missing` |
| `cwd` | `str` | absolute; must resolve inside `allowed_read` ∪ `allowed_write` | `cwd_outside_grant` |
| `allowed_read` | `list[str]` | absolute paths, **no globs** (§4.2) | `path_not_allowed` |
| `allowed_write` | `list[str]` | absolute paths, no globs; read implies nothing | `path_not_allowed` |
| `always_denied` | `list[str]` | absolute paths subtracted last; **wins over both** | `path_denied` |
| `timeout_s` | `int` | wall clock for the child | `timeout` |
| `env_passthrough` | `list[str]` | **default empty**; nothing inherited implicitly | `env_not_declared` |
| `credential_ref` | `str \| None` | resolved by step 9, never inline | `credential_unbound` |

There is deliberately **no `network` parameter**: §3 measured that network access cannot be removed without a namespace, so such a parameter would be a lie in the interface, and F1 forbids the permissions the user sees from lying. Egress is step 11's axis.

### 4.2 Allowlist schema, and why no globs

```yaml
host_exec:
  version: 1                       # schema version; unknown -> refuse
  allowed_read:  [/home/jojo/proj/ws1]        # absolute, existing, realpath'd at grant time
  allowed_write: [/home/jojo/proj/ws1/out]
  always_denied: [/home/jojo/.thoughtmachine, /home/jojo/.ssh, /home/jojo/proj/ws1/.git/config]
  admit_shells: false              # §9 Q1; false until the operator rules
  landlock:
    abi_min: 1                     # below this, refuse (§4.3)
    require_complete_mask: true    # §5; a partial mask is a refusal, not a config
```

**No globs, and none expanded at call time.** A glob re-expanded per call means the *enforced* set drifts from the *displayed* set whenever a file appears — the user approves `out/*.log` and gets whatever matches at 03:00. Landlock has no globbing either, so a glob would be expanded to paths at grant time regardless; doing it once, recording the expansion and showing it is the only version where the grant means one thing. If the expansion changes, the grant is re-approved. This is F1 applied to a list.

**`always_denied` is subtracted last and wins.** The vault (`~/.thoughtmachine`), `~/.ssh` and `.git/config` belong there by default, mirroring the deny list the file tools already enforce (`DISK thoughtmachine/security.py:356`, `:596`), so widening a path list can never widen it *over* a credential store. Paths are absolute and `realpath`'d at grant time — the discipline of `DISK tools/base.py:381` for `worktree_root`.

**A path allows its subtree, and that is stated, not implied.** Only the single-file rule was probed (`PROBE`: one file allowlisted, one denied); behaviour for files created later under an allowed directory is **UNVERIFIED** and must be probed before the schema ships, because "allow the output directory" is exactly the case that matters.

### 4.3 ABI negotiation, and what happens when Landlock is absent

Probe once at process start (syscall 444, flags=VERSION — `PROBE`), cache the integer and write it into every audit record: a refusal that does not name its layer is unactionable (§8, L5). If the probe fails or returns `< landlock.abi_min`, the `host_exec` surface is **unavailable** and the `host_bash` grain is forced to `banned` for that session rather than falling back to option B. That is L2: a control that cannot do its job refuses; it does not degrade into a different control carrying the same name in the UI.

Second detail: naming a bit the running ABI does not implement is expected to make ruleset creation fail rather than be ignored, so the mask must be built from what the ABI supports and a create-time failure is a **refusal**, never a silent retry with a smaller mask. That path is **UNVERIFIED** here — no probe was run — and L2 is exactly the law that says an unverified path must fail closed.

### 4.4 Refusal messages

One row per refusal. Each names the path **and** the layer, because the layer tells the user whether to change the grant or the kernel.

| condition | exact message | layer named |
|---|---|---|
| ABI probe failed | `host_exec refused: Landlock is unavailable on this kernel (ABI probe failed). A path allowlist cannot be enforced here, so the grant was refused.` | kernel / Landlock |
| ABI below minimum | `host_exec refused: Landlock ABI {n} is below the required minimum {min}. Enforcement cannot be guaranteed.` | kernel / Landlock |
| mask incomplete | `host_exec refused: filesystem mask incomplete (missing {bit}). A partial mask does not deny; see §5.` | ThoughtMachine |
| ruleset create failed | `host_exec refused: Landlock create failed (EINVAL). The mask names a bit this ABI does not support.` | kernel / Landlock |
| path outside grant | `host_exec refused: {path} is not in this grant (allowed_read: {…}; allowed_write: {…}).` | ThoughtMachine |
| path in deny list | `host_exec refused: {path} is in always_denied and cannot be granted.` | ThoughtMachine |
| cwd outside grant | `host_exec refused: cwd {path} is outside every allowed path in this grant.` | ThoughtMachine |
| shell not admitted | `host_exec refused: {argv[0]} is a shell; this grant does not admit shells (admit_shells: false).` | ThoughtMachine |
| credential unbound | `host_exec refused: credential {ref} is not bound to this workspace.` | ThoughtMachine / step 9 |
| kernel denies at runtime | `host_exec: the kernel denied {path} for {op} (Landlock EACCES). This is enforcement, not a tool defect.` | kernel / Landlock |
| child timed out | `host_exec: {argv[0]} exceeded {timeout}s and was killed.` | ThoughtMachine |

The last-but-one row matters most for usability: when option A works the agent sees `EACCES` from a program that has no idea it is confined, and translating that into a sentence naming Landlock is the difference between a controlled refusal and an apparent bug report.

## 5. The total-mask law

This is the most important section in the note, because it is the one thing here that was measured rather than reasoned about.

The probe installed a ruleset whose `handled_access_fs` named **only** `READ_FILE`, with a single allow rule on `/workspace/README.md`. Result: the allowlisted read succeeded, the non-allowlisted read of `/etc/hostname` failed with errno 13 (`PROBE`) — and **directory enumeration of `/etc` leaked, and a write to `/tmp` succeeded** (`PROBE`). Nothing was broken. The mask simply did not name `READ_DIR` or `WRITE_FILE`, so those operations were never mediated.

Therefore, as law:

1. Every filesystem access bit the shell may not perform **must** be named in `handled_access_fs`. The complete form is `0x7fff` (`PROBE`, all bits set), and that is the shape to assert, not a hand-picked subset.
2. The mask is **asserted complete by a test**, not by review: a call that opens, enumerates, writes, creates, renames and removes inside a non-allowlisted path must *all* fail. A test that checks one read is the test that passed before the leak.
3. A partial mask is a **bug**, not a configuration option. `require_complete_mask: true` is in the schema for that reason, and `false` is not a supported value.

**"Landlock works" is not "Landlock confines."** The first sentence is what the ABI probe proves; only the mask makes the second true. Any review of this feature that stops at "restrict_self returned 0" has reviewed nothing.

## 6. Q3 — what this closes

Each item maps to the framing requirements: **F1** — the permissions the user SEES must not lie; **F2** — the agent must never execute on the user's computer outside a named gate.

- **Accidental escape** — an agent wandering out of the workspace for a config file: Landlock returns EACCES instead of a result, and the refusal names the path (§4.4). *F2.*
- **A mistyped or over-broad path** — `allowed_write: ["/home/jojo"]` is visible in the UI as `/home/jojo`, next to the grant, at approval time; the narrowing is a *displayed* object, so an over-broad grant can be seen and refused. *F1.*
- **Path traversal and symlink escape** — covered twice: policy-side by the `realpath` + second-containment pass that already exists (`DISK thoughtmachine/security.py:623`, `:632`, `:661`), kernel-side because the rule was built from the resolved path. A `..` in `argv` is irrelevant — the path it resolves to is what the kernel compares. *F2.*
- **Reading credential material the user never handed over** — `~/.ssh`, `~/.thoughtmachine`, `.git/config` sit in `always_denied` and are subtracted last, so no path grant re-includes them. *F1 and F2.*
- **Cross-workspace bleed** — the allowlist is per grant, so workspace A's output directory is unreachable from B's grant unless B names it, and then it is visible in B's permissions. *F1.*
- **A legitimate binary reading data it should not** — the case motivating the note: the binary is allowed, the *path it reads* is not, and the kernel does not care how legitimate the binary is. *F2.*

## 7. Q4 — what this does NOT close

Design notes usually lie here. This one will not.

- **A malicious or trojaned binary inside the allowed path is still inside it.** Narrowing is a contract about where a program may reach, not a judgement about what it does there. If `argv[0]` is attacker-controlled and inside `allowed_read`, it runs with the grant's write paths available. The allowlist must therefore be about **binaries as well as paths** (§9 Q1).
- **Data exfiltration is not closed, and cannot be closed here.** Without a network namespace (measured: `HOST unshare -Ur true` → `Operation not permitted`) the child shares the host network. A confined program can still open a socket. Egress is step 11's axis and this note does not touch it.
- **Files reached through an already-open descriptor.** Landlock mediates path opening; a descriptor inherited from the parent, or opened before the domain was installed, is not re-mediated by the path rules. The design installs the domain in the child *before* `execve` and passes no inherited descriptors — but that is a property of our launcher, not of Landlock, and it must be asserted in the implementation.
- **Side channels.** Timing differences, `/proc` metadata and differing error codes still leak whether a path exists. A refusal is informative; that is the price of a readable refusal (§4.4), and it is the right price.
- **Allowlisting a shell re-opens everything the shell can reach.** `argv = ["bash", "-c", ...]` with a write path grants that write path to anything bash can invoke anywhere. This is why `admit_shells` defaults to `false` and why §9 Q1 is the first open question rather than a footnote.
- **A kernel without Landlock, or an LSM bypass.** ABI negotiation refuses (§4.3), so the first is handled; the second is out of scope for any in-process control and is the operator's threat model, not ours.

## 8. Failure modes and the rules that prevent them

**L1 — The mask must be total.** Measured: with only `READ_FILE` handled, the read was denied while enumeration leaked and a `/tmp` write succeeded (`PROBE §5`). A partial mask is a bug.

**L2 — Fail closed; never degrade.** If the ABI is unknown, Landlock is absent, the ruleset cannot be created, or the mask cannot be asserted complete, the call is **REFUSED** — not downgraded to option B, not retried with a smaller mask. A control that cannot do its job must refuse, never degrade into a different control that carries the same name in the UI. That this is real and not hypothetical: the current gate already has a deny-all ceiling for exactly this purpose (`DISK security/security_gate.py:92`, `_DISK_FAIL_CLOSED_CEILING` → `host_bash="banned"`).

**L3 — A narrowing axis is inert unless it is consulted at the choke point.** All six file-writing tools funnel through one validator (`DISK tools/base.py:490`) into `DISK thoughtmachine/security.py:427`, and that call passes `mode='read'` — **hardcoded, for writers too** (`DISK tools/base.py:505`: `security_validate_path(path, mode='read', ...)`). `mode` is interpolated into the audit log and never enforced, so the `filesystem` grain (`banned|ask|read|write`, `DISK security/resource_catalog.py:39-47`) is not consulted anywhere on that path. The axis exists, is plumbed end to end, and does nothing. Any new axis must be consulted at the choke point or it is decoration.

**L4 — A vocabulary that silently drops malformed entries is fail-open.** `HostBashTool` requires `["host_bash:allow"]` (`DISK tools/host_bash_tool.py:89`) precisely because `check_required_categories` **skips** any entry lacking `":"` — the code reads `if ":" not in req: continue  # malformed, skip` (`DISK security/security_gate.py:1818-1819`). A bare `["host_bash"]` therefore gates nothing while looking like it gates something. The rule: a malformed requirement is a **refusal**, never a `continue`.

**L5 — The narrowing must be visible.** The allowlist is displayed next to the grant in the permissions view, and every refusal names the path and the layer (`F1`). A narrowed grant the user cannot see is worse than the wide grant it replaced, because it moves the lie from the permission to the review.

## 9. Open questions for the operator

**Q1. Does the allowlist admit shells?**
Options: (a) never — `admit_shells: false`, any `argv[0]` resolving to a shell refused; (b) only when the grant enumerates binaries as well as paths; (c) yes, because a shell is what makes the tool useful.
Trade: (c) makes every path in the grant reachable by every program on the machine — the allowlist becomes a lie in exactly the way F1 forbids; (a) is honest but rules out most real uses of a host shell. **Recommendation: (b), shipping `admit_shells: false` until (b) is implemented** — narrowing that ignores which binary runs is not narrowing.

**Q2. Is a path read-only or read-write per entry?**
Options: (a) separate lists per right, as sketched (`allowed_read` / `allowed_write`); (b) one list with a mode suffix; (c) one list, read-write throughout.
Trade: (c) makes the common case (read a toolchain, write to one output directory) write everywhere; (b) invites suffix-parsing bugs on paths containing the suffix. **Recommendation: (a)** — two arrays of the same type, matching how the operation bits are named in the kernel.

**Q3. Does one denial kill the whole call, or only the offending path?**
Options: (a) kill the call and report the first denial; (b) let the child continue and report every denial at exit.
Trade: (b) gives a complete picture of what a program wanted, but a partially-successful run may have written half its output — a state the user did not approve; (a) is safer and easier to explain. **Recommendation: (a)**, with the full attempted path list attached to the refusal when the child can report it.

**Q4. May the agent request a wider grant itself?**
Options: (a) no — the agent may only use what the user granted, widening is user-only; (b) yes, through the existing approval prompt.
Trade: (b) is convenient and converts a permission boundary into a prompt the user learns to click through, and lets a compromised agent argue for its own expansion; (a) keeps the gate at the human. **Recommendation: (a)** — a grant the confined party can widen is not a confinement.

**Q5. What is the rollout?**
Options: (a) opt-in per workspace, `banned` by default, migration documented; (b) default for new workspaces only; (c) default everywhere with an escape hatch.
Trade: (c) breaks every existing workspace that legitimately runs host commands and guarantees an escape hatch that becomes the real configuration; (b) yields two long-lived populations with different security properties. **Recommendation: (a)** — the narrowing is per-grant, so a workspace can move one grant at a time.

### Lessons recorded while writing this note

1. **A citation in the source can be stale and still look authoritative.** `tools/host_bash_tool.py:81-82` cites `security_gate.py:1483-1485` for the skip-malformed behaviour; at `c27dfbe2` that code is at **`:1818-1819`** (verified this session). Anything citing line numbers must be re-grepped before publication, including citations a brief hands you.
2. **A measurement's layer is part of the measurement.** The container reported the LSM list as UNAVAILABLE (securityfs empty) while the host reported a nine-entry stack; recording only the value would have produced a confident, wrong sentence in §3.
3. **The strongest thing found was a negative.** "userns denied" rules out an entire family of designs (bwrap, nsjail, mount namespaces, removing network access). A recon that only lists what exists would have missed the constraint that shapes the whole note.
4. **I corrected the evidence file while writing this.** `.scratch/host-primitives.md` §C3 says a seccomp filter "cannot be added to a running process without a helper that re-execs" — too strong: a child can install a filter on itself before `execve`, which needs no helper. §3 states the narrower claim. The evidence file is evidence, not scripture; when the design contradicts it, say so and say where.
5. **Globs were the tempting shortcut and are the F1 violation.** The obvious schema is "allow `out/*.log`". It reads well and means something different at 03:00 than at approval time. Refusing the convenient form was the hardest single decision in §4.

## 10. Where this leaves the sprint

**Step 5 — two-axis permissions.** The gate already carries an ordered vocabulary per grain (`banned|ask|read|write` for `filesystem`, `banned|ask|allow` for `host_bash`; `DISK security/resource_catalog.py:39-47`) and a genuine sub-scope precedent — `write_on_feature_branch` is a narrowing of `write`, derived by `split_git_permission` (`DISK security/security_gate.py:590`). A narrowed host grant is therefore an **extension** of an existing shape: a new level or sub-scope on `host_bash`, resolved by the same rank/ceiling machinery (`DISK security/security_gate.py:568` `cap`, `:148` `_min_permission`, `:310` `apply_workspace_ceiling`), not a new mechanism. What step 5 must add is the *payload* — the allowlist that travels with the level.

**Step 7 — workspace-scoped file tool.** That tool is §4's **option B** applied to our own tools, and it exists in substance: all six writers funnel through `DISK tools/base.py:490` into `DISK thoughtmachine/security.py:427`, which resolves symlinks and re-checks containment (`:623`, `:632`, `:661`). L3 is why any read/write distinction there must be enforced **inside `base.py:490`** and not in each tool: the tools already carry a `mode` argument that reaches `base.py:505` and is discarded into a log line. Fix the choke point or repeat the decoration six times.

**Step 9 — credentials.** Binding a credential to a resource means the allowlist can name **the credential's own path and nothing else** — `always_denied` covers the stores (`~/.ssh`, `~/.thoughtmachine`, `.git/config`), so a credential is usable without being readable. The strongest form of this feature: the agent can spend a token it can never print.

**Step 11 — resources and egress.** Egress is a **separate axis** and must not be conflated with paths. This note measured why: without a network namespace the child shares the host network (§3), so no path allowlist can close an exfiltration channel, and a `network` parameter on `host_exec` would be a lie in the interface (§4.1). Step 11 owns that axis; this note deliberately does not.
