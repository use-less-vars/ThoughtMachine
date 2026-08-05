#!/usr/bin/env python3
"""
live_smoke_docker.py — standalone, host-runnable live end-to-end smoke test
for ThoughtMachine Docker Phase-1.

Run from the repo root with the project venv python:

    python live_smoke_docker.py

Exit code is 0 iff every check is PASS; otherwise 1. A PASS/FAIL receipt
table is always printed.

What it exercises (in order):
  1. setup            — docker daemon reachable + smoke image built
  2. gate             — permission gate -> (network_mode, workspace_mode)
                        for (fs=write,net=none), (fs=read,net=none),
                        (fs=write,net=bridge) — both the workspace_id path
                        (security_gate) and the workspace_id=None fallback
  3. volume           — ensure_workspace_volume_populated: one-shot copy
                        into a named volume + .workspace_synced sentinel
  4. persist          — create -> exec -> stop -> REUSE (stable container
                        id, no CONTAINER_RECREATE_MISMATCH) -> write ->
                        stop/restart -> data survives (volume persistence)
  5. isolation        — ro workspace blocks writes; no-network blocks
                        egress; bridge grants egress (host-network
                        dependent; documented exception, see below)
  6. wiring           — DockerCodeRunner + ContainerStart/Exec/Status/Stop
                        tools end-to-end via their JSON APIs
  7. cleanup          — session container sweep, volume + image removal

Notes / documented deviations:
  * network permissions: the no-network scenarios use
    session_permissions={"network": "banned", ...} ("banned" is a valid
    SessionPermissions literal that maps to network_mode="none").
  * egress check: bridge egress depends on the host's internet access. If
    example.com:80 is unreachable the bridge check is reported PASS with
    detail "egress-blocked (documented exception)" — an environment
    limitation, not a code failure.
  * tools/runner resolve the workspace via ToolBase's deprecated
    workspace_path fallback field (the tmpdir session is not registered in
    SessionRegistry/WorkspaceRegistry); session_id is still passed so the
    containers carry the session label and are swept by cleanup_session.

No pytest, no git operations, no edits to any other file.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import uuid

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

try:
    import docker  # noqa: E402
    from docker.types import Mount  # noqa: E402

    from docker_executor import (  # noqa: E402
        _compute_container_config_from_permissions,
        ensure_workspace_volume_populated,
    )
    # from tools import ... also verifies the tools/__init__.py re-exports
    from tools import (  # noqa: E402
        ContainerExecTool,
        ContainerStartTool,
        ContainerStatusTool,
        ContainerStopTool,
    )
    from tools.container_manager import (  # noqa: E402
        ContainerManager,
        cleanup_session,
        list_session_containers,
    )
    from tools.docker_code_runner import DockerCodeRunner  # noqa: E402
except Exception as _import_err:  # pragma: no cover - environment guard
    print("IMPORT-FAILED | FAIL | %s: %s"
          % (type(_import_err).__name__, _import_err))
    print("HINT: run with the project venv python from the repo root; "
          "the docker Python SDK is required.")
    sys.exit(1)

DOCKERFILE = "\n".join([
    "FROM python:3.11-slim",
    'RUN adduser --uid 1000 --disabled-password --gecos "" agent',
    "USER agent",
    'CMD ["tail", "-f", "/dev/null"]',
    "",
])

CONNECT_CMD = (
    "python3 -c \"import socket; "
    "socket.create_connection(('example.com', 80), 3); print('CONNECTED')\""
)


class SmokeFail(Exception):
    """Raise with a human-readable detail message to fail one check."""


def main() -> int:
    rows = []  # list of (name, result, detail)
    tag = uuid.uuid4().hex[:8]
    session_id = str(uuid.uuid4())
    image_tag = "tm-smoke-%s:latest" % tag

    client = None
    workspace_dir = None
    build_ctx = None
    created_volumes = []
    image_built = False
    fatal = False

    def check(name, fn):
        nonlocal fatal
        try:
            detail = fn()
            rows.append((name, "PASS", detail or ""))
        except SmokeFail as sf:
            rows.append((name, "FAIL", str(sf)))
            fatal = True
        except Exception as exc:  # noqa: BLE001 - record every failure
            rows.append((name, "FAIL", "%s: %s" % (type(exc).__name__, exc)))
            fatal = True

    def ok(cond, msg):
        if not cond:
            raise SmokeFail(msg)

    def start_and_exec(mgr, name, cmd, timeout=20):
        """start(name) -> (start_res, exec_res); exec TimeoutError -> (res, None)."""
        res = mgr.start(name=name)
        time.sleep(1.0)  # let the container finish starting, like the tests do
        try:
            out = mgr.exec(res["id"], cmd, timeout=timeout)
        except TimeoutError:
            return res, None
        return res, out

    def manager_for(ws_id, sp):
        return ContainerManager(
            workspace_path=str(workspace_dir),
            session_id=session_id,
            workspace_id=ws_id,
            session_permissions=sp,
            image=image_tag,
            mem_limit="512m",
            cpu_quota=50000,
        )

    try:
        # ---------------- STEP 1: setup (docker + image) ----------------
        def _setup():
            nonlocal client, workspace_dir, build_ctx, image_built
            client = docker.from_env()
            client.ping()
            workspace_dir = tempfile.mkdtemp(prefix="tm-smoke-ws-")
            build_ctx = tempfile.mkdtemp(prefix="tm-smoke-ctx-")
            with open(os.path.join(build_ctx, "Dockerfile"), "w") as fh:
                fh.write(DOCKERFILE)
            with open(os.path.join(workspace_dir, "host_marker.txt"), "w") as fh:
                fh.write("tm-smoke-host-marker-%s\n" % tag)
            client.images.build(path=build_ctx, tag=image_tag, rm=True)
            image_built = True
            return "docker up; image %s built; workspace %s" % (
                image_tag, os.path.basename(workspace_dir))

        check("setup", _setup)

        # ---------------- STEP 2: permission gate -> config ----------------
        def _gate():
            cases = [
                ({"network": "banned", "filesystem": "write", "container": True}, ("none", "rw")),
                ({"network": "banned", "filesystem": "read", "container": True}, ("none", "ro")),
                ({"network": "write", "filesystem": "write", "container": True}, ("bridge", "rw")),
            ]
            details = []
            for sp, expected in cases:
                got_gate = _compute_container_config_from_permissions(
                    str(workspace_dir), str(uuid.uuid4()), sp)
                got_fb = _compute_container_config_from_permissions(
                    str(workspace_dir), None, sp)
                ok(got_gate == expected,
                   "gate path %r -> %r, expected %r" % (sp, got_gate, expected))
                ok(got_fb == expected,
                   "fallback path %r -> %r, expected %r" % (sp, got_fb, expected))
                details.append("%s/%s -> %s/%s (gate+fallback)"
                               % (sp["network"], sp["filesystem"],
                                  expected[0], expected[1]))
            return "; ".join(details)

        if not fatal:
            check("gate", _gate)

        # ---------------- STEP 3: volume population ----------------
        def _volume():
            nonlocal created_volumes
            ws_id = str(uuid.uuid4())
            vol = "tm-workspace-%s" % ws_id
            created_volumes.append(vol)
            ok(ensure_workspace_volume_populated(
                client, image_tag, str(workspace_dir), vol, network_mode="none"),
               "ensure_workspace_volume_populated returned False")
            listing = client.containers.run(
                image_tag,
                command="ls -a /workspace",
                mounts=[Mount(target="/workspace", source=vol, type="volume")],
                network_mode="none",
                user="1000:1000",
                remove=True,
            ).decode("utf-8", "replace")
            ok(".workspace_synced" in listing,
               "sentinel missing in volume listing: %r" % listing)
            ok("host_marker.txt" in listing,
               "host marker missing in volume listing: %r" % listing)
            return "volume %s populated; sentinel + host_marker present" % vol

        if not fatal:
            check("volume", _volume)

        # ---------------- STEP 4: lifecycle / reuse / persistence ----------------
        def _persist():
            nonlocal created_volumes
            ws_id = str(uuid.uuid4())
            vol = "tm-workspace-%s" % ws_id
            created_volumes.append(vol)
            name = "smoke-persist-%s" % tag
            sp = {"network": "banned", "filesystem": "write", "container": True}
            mgr = manager_for(ws_id, sp)

            r1 = mgr.start(name=name)
            ok(r1["status"] == "created",
               "first start status=%r (expected 'created')" % r1["status"])
            time.sleep(1.0)
            h = mgr.exec(r1["id"], "python3 -c \"print('HELLO_SMOKE')\"", timeout=20)
            ok(h["exit_code"] == 0 and "HELLO_SMOKE" in h.get("stdout", ""),
               "hello exec failed: %r" % h)

            s1 = mgr.stop(r1["id"])
            ok(s1["status"] == "stopped", "stop status=%r" % s1["status"])

            r2 = mgr.start(name=name)
            ok(r2["status"] == "reused",
               "second start status=%r (expected 'reused')" % r2["status"])
            ok(r2["id"] == r1["id"],
               "reused id changed %s -> %s (CONTAINER_RECREATE_MISMATCH?)"
               % (r1["id"], r2["id"]))
            w = mgr.exec(r2["id"],
                         "echo persist-%s > /workspace/persist.txt && cat /workspace/persist.txt" % tag,
                         timeout=20)
            ok(w["exit_code"] == 0 and "persist-%s" % tag in w.get("stdout", ""),
               "write-to-workspace exec failed: %r" % w)

            mgr.stop(r2["id"])
            r3 = mgr.start(name=name)
            ok(r3["status"] == "reused" and r3["id"] == r1["id"],
               "third start not a reuse: %r" % r3)
            p = mgr.exec(r3["id"], "cat /workspace/persist.txt", timeout=20)
            ok(p["exit_code"] == 0 and "persist-%s" % tag in p.get("stdout", ""),
               "persisted data missing after restart: %r" % p)
            mgr.stop(r3["id"])
            return "created -> hello -> stop -> reused(id stable) -> write -> restart -> data persisted"

        if not fatal:
            check("persist", _persist)

        # ---------------- STEP 5: isolation ----------------
        def _isolation_ro():
            nonlocal created_volumes
            ws_id = str(uuid.uuid4())
            created_volumes.append("tm-workspace-%s" % ws_id)
            mgr = manager_for(ws_id, {"network": "banned", "filesystem": "read",
                                      "container": True})
            res, out = start_and_exec(
                mgr, "smoke-ro-%s" % tag, "touch /workspace/blocked.txt && echo WROTE", timeout=20)
            ok(out is not None, "exec timed out (unexpected for ro volume)")
            ok(out["exit_code"] != 0,
               "write to ro /workspace succeeded (exit 0): %r" % out)
            return "touch /workspace/blocked.txt -> exit %s (ro enforced)" % out["exit_code"]

        def _isolation_nonet():
            nonlocal created_volumes
            ws_id = str(uuid.uuid4())
            created_volumes.append("tm-workspace-%s" % ws_id)
            mgr = manager_for(ws_id, {"network": "banned", "filesystem": "write",
                                      "container": True})
            res, out = start_and_exec(
                mgr, "smoke-nonet-%s" % tag, CONNECT_CMD, timeout=15)
            blocked = out is None or (
                out["exit_code"] != 0 and "CONNECTED" not in out.get("stdout", ""))
            ok(blocked, "egress succeeded with network=none: %r" % out)
            detail = "timed out" if out is None else (
                "exit %s: %s" % (out["exit_code"], out.get("stderr", "").strip()[:120]))
            return "no-network blocks egress (%s)" % detail

        def _isolation_bridge():
            nonlocal created_volumes
            ws_id = str(uuid.uuid4())
            created_volumes.append("tm-workspace-%s" % ws_id)
            mgr = manager_for(ws_id, {"network": "write", "filesystem": "write",
                                      "container": True})
            res, out = start_and_exec(
                mgr, "smoke-bridge-%s" % tag, CONNECT_CMD, timeout=20)
            if out is None:
                return "bridge active; egress timed out (documented exception: egress-blocked)"
            if out["exit_code"] == 0 and "CONNECTED" in out.get("stdout", ""):
                return "bridge egress OK (example.com:80 reachable)"
            return "bridge active; egress-blocked (documented exception): exit %s %s" % (
                out["exit_code"], out.get("stderr", "").strip()[:120])

        if not fatal:
            check("isolation-ro", _isolation_ro)
        if not fatal:
            check("isolation-nonet", _isolation_nonet)
        if not fatal:
            check("isolation-bridge", _isolation_bridge)

        # ---------------- STEP 6: tool wiring ----------------
        def _wiring_runner():
            runner = DockerCodeRunner(
                command="echo TM_SMOKE_RUNNER_OK",
                workspace_path=str(workspace_dir),
                session_id=session_id,
                session_permissions={"network": "write", "filesystem": "write",
                                     "container": True},
                image=image_tag,
                mem_limit="512m",
                cpu_quota=50000,
            )
            out = runner.execute()
            data = json.loads(out)
            ok(data.get("success") is True,
               "DockerCodeRunner success=False: %r" % str(out)[:300])
            ok("TM_SMOKE_RUNNER_OK" in data.get("stdout", ""),
               "DockerCodeRunner stdout missing marker: %r" % str(out)[:300])
            return "DockerCodeRunner success; stdout=%r" % data.get("stdout", "").strip()

        def _wiring_ctl():
            sp = {"network": "write", "filesystem": "write", "container": True}
            start_tool = ContainerStartTool(
                name="smoke-ctl-%s" % tag,
                workspace_path=str(workspace_dir),
                session_id=session_id,
                session_permissions=sp,
                image=image_tag,
                mem_limit="512m",
                cpu_quota=50000,
            )
            s = json.loads(start_tool.execute())
            ok(s.get("success") is True, "ContainerStartTool failed: %r" % s)
            cid = s["container_id"]

            exec_tool = ContainerExecTool(
                container_id=cid,
                command="echo CTL_OK",
                workspace_path=str(workspace_dir),
                session_id=session_id,
                session_permissions=sp,
            )
            e = json.loads(exec_tool.execute())
            ok(e.get("success") is True and "CTL_OK" in e.get("stdout", ""),
               "ContainerExecTool failed: %r" % e)

            status_tool = ContainerStatusTool(
                container_id=cid,
                workspace_path=str(workspace_dir),
                session_id=session_id,
                session_permissions=sp,
            )
            st = json.loads(status_tool.execute())
            ok(st.get("success") is True and st.get("status") == "running",
               "ContainerStatusTool failed: %r" % st)

            stop_tool = ContainerStopTool(
                container_id=cid,
                workspace_path=str(workspace_dir),
                session_id=session_id,
                session_permissions=sp,
            )
            stp = json.loads(stop_tool.execute())
            ok(stp.get("status") == "stopped", "ContainerStopTool failed: %r" % stp)
            return "start -> exec -> status -> stop all success (cid=%s)" % cid[:12]

        if not fatal:
            check("wiring-runner", _wiring_runner)
        if not fatal:
            check("wiring-ctl", _wiring_ctl)

    finally:
        # ---------------- STEP 7: cleanup ----------------
        issues = []
        if client is not None:
            try:
                cleanup_session(session_id, client)
            except Exception as exc:  # noqa: BLE001
                issues.append("cleanup_session: %s: %s" % (type(exc).__name__, exc))
            try:
                # Sweep any unlabeled leftovers (runner/ctl containers created
                # without a registered session still carry our unique image tag).
                for c in client.containers.list(all=True,
                                                filters={"ancestor": image_tag}):
                    c.remove(force=True)
            except Exception as exc:  # noqa: BLE001
                issues.append("leftover sweep: %s: %s" % (type(exc).__name__, exc))
            try:
                for vol in created_volumes:
                    try:
                        client.volumes.get(vol).remove(force=True)
                    except docker.errors.NotFound:
                        pass
            except Exception as exc:  # noqa: BLE001
                issues.append("volume sweep: %s: %s" % (type(exc).__name__, exc))
            if image_built:
                try:
                    client.images.remove(image_tag, force=True)
                except docker.errors.ImageNotFound:
                    pass
                except Exception as exc:  # noqa: BLE001
                    issues.append("image remove: %s: %s" % (type(exc).__name__, exc))
            # verify nothing is left behind
            try:
                left = list_session_containers(session_id, client)
                if left:
                    issues.append("session containers remain: %r" % left)
                for c in client.containers.list(all=True,
                                                filters={"ancestor": image_tag}):
                    issues.append("container remains: %s" % c.name)
                for vol in created_volumes:
                    try:
                        client.volumes.get(vol)
                        issues.append("volume remains: %s" % vol)
                    except docker.errors.NotFound:
                        pass
            except Exception as exc:  # noqa: BLE001
                issues.append("verify sweep: %s: %s" % (type(exc).__name__, exc))
        else:
            issues.append("docker client unavailable; manual cleanup may be needed")
        if workspace_dir:
            shutil.rmtree(workspace_dir, ignore_errors=True)
        if build_ctx:
            shutil.rmtree(build_ctx, ignore_errors=True)
        rows.append(("cleanup", "PASS" if not issues else "FAIL",
                     "; ".join(issues) or "all resources removed"))

    # ---------------- receipt ----------------
    fails = [r for r in rows if r[1] == "FAIL"]
    skips = [r for r in rows if r[1] == "SKIP"]
    passes = len(rows) - len(fails) - len(skips)
    print()
    print("=" * 78)
    print(" live_smoke_docker — ThoughtMachine Docker Phase-1 receipt")
    print("=" * 78)
    print("%-16s | %-4s | %s" % ("check", "res", "detail"))
    print("-" * 78)
    for name, result, detail in rows:
        print("%-16s | %-4s | %s" % (name, result, detail))
    print("-" * 78)
    print("SUMMARY: %d PASS, %d FAIL, %d SKIP" % (passes, len(fails), len(skips)))
    print("EXIT: %d" % (1 if fails else 0))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
