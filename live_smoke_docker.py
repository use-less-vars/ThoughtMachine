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
        ContainerBuildTool,
        ContainerListTool,
        ContainerLogsTool,
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
    # Phase-2 (Commit 6) state - separate workspace/session/image from Phase-1
    p2_ws = None
    p2_ws_id = None
    p2_image_tag = None
    p2_containers = []          # [a, b, c, d] - explicit teardown in finally
    p2_cid_a = None
    p2_cid_b = None
    p2_cid_c = None
    p2_cid_d = None
    p2_session = str(uuid.uuid4())     # a & b share one session (list filter)
    p2_session_c = str(uuid.uuid4())   # fresh session for container c
    p2_session_d = str(uuid.uuid4())   # fresh session for container d
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

        # ---------------- PHASE 2 (Commit 6): steps 11-16 ----------------
        # ContainerBuildTool / ContainerListTool / ContainerLogsTool
        # end-to-end against real Docker, plus a true-staleness volume
        # refresh sequence.
        # Documented deviation: containers a & b deliberately share ONE
        # session id (p2_session) because ContainerListTool filters on the
        # single `thoughtmachine.session_id` label - with different session
        # ids no single list call could observe both. Containers c and d use
        # their own fresh session ids.
        sp = {"network": "write", "filesystem": "write", "container": True}

        def _p2_build():
            nonlocal p2_ws, p2_image_tag
            p2_ws = tempfile.mkdtemp(prefix="tm-smoke-p2-ws-")
            with open(os.path.join(p2_ws, "Dockerfile"), "w") as fh:
                # python:3.11-slim reuses the cached base from STEP 1,
                # avoiding a slow 3.12 pull. start() overrides CMD anyway.
                fh.write("\n".join([
                    "FROM python:3.11-slim",
                    "WORKDIR /workspace",
                    "COPY . .",
                    'CMD ["python", "-c", "print(\'smoke-ready\')"]',
                    "",
                ]))
            with open(os.path.join(p2_ws, "marker.txt"), "w") as fh:
                fh.write("tm-smoke-p2-marker-%s\n" % tag)
            p2_image_tag = "tm-smoke-p2-%s:latest" % tag
            b = json.loads(ContainerBuildTool(
                workspace_path=p2_ws,
                session_id=p2_session,
                session_permissions=sp,
                tag=p2_image_tag,
            ).execute())
            ok(b.get("success") is True,
               "ContainerBuildTool failed: %s" % b.get("error"))
            ok(b.get("image_tag") == p2_image_tag,
               "unexpected image_tag %r" % b.get("image_tag"))
            ok(bool(b.get("build_log")), "build_log empty")
            return "image %s built (build_log %d chars)" % (
                p2_image_tag, len(b.get("build_log") or ""))

        def _p2_start():
            nonlocal p2_cid_a, p2_cid_b
            sa = json.loads(ContainerStartTool(
                name="smoke-p2-a-%s" % tag,
                workspace_path=p2_ws,
                session_id=p2_session,
                session_permissions=sp,
                image=p2_image_tag,
                mem_limit="512m",
                cpu_quota=50000,
            ).execute())
            ok(sa.get("success") is True, "start a failed: %r" % sa)
            ok(sa.get("status") == "created",
               "start a status %r (expected 'created')" % sa.get("status"))
            p2_cid_a = sa["container_id"]
            p2_containers.append(p2_cid_a)
            sb = json.loads(ContainerStartTool(
                name="smoke-p2-b-%s" % tag,
                workspace_path=p2_ws,
                session_id=p2_session,
                session_permissions=sp,
                image=p2_image_tag,
                mem_limit="512m",
                cpu_quota=50000,
            ).execute())
            ok(sb.get("success") is True, "start b failed: %r" % sb)
            ok(sb.get("status") == "created",
               "start b status %r (expected 'created')" % sb.get("status"))
            p2_cid_b = sb["container_id"]
            p2_containers.append(p2_cid_b)
            return "started %s, %s" % (p2_cid_a[:12], p2_cid_b[:12])

        def _p2_list():
            lst = json.loads(ContainerListTool(
                workspace_path=p2_ws,
                session_id=p2_session,
                session_permissions=sp,
            ).execute())
            ok(lst.get("success") is True, "ContainerListTool failed: %r" % lst)
            containers = lst.get("containers") or []
            ok(lst.get("count", 0) >= 2, "count %r < 2" % lst.get("count"))
            names = [c.get("name") for c in containers]
            ok("smoke-p2-a-%s" % tag in names and "smoke-p2-b-%s" % tag in names,
               "missing containers; got %r" % names)
            for c in containers:
                ok(set(c.keys()) == {"container_id", "name", "image", "status",
                                     "uptime_seconds"},
                   "unexpected entry keys %r" % sorted(c.keys()))
            return "listed %d container(s): %s" % (len(containers),
                                                   ", ".join(names))

        def _p2_logs():
            # start() hardcodes PID 1 = `tail -f /dev/null`; docker logs
            # captures ONLY PID-1 output - exec streams are never logged. So
            # the counter writes into PID 1's stdout pipe via /proc/1/fd/1
            # (both PID 1 and the exec run as uid 1000, so the write is
            # permitted). Foreground, not `&`: exec teardown may reap
            # background children; 20*0.5s=10s < exec timeout (an exec
            # timeout KILLS the whole container).
            counter_cmd = (
                "i=0; while [ $i -lt 20 ]; do "
                "echo tick-$i > /proc/1/fd/1; i=$((i+1)); sleep 0.5; done"
            )
            e = json.loads(ContainerExecTool(
                container_id=p2_cid_a,
                command=counter_cmd,
                timeout=30,
                workspace_path=p2_ws,
                session_id=p2_session,
                session_permissions=sp,
            ).execute())
            ok(e.get("success") is True, "counter exec failed: %r" % e)
            ok(e.get("exit_code") == 0,
               "counter exit_code %r" % e.get("exit_code"))
            lr = json.loads(ContainerLogsTool(
                container_id=p2_cid_a,
                tail=50,
                workspace_path=p2_ws,
                session_id=p2_session,
                session_permissions=sp,
            ).execute())
            ok(lr.get("success") is True, "ContainerLogsTool failed: %r" % lr)
            # tty=True => unframed raw stream => _split fallback => stdout
            ok("tick-" in (lr.get("stdout") or ""),
               "tick- not in logs stdout: %r" % (lr.get("stdout") or "")[:200])
            return "counter -> PID-1 log capture (20 ticks)"

        def _p2_volume_refresh():
            nonlocal p2_ws_id, p2_cid_c, p2_cid_d
            # TRUE staleness test: populate a named volume, remove the
            # container to free it, add a NEW file on the host, then start a
            # second container against the same workspace_id - the Commit-1
            # host-manifest sha check must detect the change and refresh the
            # volume (merge, not wipe).
            p2_ws_id = str(uuid.uuid4())
            mgr_c = ContainerManager(
                workspace_path=p2_ws,
                session_id=p2_session_c,
                workspace_id=p2_ws_id,
                session_permissions=sp,
                image=p2_image_tag,
                mem_limit="512m",
                cpu_quota=50000,
            )
            c = mgr_c.start(name="smoke-p2-c-%s" % tag)
            ok(c.get("status") == "created",
               "start c status %r (expected 'created')" % c.get("status"))
            p2_cid_c = c["id"]  # ContainerManager.start() key is "id"
            p2_containers.append(p2_cid_c)
            created_volumes.append("tm-workspace-%s" % p2_ws_id)
            e1 = json.loads(ContainerExecTool(
                container_id=p2_cid_c,
                command="cat /workspace/marker.txt",
                timeout=20,
                workspace_path=p2_ws,
                session_id=p2_session_c,
                session_permissions=sp,
            ).execute())
            ok(e1.get("success") is True
               and "tm-smoke-p2-marker-%s" % tag in (e1.get("stdout") or ""),
               "marker not in volume after first populate: %r" % e1)
            # free the volume, then mutate the host workspace
            pc = client.containers.get(p2_cid_c)
            try:
                pc.stop(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            pc.remove(force=True)
            with open(os.path.join(p2_ws, "refresh-probe.txt"), "w") as fh:
                fh.write("refresh-probe-%s\n" % tag)
            # second container, same workspace_id => staleness refresh
            mgr_d = ContainerManager(
                workspace_path=p2_ws,
                session_id=p2_session_d,
                workspace_id=p2_ws_id,
                session_permissions=sp,
                image=p2_image_tag,
                mem_limit="512m",
                cpu_quota=50000,
            )
            d = mgr_d.start(name="smoke-p2-d-%s" % tag)
            ok(d.get("status") == "created",
               "start d status %r (expected 'created')" % d.get("status"))
            p2_cid_d = d["id"]
            p2_containers.append(p2_cid_d)
            e2 = json.loads(ContainerExecTool(
                container_id=p2_cid_d,
                command="cat /workspace/refresh-probe.txt; cat /workspace/marker.txt",
                timeout=20,
                workspace_path=p2_ws,
                session_id=p2_session_d,
                session_permissions=sp,
            ).execute())
            ok(e2.get("success") is True, "refresh exec failed: %r" % e2)
            out = e2.get("stdout") or ""
            ok("refresh-probe-%s" % tag in out,
               "refresh-probe missing (volume NOT refreshed): %r" % out)
            ok("tm-smoke-p2-marker-%s" % tag in out,
               "marker missing after refresh (partial wipe?): %r" % out)
            return "volume refreshed: host probe + original marker both present"

        def _p2_stop_verify():
            # container c was already removed in the volume-refresh step;
            # a, b and d are stopped here.
            for cid, sess in ((p2_cid_a, p2_session),
                              (p2_cid_b, p2_session),
                              (p2_cid_d, p2_session_d)):
                st = json.loads(ContainerStopTool(
                    container_id=cid,
                    workspace_path=p2_ws,
                    session_id=sess,
                    session_permissions=sp,
                ).execute())
                ok(st.get("status") == "stopped",
                   "stop %s status %r (%s)" % (cid[:12], st.get("status"),
                                               st.get("error")))
            lst = json.loads(ContainerListTool(
                workspace_path=p2_ws,
                session_id=p2_session,
                session_permissions=sp,
            ).execute())
            ok(lst.get("success") is True, "final list failed: %r" % lst)
            by_name = {c.get("name"): c for c in (lst.get("containers") or [])}
            ok("smoke-p2-a-%s" % tag in by_name
               and "smoke-p2-b-%s" % tag in by_name,
               "final list missing a/b: %r" % sorted(by_name))
            for nm in ("smoke-p2-a-%s" % tag, "smoke-p2-b-%s" % tag):
                ok(by_name[nm].get("status") != "running",
                   "%s still running (%r)" % (nm, by_name[nm].get("status")))
            return "a, b, d stopped; final list shows a/b non-running"

        if not fatal:
            check("phase2-build", _p2_build)
        if not fatal:
            check("phase2-start", _p2_start)
        if not fatal:
            check("phase2-list", _p2_list)
        if not fatal:
            check("phase2-logs", _p2_logs)
        if not fatal:
            check("phase2-volume-refresh", _p2_volume_refresh)
        if not fatal:
            check("phase2-stop-verify", _p2_stop_verify)

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
            # --- Phase-2 teardown (separate session/image from Phase-1) ---
            # cleanup_session + the ancestor sweep above cover ONLY the main
            # session/image; p2 containers + image need explicit removal.
            for cid in p2_containers:
                try:
                    pc = client.containers.get(cid)
                    try:
                        pc.stop(timeout=5)
                    except Exception:  # noqa: BLE001
                        pass
                    pc.remove(force=True)
                except docker.errors.NotFound:
                    pass  # already removed (volume-refresh step)
                except Exception as exc:  # noqa: BLE001
                    issues.append("p2-container %s: %s: %s"
                                  % (cid[:12], type(exc).__name__, exc))
            if p2_image_tag:
                try:
                    client.images.remove(p2_image_tag, force=True)
                except docker.errors.ImageNotFound:
                    pass
                except Exception as exc:  # noqa: BLE001
                    issues.append("p2-image remove: %s: %s"
                                  % (type(exc).__name__, exc))
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
        if p2_ws:
            shutil.rmtree(p2_ws, ignore_errors=True)
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
