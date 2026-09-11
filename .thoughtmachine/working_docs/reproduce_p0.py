#!/usr/bin/env python3
# reproduce_p0.py - HOST-RUN P0 bug reproducer for ThoughtMachine-dev
#
# PURPOSE
#   Reproduce / investigate four P0 container-lifecycle defects against a REAL
#   Docker daemon running on the HOST. Launch via `.thoughtmachine/working_docs/
#   reproduce_p0.sh` (which wraps it with preflight + cleanup).
#
# SAFETY
#   - Only ever touches containers whose name starts with "tm-p0-".
#   - Vault roots are SPLIT into a read target and a write target:
#       $P0_VAULT   = READ target. Defaults to the throwaway /tmp/tm-p0-vault,
#                     and flips to the operator's REAL vault
#                     (~/.thoughtmachine) when P0_WS is set (REAL_MODE), so
#                     real-mode evidence is read from REAL data.
#       $P0_SCRATCH = WRITE target. ALWAYS throwaway (/tmp/tm-p0-vault).
#     Every write performed by this driver is rooted at $P0_SCRATCH, so
#     REAL_MODE must NEVER write to $P0_VAULT.
#   - NEVER modifies repository files.
#
# This driver uses the REAL ContainerManager + REAL GitReadTool. No mocks.
# Every section is wrapped in try/except and prints the FULL traceback so one
# failing section still lets the rest of the run (and the SUMMARY) complete.
#
# NOTE: this script performs live Docker operations. It is meant to be executed
# on the HOST (real docker daemon). Running it where the docker socket is absent
# will report the daemon as UNAVAILABLE and the BUG sections as BLOCKED/ERROR.

import contextlib
import importlib
import inspect
import json
import os
import pathlib
import platform
import sys
import traceback

# --- repo root on sys.path -------------------------------------------------
_THIS = os.path.abspath(__file__)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_THIS)))
for _p in (REPO_ROOT, os.getcwd()):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

P0_IMAGE = os.environ.get("P0_IMAGE", "alpine:latest")
# P0_IMAGE_B: OPTIONAL second image used ONLY for the SECTION 2 network-diag
# container. Provide an image that HAS python3 (e.g. the agent-executor image)
# so the bare-IP TCP errno probe can run; falls back to P0_IMAGE when unset.
P0_IMAGE_B = os.environ.get("P0_IMAGE_B", "").strip()
NET_IMAGE = P0_IMAGE_B or P0_IMAGE
# P0_WS: the operator's REAL workspace id. When set, the driver ALSO runs the
# BUG1/BUG2 scenarios against the operator's REAL vault + REAL workspace, in
# STRICTLY READ-ONLY fashion (nothing is ever written under P0_REAL_VAULT_ROOT).
P0_WS = os.environ.get("P0_WS", "").strip()
P0_REAL_VAULT_ROOT = os.environ.get("P0_REAL_VAULT_ROOT", "").strip()
REAL_MODE = bool(P0_WS)
NAME_PREFIX = "tm-p0-"

# --- vault roots: READ target vs WRITE target -------------------------------
# P0_SCRATCH: ALWAYS a throwaway path OUTSIDE the operator's real vault. Every
#   WRITE this driver performs (the throwaway workspace fixtures built by
#   setup_vault(), and the read-only-mode log/audit redirection) is rooted here.
# P0_REAL_VAULT: the operator's real vault; the READ target in REAL_MODE.
# P0_VAULT: the vault root the driver RESOLVES against.
#   * P0_WS set (REAL_MODE)     -> P0_REAL_VAULT (~/.thoughtmachine), so the
#     security gate / capability resolution and all evidence read REAL data.
#     REAL_MODE must NEVER write to P0_VAULT.
#   * P0_WS unset (scratch mode) -> P0_SCRATCH (/tmp/tm-p0-vault), unchanged.
#   Either mode stays overridable via $P0_VAULT.
P0_SCRATCH = os.environ.get("P0_SCRATCH", "/tmp/tm-p0-vault")
P0_REAL_VAULT = str(pathlib.Path.home() / ".thoughtmachine")
P0_VAULT = os.environ.get("P0_VAULT") or (
    P0_REAL_VAULT if REAL_MODE else P0_SCRATCH
)

# Point the process vault at P0_VAULT (the READ target): in REAL_MODE that is
# the operator's REAL vault, so incidental framework reads resolve against real
# data. All WRITES are redirected to P0_SCRATCH, never to P0_VAULT in REAL_MODE.
os.environ["THOUGHTMACHINE_VAULT_ROOT"] = P0_VAULT

RESULTS = []  # list of {"bug","verdict","detail"}


# --- tiny helpers ----------------------------------------------------------
def banner(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def record(bug, verdict, detail=""):
    RESULTS.append({"bug": bug, "verdict": verdict, "detail": detail})
    print("\n>>> RECORD [%s] = %s  ::  %s\n" % (bug, verdict, detail))


def show(label, value):
    print("    %s: %r" % (label, value))


def assert_p0_name(name):
    assert isinstance(name, str) and name.startswith(NAME_PREFIX), (
        "SAFETY VIOLATION: attempted to touch non-tm-p0- container: %r" % (name,)
    )


def make_manager(session_id, workspace_id, sp, **kw):
    from infra.container_manager import ContainerManager
    kw.setdefault("vault_root", P0_VAULT)
    kw.setdefault("image", P0_IMAGE)
    return ContainerManager(
        workspace_path=REPO_ROOT,
        session_id=session_id,
        workspace_id=workspace_id,
        session_permissions=sp,
        mem_limit="256m",
        cpu_quota=50000,
        **kw
    )


def count_running(mgr):
    entries = mgr.list_containers()
    running = [e for e in entries if str(e.get("status", "")).lower() == "running"]
    return entries, running


# --- real-workspace (authoritative) mode helpers ---------------------------
def _real_vault_root():
    """Resolve the operator's REAL vault root (env override, else framework)."""
    if P0_REAL_VAULT_ROOT:
        return P0_REAL_VAULT_ROOT
    from thoughtmachine.vault import vault_root as _vr
    return str(_vr())


def snapshot_tree(root):
    """Read-only recursive fingerprint of a directory tree (relpath -> size,mtime)."""
    snap = {}
    if not root or not os.path.isdir(root):
        return snap
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            try:
                st = os.stat(p)
                snap[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns)
            except OSError:
                snap[os.path.relpath(p, root)] = ("<stat-error>",)
    return snap


@contextlib.contextmanager
def real_vault_readonly():
    """Temporarily point THOUGHTMACHINE_VAULT_ROOT at the REAL vault so the
    security gate / capability resolution reads the operator's REAL data, while
    REDIRECTING every incidental framework log write to a scratch dir.

    Guarantees NOTHING is written under the real vault root.
    """
    real_root = _real_vault_root()
    # Rooted at P0_SCRATCH (always throwaway), NEVER at P0_VAULT: in REAL_MODE
    # P0_VAULT *is* the real vault, so deriving scratch from it would create and
    # write inside the very tree this guard promises not to touch.
    scratch = os.path.join(P0_SCRATCH, "real-mode-scratch")
    # Enforce the guarantee arithmetically (not by relying on the /tmp default)
    # so the guard holds for ANY real root, including ~/.thoughtmachine.
    real_root_path = pathlib.Path(real_root).expanduser().resolve()
    scratch_path = pathlib.Path(scratch).expanduser().resolve()
    if scratch_path == real_root_path or real_root_path in scratch_path.parents:
        raise RuntimeError(
            "SAFETY VIOLATION: scratch dir is inside the real vault "
            "(%s inside %s)" % (scratch_path, real_root_path)
        )
    try:
        os.makedirs(scratch, exist_ok=True)
    except Exception:
        pass
    prev_env = os.environ.get("THOUGHTMACHINE_VAULT_ROOT")
    prev_audit = os.environ.get("CONTAINER_AUDIT_LOG_PATH")
    os.environ["THOUGHTMACHINE_VAULT_ROOT"] = real_root
    os.environ["CONTAINER_AUDIT_LOG_PATH"] = os.path.join(scratch, "container_audit.log")
    patched = []
    for modname in ("agent._log_root", "agent.logging", "agent.logging.lifecycle",
                    "agent.logging.event_logger"):
        try:
            mod = importlib.import_module(modname)
        except Exception:
            continue
        if hasattr(mod, "get_log_root"):
            patched.append((mod, mod.get_log_root))
            mod.get_log_root = (lambda _s: (lambda *a, **k: pathlib.Path(_s)))(scratch)
    try:
        yield real_root
    finally:
        for mod, orig in patched:
            mod.get_log_root = orig
        if prev_env is None:
            os.environ.pop("THOUGHTMACHINE_VAULT_ROOT", None)
        else:
            os.environ["THOUGHTMACHINE_VAULT_ROOT"] = prev_env
        if prev_audit is None:
            os.environ.pop("CONTAINER_AUDIT_LOG_PATH", None)
        else:
            os.environ["CONTAINER_AUDIT_LOG_PATH"] = prev_audit


def mode_banner(mode, wsid=None):
    tag = ("MODE: REAL WORKSPACE %s" % wsid) if wsid else "MODE: SYNTHETIC VAULT"
    print("\n" + "*" * 78)
    print("*** %s ***" % tag)
    print("*" * 78)


def read_json_safe(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"__error__": str(e)}


def exec_in_container(mgr, container_id, command):
    """Run *command* inside a tm-p0- container; print stdout + exit code."""
    try:
        c = mgr.client.containers.get(container_id)
        res = c.exec_run(["/bin/sh", "-c", command], demux=False)
        code = getattr(res, "exit_code", None)
        out = res.output or b""
        if isinstance(out, (bytes, bytearray)):
            out = out.decode("utf-8", "replace")
        print("    $ %s" % command)
        print("      exit_code=%s" % code)
        for ln in (out or "").splitlines():
            print("      | %s" % ln)
        return code, out
    except Exception as e:
        print("    $ %s  -> exec FAILED: %s" % (command, e))
        traceback.print_exc()
        return None, ""


def section0d_real_workspace():
    banner("SECTION 0d - REAL-WORKSPACE MODE (READ-ONLY, NO WRITES)")
    import json, pathlib
    ws = os.environ.get("P0_WS")
    if not ws:
        print("P0_WS not set -> real-workspace comparison SKIPPED (synthetic vault only).")
        print("Authoritative Bug-2 evidence requires: P0_WS=<your real workspace id>")
        return
    try:
        from thoughtmachine.vault import vault_root as _vroot
        default_root = pathlib.Path(_vroot())
    except Exception:
        default_root = pathlib.Path(os.environ.get("THOUGHTMACHINE_VAULT_ROOT") or (pathlib.Path.home() / ".thoughtmachine"))
    real_root = pathlib.Path(os.environ.get("P0_REAL_VAULT_ROOT") or default_root)
    ws_dir = real_root / "workspaces" / ws
    print(f"real vault root    : {real_root}")
    print("READ-ONLY GUARANTEE : this section writes NOTHING under that tree")
    print(f"workspace dir      : {ws_dir}  exists={ws_dir.is_dir()}")
    try:
        before = snapshot_tree(ws_dir)
    except Exception:
        traceback.print_exc()
        before = None
    for fname in ("config.json", "capabilities.json"):
        p = ws_dir / fname
        print(f"--- {fname} exists={p.exists()} path={p}")
        if p.exists():
            try:
                print(p.read_text())
            except Exception:
                traceback.print_exc()
    sp = {"container": True, "network": "outbound", "filesystem": "write", "git": "read"}
    print(f"session_permissions used for the comparison: {sp}")
    expected = {}
    try:
        from security.security_gate import get_expected_container_config
        expected = get_expected_container_config(sp, None)
        print(f"GATE  get_expected_container_config(sp, None) -> {expected}")
    except Exception:
        traceback.print_exc()
    try:
        mgr = make_manager("p0-real-gate", ws, sp, vault_root=str(real_root))
        computed = mgr._compute_config(str(REPO_ROOT), ws, sp)
        print(f"MANAGER _compute_config({REPO_ROOT!r}, {ws!r}, sp) -> {computed}   (instance-bound hook: docker_executor._compute_container_config_from_permissions)")
        gate_net = (expected or {}).get("network_mode")
        mgr_net = computed[0] if isinstance(computed, (tuple, list)) and computed else None
        if gate_net is not None and mgr_net is not None:
            verdict = "AGREE" if gate_net == mgr_net else "DIVERGENCE (this is Bug 2's mechanism)"
            print(f"BUG2 PRE-VERDICT: gate network_mode={gate_net!r} vs manager network_mode={mgr_net!r} -> {verdict}")
    except Exception:
        print(traceback.format_exc())
        print("MANAGER _compute_config: could not be evaluated (traceback above)")
    if before is not None:
        try:
            after = snapshot_tree(ws_dir)
            print(f"TREE UNCHANGED: {before == after}  (before={len(before)} entries, after={len(after)} entries)")
            if before != after:
                print("!! WARNING: the real vault tree CHANGED during a read-only section")
                print("   before:", before)
                print("   after :", after)
        except Exception:
            traceback.print_exc()


def section_real_probes():
    """REAL-WORKSPACE probes. Runs ONLY when P0_WS is set. Creates ONLY tm-p0-* containers."""
    banner("SECTION 0e - REAL-WORKSPACE PROBES (Bug1 census + Bug2 inspect)")
    ws = os.environ.get("P0_WS")
    if not ws:
        print("P0_WS not set -> real probes SKIPPED. This is the SINGLE most authoritative section:")
        print("  re-run with:  P0_WS=<your real workspace id> bash .thoughtmachine/working_docs/reproduce_p0.sh")
        return
    real_root = os.environ.get("P0_REAL_VAULT_ROOT")   # None => framework default (vault_root())
    kw = {}
    if real_root:
        kw["vault_root"] = real_root
    sp = {"container": True, "network": "outbound", "filesystem": "write", "git": "read"}
    try:
        mgr = make_manager("p0-real-probes", ws, sp, **kw)
    except Exception:
        print("REAL PROBES BLOCKED: could not build ContainerManager (see traceback; no docker daemon?)")
        print(traceback.format_exc())
        return
    try:
        mgr.client.ping()
        _docker_ok = True
    except Exception:
        _docker_ok = False
    # ---- BUG 1, real workspace: census BEFORE any creation ----
    try:
        census = mgr.list_containers()
        running = [c for c in census if str(c.get("status", "")).startswith("running")]
        print(f"BUG1 REAL CENSUS: limit={mgr._get_max_containers()}  counted(all=True)={len(census)}  running_only={len(running)}")
        for c in census:
            print(f"   - {c.get('name')!r:30} status={c.get('status')!r:12} image={c.get('image')!r} id={str(c.get('container_id'))[:12]}")
        if len(census) >= mgr._get_max_containers():
            print(">>> limit already reached by EXISTING containers -> attempting a start to capture the refusal message")
            r = mgr.start(name="tm-p0-real-limit", image=os.environ.get("P0_IMAGE", "alpine:latest"))
            print(f"    start() returned: {r!r}")
            print("    <- if this is the limit error while running_only is smaller than limit, THAT IS BUG 1 ON A REAL WORKSPACE")
        else:
            print(f">>> count({len(census)}) < limit({mgr._get_max_containers()}): no refusal to capture here; the synthetic section demonstrates the stop-does-not-free-a-slot transition.")
    except Exception:
        traceback.print_exc()
    # ---- BUG 2, real workspace: ONE real container + inspect ----
    try:
        if not _docker_ok:
            print("BUG2 REAL PROBE BLOCKED: no docker daemon")
        else:
            r = mgr.start(name="tm-p0-real-net", image=os.environ.get("P0_IMAGE", "alpine:latest"))
            print(f"BUG2 mgr.start() returned: {r!r}")
            cid = (r or {}).get("id") or (r or {}).get("container_id")
            print(f"computed by manager: network_mode/workspace_mode = {mgr._compute_config(str(REPO_ROOT), ws, sp)}")
            if cid:
                try:
                    c = mgr.client.containers.get(cid)
                    hc = (c.attrs or {}).get("HostConfig", {})
                    print(f"docker inspect {c.name}: HostConfig.NetworkMode = {hc.get('NetworkMode')!r}")
                    print(f"docker inspect {c.name}: NetworkSettings      = {(c.attrs or {}).get('NetworkSettings')!r}")
                    print(f"docker inspect {c.name}: Config.Image         = {(c.attrs.get('Config') or {}).get('Image')!r}   (requested {os.environ.get('P0_IMAGE')!r})")
                    for cmd in ("cat /proc/net/dev", "cat /proc/net/route", "cat /etc/resolv.conf",
                                "python3 -c \"import socket;\\nsocket.create_connection(('1.1.1.1',443),5)\""):
                        try:
                            out = exec_in_container(mgr, cid, cmd)
                            print(f"--- $ {cmd}\n{out}")
                        except Exception:
                            traceback.print_exc()
                except Exception:
                    traceback.print_exc()
    except Exception:
        traceback.print_exc()
    print("NOTE: only tm-p0-* containers are created here; the CLEANUP section removes exactly those.")


# --- Section 0: preflight --------------------------------------------------
def section0_preflight():
    banner("SECTION 0 - PREFLIGHT")
    print("python   : %s" % sys.version.split()[0])
    print("platform : %s" % platform.platform())
    print("repo_root: %s" % REPO_ROOT)
    print("vault    : %s" % P0_VAULT)
    print("image    : %s" % P0_IMAGE)

    docker = None
    client = None
    try:
        import docker as _docker
        docker = _docker
        print("docker SDK: %s" % getattr(docker, "__version__", "unknown"))
    except Exception:
        print("docker SDK: NOT INSTALLED")
        traceback.print_exc()

    if docker is not None:
        try:
            client = docker.from_env()
            v = client.version()
            print("daemon ping: OK  ServerVersion=%s  ApiVersion=%s"
                  % (v.get("Version"), v.get("ApiVersion")))
        except Exception as e:
            print("daemon: UNAVAILABLE (%s)" % e)
            traceback.print_exc()
            client = None

    print("\n  -- introspected signatures (should match source) --")
    try:
        from infra.container_manager import ContainerManager
        print("  ContainerManager.__init__ %s" % inspect.signature(ContainerManager.__init__))
        print("  ContainerManager.start    %s" % inspect.signature(ContainerManager.start))
        print("  ContainerManager.stop     %s" % inspect.signature(ContainerManager.stop))
        print("  ContainerManager.list_containers %s"
              % inspect.signature(ContainerManager.list_containers))
    except Exception:
        print("  import ContainerManager FAILED")
        traceback.print_exc()
    try:
        from tools.container_control import (
            ContainerStartTool, ContainerStopTool, ContainerListTool,
        )
        for cls in (ContainerStartTool, ContainerStopTool, ContainerListTool):
            print("  %s fields=%s" % (cls.__name__, sorted(cls.model_fields.keys())))
    except Exception:
        print("  import container_control FAILED")
        traceback.print_exc()
    try:
        from tools.git_info_tool import GitReadTool
        print("  GitReadTool.__init__ %s" % inspect.signature(GitReadTool.__init__))
    except Exception:
        print("  import GitReadTool FAILED")
        traceback.print_exc()

    return client


def setup_vault():
    banner("SECTION 0b - THROWAWAY VAULT")
    # Fixtures are ALWAYS written under P0_SCRATCH, NEVER under P0_VAULT: in
    # REAL_MODE P0_VAULT is the operator's real vault, so rooting these writes
    # there would create tm-p0 fixtures inside real data.
    ws_dir = os.path.join(P0_SCRATCH, "workspaces")
    os.makedirs(os.path.join(ws_dir, "p0-ws"), exist_ok=True)
    for wid in ("p0-bug1", "p0-bug2", "p0-bug3"):
        os.makedirs(os.path.join(ws_dir, wid), exist_ok=True)
    cfg = {"max_containers": 8, "disk_quota_mb": 4096}
    cfg_path = os.path.join(ws_dir, "p0-ws", "config.json")
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)
    print("wrote %s = %s" % (cfg_path, cfg))
    print("NOTE: deliberately NO capabilities.json anywhere -> security gate fails CLOSED")


def image_available(client, image):
    if client is None:
        return False
    try:
        client.images.get(image)
        print("image %r present locally" % image)
        return True
    except Exception as e:
        print("image %r NOT found locally: %s" % (image, e))
        print("     -> run:  docker pull %s   (BUG1/BUG3 need a real local image)" % image)
        return False


# --- Section 0c: gate view -------------------------------------------------
def section_gate_view(client):
    banner("SECTION 0c - GATE VIEW (security decode, no containers)")
    try:
        mgr = make_manager("p0-gate", "p0-ws", {})
        for sp in ({"network": "outbound", "filesystem": "read"},
                   {"network": "write", "filesystem": "write"}):
            nm, wm = mgr._compute_config(REPO_ROOT, "p0-ws", sp)
            print("    _compute_config(sp=%s) -> network_mode=%r workspace_mode=%r"
                  % (sp, nm, wm))
    except Exception:
        print("gate view (ContainerManager._compute_config) FAILED")
        traceback.print_exc()

    try:
        from docker_executor import _compute_container_config_from_permissions
        nm, wm = _compute_container_config_from_permissions(
            REPO_ROOT, "p0-ws", {"network": "outbound", "filesystem": "read"})
        print("    _compute_container_config_from_permissions(outbound) -> %r,%r" % (nm, wm))
    except Exception:
        print("gate view (docker_executor) FAILED")
        traceback.print_exc()

    try:
        from security.security_gate import get_expected_container_config
        conf = get_expected_container_config(
            {"network": "outbound", "filesystem": "read"}, None)
        print("    get_expected_container_config(sp, None) -> %s" % (conf,))
    except Exception:
        print("gate view (get_expected_container_config) FAILED")
        traceback.print_exc()

    print("    EXPECTATION: the fail-closed workspace yields network_mode='none' even")
    print("    though the session asked for 'outbound' -> the request is silently capped.")


# --- Section 1: BUG1 - limit counts stopped containers ---------------------
def section_bug1(client, image_ok):
    banner("SECTION 1 - BUG1: container limit counts STOPPED containers")
    if client is None:
        record("BUG1", "BLOCKED", "no docker daemon")
        return
    if not image_ok:
        record("BUG1", "BLOCKED", "local image %r missing; docker pull it and re-run" % P0_IMAGE)
        return
    try:
        mgr = make_manager("p0-bug1", "p0-bug1", {},
                           session_config={"container_limits": {"max_containers": 2}})
        print("    effective limit (_get_max_containers) = %s" % mgr._get_max_containers())
        r1 = mgr.start(name="tm-p0-1")
        r2 = mgr.start(name="tm-p0-2")
        show("start tm-p0-1", r1)
        show("start tm-p0-2", r2)
        for r in (r1, r2):
            assert_p0_name(r.get("name", "tm-p0-x"))
        mgr.stop(r1.get("id") or r1.get("container_id"))
        mgr.stop(r2.get("id") or r2.get("container_id"))
        entries, running = count_running(mgr)
        print("    list_containers len=%d  running=%d" % (len(entries), len(running)))
        print("    statuses=%s" % [e.get("status") for e in entries])
        r3 = mgr.start(name="tm-p0-3")
        show("start tm-p0-3 (all stopped)", r3)
        err = str(r3.get("error", ""))
        if "limit" in err.lower() and len(running) == 0:
            record("BUG1", "REPRODUCED",
                   "%r returned while running==0 (%d stopped containers were counted)"
                   % (err, len(entries)))
        elif "id" in r3:
            record("BUG1", "NOT-REPRODUCED",
                   "3rd start succeeded despite 2 stopped containers")
        else:
            record("BUG1", "INCONCLUSIVE", "unexpected start result: %r" % (r3,))
    except Exception:
        print("BUG1 section raised:")
        traceback.print_exc()
        record("BUG1", "ERROR", "see traceback above")


# --- Section 2: BUG2 - requested network silently ignored ------------------
def section_bug2(client, image_ok):
    banner("SECTION 2 - BUG2: requested network silently ignored")
    if client is None:
        record("BUG2", "BLOCKED", "no docker daemon")
        return
    if not image_ok:
        record("BUG2", "BLOCKED", "local image %r missing; docker pull it and re-run" % P0_IMAGE)
        return
    try:
        mgr = make_manager("p0-bug2", "p0-bug2", {"network": "outbound"})
        nm, wm = mgr._compute_config(REPO_ROOT, "p0-bug2",
                                     {"network": "outbound", "filesystem": "read"})
        print("    (a) _compute_config(outbound) -> network_mode=%r workspace_mode=%r" % (nm, wm))

        r = mgr.start(name="tm-p0-net")
        show("start tm-p0-net (sp network=outbound)", r)
        actual = None
        if r.get("id"):
            attrs = mgr.client.containers.get(r["id"]).attrs
            actual = attrs.get("HostConfig", {}).get("NetworkMode")
        print("    (a) actual HostConfig.NetworkMode = %r" % (actual,))
        if actual is not None and actual != "bridge":
            record("BUG2(a)", "REPRODUCED",
                   "session asked network='outbound' but container NetworkMode=%r "
                   "(silently capped; only a WARNING is logged)" % (actual,))
        elif actual == "bridge":
            record("BUG2(a)", "NOT-REPRODUCED", "network honoured (NetworkMode=bridge)")
        else:
            record("BUG2(a)", "INCONCLUSIVE", "could not read NetworkMode")

        # (c) reuse path skips the config check entirely
        mgrA = make_manager("p0-bug2", "p0-bug2", {})
        rA = mgrA.start(name="tm-p0-net-reuse")
        show("start tm-p0-net-reuse (sp={})", rA)
        mgrB = make_manager("p0-bug2", "p0-bug2", {"network": "outbound"})
        rB = mgrB.start(name="tm-p0-net-reuse")
        show("start tm-p0-net-reuse (sp network=outbound)", rB)
        reuse_mode = None
        if rB.get("id"):
            reuse_mode = (mgrB.client.containers.get(rB["id"]).attrs
                          .get("HostConfig", {}).get("NetworkMode"))
        print("    (c) reuse status=%r NetworkMode=%r" % (rB.get("status"), reuse_mode))
        if rB.get("status") == "reused" and reuse_mode == "none":
            record("BUG2(c)", "REPRODUCED",
                   "container created with no network is returned 'reused' when "
                   "network='outbound' is later requested -> newly granted network ignored")
        elif rB.get("status") == "reused":
            record("BUG2(c)", "NOT-REPRODUCED", "reused but NetworkMode=%r" % (reuse_mode,))
        else:
            record("BUG2(c)", "INCONCLUSIVE", "unexpected reuse result: %r" % (rB,))
    except Exception:
        print("BUG2 section raised:")
        traceback.print_exc()
        record("BUG2", "ERROR", "see traceback above")


# --- Section 3: BUG3 - image mismatch silently reused ----------------------
def section_bug3(client, image_ok):
    banner("SECTION 3 - BUG3: image mismatch silently reused")
    if client is None:
        record("BUG3", "BLOCKED", "no docker daemon")
        return
    if not image_ok:
        record("BUG3", "BLOCKED", "local image %r missing; docker pull it and re-run" % P0_IMAGE)
        return
    try:
        mgr = make_manager("p0-bug3", "p0-bug3", {})
        r1 = mgr.start(name="tm-p0-img", image=P0_IMAGE)
        show("start tm-p0-img image=%s" % P0_IMAGE, r1)
        bogus = "tm-p0-nonexistent:latest"
        # reuse short-circuits before image resolution, so the bogus tag need not exist
        r2 = mgr.start(name="tm-p0-img", image=bogus)
        show("start tm-p0-img image=%s" % bogus, r2)
        cfg_image = None
        if r2.get("id"):
            cfg_image = mgr.client.containers.get(r2["id"]).attrs.get("Config", {}).get("Image")
        print("    reuse status=%r Config.Image=%r" % (r2.get("status"), cfg_image))
        if r2.get("status") == "reused" and cfg_image == P0_IMAGE:
            record("BUG3", "REPRODUCED",
                   "requested image=%r silently ignored; container still runs %r"
                   % (bogus, cfg_image))
        elif r2.get("status") == "reused":
            record("BUG3", "NOT-REPRODUCED", "reused, Config.Image=%r" % (cfg_image,))
        else:
            record("BUG3", "INCONCLUSIVE", "unexpected result: %r" % (r2,))
    except Exception:
        print("BUG3 section raised:")
        traceback.print_exc()
        record("BUG3", "ERROR", "see traceback above")


# --- Section 4: BUG4 - git fallback (INVESTIGATION ONLY) -------------------
def section_bug4():
    banner("SECTION 4 - BUG4: git execution-mode fallback  (INVESTIGATION ONLY - NO CONTAINERS)")
    try:
        from tools.git_info_tool import GitReadTool, resolve_git_execution_mode
    except Exception:
        print("BUG4 import failed:")
        traceback.print_exc()
        record("BUG4", "ERROR", "import failed")
        return
    try:
        tool = GitReadTool(operation="status", session_id="p0-session",
                           workspace_id="p0-ws", session_permissions={})
        print("    tool._resolved_workspace_path=%r" % tool._resolved_workspace_path)
        print("    tool._resolved_workspace_id  =%r" % tool._resolved_workspace_id)
        combos = [
            ("empty cfg/meta, no ws",              {}, {}, None, None),
            ("empty cfg/meta, with ws",            {}, {}, REPO_ROOT, "p0-ws"),
            ("cfg git_execution_mode=host",        {"git_execution_mode": "host"}, {}, REPO_ROOT, "p0-ws"),
            ("meta git_execution_mode=host",       {}, {"git_execution_mode": "host"}, REPO_ROOT, "p0-ws"),
            ("cfg git_execution_mode=container",   {"git_execution_mode": "container"}, {}, REPO_ROOT, "p0-ws"),
            ("cfg container, no ws id",            {"git_execution_mode": "container"}, {}, REPO_ROOT, None),
        ]
        for label, ac, meta, wp, wid in combos:
            mode = resolve_git_execution_mode(ac, meta, wp, wid)
            print("    resolve_git_execution_mode(%s) -> %r" % (label, mode))
        print("    tool._git_execution_mode() = %r" % tool._git_execution_mode())
        print("    tool._use_container_mode() = %r" % tool._use_container_mode())
        print("    --- source-derived fallback trigger conditions ---")
        print("    _resolve_resource_execution -> host_fallback when not _use_container_mode();")
        print("    _use_container_mode() is False when _git_execution_mode()=='host' OR when")
        print("    there is no resolved workspace path/id. In the fallback path a")
        print("    'degraded to hardened host git' WARNING is logged and the per-call trailer")
        print("    reports execution_mode/fallback_used accordingly.")
        if hasattr(tool, "_with_mode"):
            tool._last_execution_mode = "host_fallback"
            tool._last_failure_reason = "resource_unavailable"
            tool._last_fallback_used = True
            print("    _with_mode('DUMMY') ->")
            for ln in tool._with_mode("DUMMY").splitlines():
                print("      " + ln)
        else:
            print("    (no _with_mode helper) expected trailer:")
            print("      DUMMY\\nexecution_mode: host_fallback\\n"
                  "failure_reason: resource_unavailable\\nfallback_used: true")
        record("BUG4", "INVESTIGATION",
               "git fallback decision surface printed; NO container started")
    except Exception:
        print("BUG4 section raised:")
        traceback.print_exc()
        record("BUG4", "ERROR", "see traceback above")


# --- cleanup + summary -----------------------------------------------------
def cleanup(client):
    banner("CLEANUP - removing ONLY tm-p0- containers")
    if client is None:
        print("no docker client; skipping cleanup")
        return
    try:
        names = []
        for c in client.containers.list(all=True):
            if c.name.startswith(NAME_PREFIX):
                assert_p0_name(c.name)
                names.append(c.name)
        print("tm-p0- containers found: %s" % names)
        for nm in names:
            try:
                client.containers.get(nm).remove(force=True)
                print("    removed %s" % nm)
            except Exception as e:
                print("    FAILED to remove %s: %s" % (nm, e))
    except Exception:
        traceback.print_exc()


def summary():
    banner("SUMMARY")
    print("%-10s%-18s%s" % ("BUG", "VERDICT", "DETAIL"))
    print("-" * 78)
    for r in RESULTS:
        print("%-10s%-18s%s" % (r["bug"], r["verdict"], r["detail"]))
    print("-" * 78)
    print("Verdicts: REPRODUCED | NOT-REPRODUCED | INVESTIGATION | INCONCLUSIVE |"
          " BLOCKED | ERROR")


def main():
    client = section0_preflight()
    setup_vault()
    image_ok = image_available(client, P0_IMAGE)
    section_gate_view(client)
    try:
        section0d_real_workspace()
    except Exception:
        print(traceback.format_exc())
    try:
        section_real_probes()
    except Exception:
        print(traceback.format_exc())
    section_bug1(client, image_ok)
    section_bug2(client, image_ok)
    section_bug3(client, image_ok)
    section_bug4()
    cleanup(client)
    summary()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
    finally:
        print("\n[done]")
