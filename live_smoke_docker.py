#!/usr/bin/env python3
"""
live_smoke_docker.py — standalone, host-runnable live end-to-end smoke test
for the ThoughtMachine Docker stack.

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
  3. volume           — host workspace bind-mounted at /workspace: host
                        marker visible immediately; host-side file changes
                        visible in running containers (no population)
  4. persist          — create -> exec -> stop -> REUSE (stable container
                        id, no CONTAINER_RECREATE_MISMATCH) -> write ->
                        stop/restart -> data survives (host-dir persistence)
  5. share            — the SAME workspace_id + name started from a SECOND,
                        different session reuses the SAME container (1 live
                        instance, workspace-label lookup, no recreation)
  6. isolation        — ro workspace blocks writes; no-network blocks
                        egress; bridge grants egress (host-network
                        dependent; documented exception, see below)
  7. wiring           — DockerCodeRunner + ContainerStart/Exec/Status/Stop
                        tools end-to-end via their JSON APIs
  8. cleanup          — session container sweep, volume + image removal

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
    SessionRegistry/WorkspaceRegistry); containers carry the
    thoughtmachine.workspace_id label (the resolved workspace id, or
    "default" for unregistered paths) and are swept by cleanup_workspace.

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
        cleanup_workspace,
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
    # Phase-1 container workspace ids (tracked for cleanup_workspace sweep)
    phase1_ws_ids = []
    # Phase-2 (Commit 6) state - separate workspace/session/image from Phase-1
    p2_ws = None
    p2_ws_id = None
    p2_image_tag = None
    p2_containers = []          # [a, b, c, d] - explicit teardown in finally
    p2_cid_a = None
    p2_cid_b = None
    p2_cid_c = None
    p2_cid_d = None
    p2_cid_note = None                # sticky-note container (phase 2)
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
        if ws_id is not None and ws_id not in phase1_ws_ids:
            phase1_ws_ids.append(ws_id)
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
            # Phase-2: the host workspace is bind-mounted at /workspace (no
            # named volume, no population). A fresh container sees the host
            # marker immediately, and a host-side write is visible in a
            # RUNNING container without any refresh step.
            listing = client.containers.run(
                image_tag,
                command="cat /workspace/host_marker.txt",
                mounts=[Mount(target="/workspace", source=str(workspace_dir),
                              type="bind", read_only=True)],
                network_mode="none",
                user="1000:1000",
                remove=True,
            ).decode("utf-8", "replace")
            ok("tm-smoke-host-marker-%s" % tag in listing,
               "host marker not visible via bind mount: %r" % listing)
            live = client.containers.run(
                image_tag,
                name="smoke-volume-live-%s" % tag,
                mounts=[Mount(target="/workspace", source=str(workspace_dir),
                              type="bind", read_only=True)],
                network_mode="none",
                user="1000:1000",
                detach=True,
                tty=True,
                stdin_open=True,
                command=["tail", "-f", "/dev/null"],
            )
            try:
                probe_name = "volume-live-probe-%s.txt" % tag
                with open(os.path.join(workspace_dir, probe_name), "w") as fh:
                    fh.write("tm-smoke-volume-live-%s\n" % tag)
                exit_code, output = live.exec_run("cat /workspace/%s" % probe_name)
                decoded = (output.decode("utf-8", "replace")
                           if isinstance(output, bytes) else str(output))
                ok(exit_code == 0 and "tm-smoke-volume-live-%s" % tag in decoded,
                   "live host change not visible in running container "
                   "(exit %s, out %r)" % (exit_code, decoded))
            finally:
                try:
                    live.remove(force=True)
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
            return ("bind-mounted workspace: host marker visible; live host "
                    "change visible without recreation")

        if not fatal:
            check("volume", _volume)

        # ---------------- STEP 4: lifecycle / reuse / persistence ----------------
        def _persist():
            ws_id = str(uuid.uuid4())
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

        # ---------------- STEP 5: share-across-sessions ----------------
        def _share_across_sessions():
            # The workspace label (not the session label) is the sharing key:
            # a second manager with a DIFFERENT session_id but the SAME
            # workspace_id + name reuses the existing container instead of
            # creating a second one. Exactly 1 live container must remain.
            ws_id = str(uuid.uuid4())
            name = "smoke-share-%s" % tag
            sp = {"network": "banned", "filesystem": "write", "container": True}
            mgr1 = manager_for(ws_id, sp)
            r1 = mgr1.start(name=name)
            ok(r1["status"] == "created",
               "first start status=%r (expected 'created')" % r1["status"])

            mgr2 = ContainerManager(
                workspace_path=str(workspace_dir),
                session_id=str(uuid.uuid4()),
                workspace_id=ws_id,
                session_permissions=sp,
                image=image_tag,
                mem_limit="512m",
                cpu_quota=50000,
            )
            r2 = mgr2.start(name=name)
            ok(r2["status"] == "reused",
               "second-session start status=%r (expected 'reused')" % r2["status"])
            cid2 = r2.get("container_id") or r2.get("id")
            ok(cid2 == r1["id"],
               "shared container id mismatch: %s vs %s" % (r1["id"], cid2))
            live = client.containers.list(all=True,
                                          filters={"name": "^%s$" % name})
            ok(len(live) == 1,
               "expected exactly 1 live container named %s, found %d"
               % (name, len(live)))
            mgr2.stop(cid2)
            return ("session A created; session B (same workspace_id) reused "
                    "the same container; 1 live instance")

        if not fatal:
            check("share-across-sessions", _share_across_sessions)

        # ---------------- STEP 6: isolation ----------------
        def _isolation_ro():
            ws_id = str(uuid.uuid4())
            mgr = manager_for(ws_id, {"network": "banned", "filesystem": "read",
                                      "container": True})
            res, out = start_and_exec(
                mgr, "smoke-ro-%s" % tag, "touch /workspace/blocked.txt && echo WROTE", timeout=20)
            ok(out is not None, "exec timed out (unexpected for ro volume)")
            ok(out["exit_code"] != 0,
               "write to ro /workspace succeeded (exit 0): %r" % out)
            return "touch /workspace/blocked.txt -> exit %s (ro enforced)" % out["exit_code"]

        def _isolation_nonet():
            ws_id = str(uuid.uuid4())
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
            ws_id = str(uuid.uuid4())
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
        # end-to-end against real Docker, plus a live bind-mount
        # visibility sequence.
        # Documented deviation: containers a & b deliberately share ONE
        # workspace id (the unregistered tmpdir resolves to "default")
        # because ContainerListTool filters on the single
        # `thoughtmachine.workspace_id` label - with different workspace ids
        # no single list call could observe both. Containers c and d use
        # their own fresh workspace ids.
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
                                     "uptime_seconds", "workspace_id", "note"},
                   "unexpected entry keys %r" % sorted(c.keys()))
            return "listed %d container(s): %s" % (len(containers),
                                                   ", ".join(names))

        def _p2_sticky_note():
            nonlocal p2_cid_note
            # Sticky notes: the note label is set at CREATE time; on reuse the
            # note is returned at the response level only. docker SDK 7.1.0 has
            # no label-update API (Container.update forwards unknown kwargs to
            # POST /containers/{id}/update, which ignores them), so on a real
            # daemon the daemon-side label is immutable — we assert the
            # response note, never the list note, for the reuse case.
            s1 = json.loads(ContainerStartTool(
                name="smoke-p2-note-%s" % tag,
                note="smoke-note-1",
                workspace_path=p2_ws,
                session_id=p2_session,
                session_permissions=sp,
                image=p2_image_tag,
                mem_limit="512m", cpu_quota=50000,
            ).execute())
            ok(s1.get("success") is True, "start(note=...) failed: %r" % s1)
            ok(s1.get("status") == "created",
               "start(note) status %r (expected created)" % s1.get("status"))
            ok(s1.get("note") == "smoke-note-1",
               "start(note) note %r (expected smoke-note-1)" % s1.get("note"))
            p2_cid_note = s1["container_id"]
            p2_containers.append(p2_cid_note)
            lst = json.loads(ContainerListTool(
                workspace_path=p2_ws,
                session_id=p2_session,
                session_permissions=sp,
            ).execute())
            ok(lst.get("success") is True, "sticky-note list failed: %r" % lst)
            by_name = {c.get("name"): c for c in (lst.get("containers") or [])}
            ne = by_name.get("smoke-p2-note-%s" % tag)
            ok(ne is not None, "note container missing from list")
            ok(ne.get("note") == "smoke-note-1",
               "list note %r (expected smoke-note-1)" % ne.get("note"))
            s2 = json.loads(ContainerStartTool(
                name="smoke-p2-note-%s" % tag,
                note="smoke-note-2",
                workspace_path=p2_ws,
                session_id=p2_session,
                session_permissions=sp,
                image=p2_image_tag,
                mem_limit="512m", cpu_quota=50000,
            ).execute())
            ok(s2.get("success") is True, "start(reuse,note) failed: %r" % s2)
            ok(s2.get("status") == "reused",
               "reuse status %r (expected reused)" % s2.get("status"))
            ok(s2.get("container_id") == p2_cid_note, "reuse id changed")
            ok(s2.get("note") == "smoke-note-2",
               "reuse note %r (expected smoke-note-2)" % s2.get("note"))
            mgr_note = ContainerManager(
                workspace_path=p2_ws, session_id=p2_session,
                workspace_id="default",
                session_permissions=sp, image=p2_image_tag,
                mem_limit="512m", cpu_quota=50000,
            )
            sn = mgr_note.set_note(p2_cid_note, "smoke-note-3")
            ok(sn.get("success") is True, "set_note failed: %r" % sn)
            ok(sn.get("note") == "smoke-note-3",
               "set_note note %r (expected smoke-note-3)" % sn.get("note"))
            return ("sticky note: create(note-1) -> list note-1 -> "
                    "reuse(note-2 response) -> set_note ok")

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
            # Phase-2 live bind-mount check: the host workspace is mounted
            # directly, so a file written on the host while container c is
            # RUNNING becomes visible inside c immediately (no refresh, no
            # recreation), and a SECOND container (d) started afterwards for
            # the same workspace_id sees the live host tree as well.
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
               "marker not visible in c: %r" % e1)
            # mutate the host workspace while c is RUNNING
            with open(os.path.join(p2_ws, "refresh-probe.txt"), "w") as fh:
                fh.write("refresh-probe-%s\n" % tag)
            e_live = json.loads(ContainerExecTool(
                container_id=p2_cid_c,
                command="cat /workspace/refresh-probe.txt; cat /workspace/marker.txt",
                timeout=20,
                workspace_path=p2_ws,
                session_id=p2_session_c,
                session_permissions=sp,
            ).execute())
            ok(e_live.get("success") is True, "live exec failed: %r" % e_live)
            out_live = e_live.get("stdout") or ""
            ok("refresh-probe-%s" % tag in out_live,
               "host probe not visible in RUNNING container c: %r" % out_live)
            ok("tm-smoke-p2-marker-%s" % tag in out_live,
               "original marker missing in c: %r" % out_live)
            # second container, same workspace_id => live host tree visible
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
            ok(e2.get("success") is True, "d exec failed: %r" % e2)
            out = e2.get("stdout") or ""
            ok("refresh-probe-%s" % tag in out,
               "host probe missing in fresh container d: %r" % out)
            ok("tm-smoke-p2-marker-%s" % tag in out,
               "marker missing in fresh container d: %r" % out)
            return "bind mount live: host probe visible in running c + fresh d"

        def _p2_stop_verify():
            # a, b, c and d are stopped here (c is no longer removed early).
            for cid, sess in ((p2_cid_a, p2_session),
                              (p2_cid_b, p2_session),
                              (p2_cid_c, p2_session_c),
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
            return "a, b, c, d stopped; final list shows a/b non-running"

        if not fatal:
            check("phase2-build", _p2_build)
        if not fatal:
            check("phase2-start", _p2_start)
        if not fatal:
            check("phase2-list", _p2_list)
        if not fatal:
            check("phase2-sticky-note", _p2_sticky_note)
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
                for wid in phase1_ws_ids:
                    cleanup_workspace(wid, client)
            except Exception as exc:  # noqa: BLE001
                issues.append("cleanup_workspace: %s: %s" % (type(exc).__name__, exc))
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
                left = []
                for wid in phase1_ws_ids:
                    left += client.containers.list(
                        all=True,
                        filters={"label": f"thoughtmachine.workspace_id={wid}"},
                    )
                if left:
                    issues.append("workspace containers remain: %r"
                                  % [c.name for c in left])
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
    print(" live_smoke_docker — ThoughtMachine Docker receipt")
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
