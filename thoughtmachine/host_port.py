"""host_port.py — the single host-execution port for ThoughtMachine.

Every process that runs **on the host** (not inside a container) must go
through :func:`host_run`.  ``thoughtmachine/host_port.py`` is the ONLY module in
``thoughtmachine/`` or ``tools/`` permitted to import ``subprocess``; each
caller names the permission *grain* (a resource-catalog category) it is
spending, and the port re-checks identity, the workspace operator kill switch,
the effective grant, credential-shaped env, cwd containment and argv typing --
in that fixed order -- before anything is spawned.

Why this port refuses ``host_bash`` levels other than ``allow``
---------------------------------------------------------------
``tools.host_bash_tool`` may prompt: it has an event bus, a pending-request
queue and an interactive user, so it can honour a session ``host_bash`` grant of
``ask`` by asking for approval (and it keeps its own approval flow and its own
``_audit_log`` -- a separate trail that this port must not merge into).  This
port has none of that machinery, so an ``ask`` grant is *not* something it can
discharge: the only value it can accept for ``host_bash`` is ``allow``.  That is
why ``AUTHORIZED_VALUES["host_bash"] == {"allow"}`` and why an ``ask`` or
``banned`` grant is refused by name instead of silently downgraded.

Layering
--------
This module imports ``subprocess`` (allowed, and only here) and imports
**nothing** from ``tools/``.  Its collaborating imports are:

* vault root seam: ``thoughtmachine.vault.vault_root`` (honours
  ``THOUGHTMACHINE_VAULT_ROOT``),
* identity: ``session.session_registry.SessionRegistry.get_default()``,
* permission resolution: ``thoughtmachine.security.SessionPermissions`` +
  ``security.security_gate.get_workspace_capabilities`` and
  ``security.security_gate.get_effective_permissions``,
* log-root seam for the audit trail: ``agent._log_root.get_log_root``.

The last three are imported lazily (inside their helpers) so that importing this
module never drags the security stack into a process that merely wants the
``HostRunResult`` shape.

Audit trail
-----------
One JSONL file, ``host_port_audit.jsonl``, written under the first of: the
explicit ``audit_dir`` argument, ``$HOST_PORT_AUDIT_DIR``, the vault log root.
Auditing **never raises**: an unwritable or unresolvable audit location costs
the audit line, never the call.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from thoughtmachine.vault import vault_root

__all__ = [
    "AUTHORIZED_VALUES",
    "CREDENTIAL_SHAPED",
    "HostRunHandle",
    "HostRunResult",
    "host_run",
    "host_run_async",
]


#: Permission grains this port can discharge, mapped to the effective-grant
#: values that authorize them.  ``grain`` is a CATEGORY NAME (a key of this
#: mapping, i.e. a ``security/resource_catalog.py`` resource) -- never a
#: ``"category:value"`` pair.  ``network`` deliberately has no ``full`` level
#: (``full`` is mcp vocabulary); ``RESOURCE_CATALOG["network"]`` is
#: ``banned|ask|write|outbound``.
AUTHORIZED_VALUES: Dict[str, frozenset] = {
    # The port cannot prompt (no event bus, no pending-request queue, no
    # interactive user), so a session ``ask`` grant has nowhere to go: only an
    # outright ``allow`` authorizes a host_bash call here.
    "host_bash": frozenset({"allow"}),
    "mcp": frozenset({"connect", "full"}),
    "network": frozenset({"outbound", "write"}),
}

#: Env keys that look credential-shaped.  Check (7) refuses a CALLER-SUPPLIED
#: key matching this pattern unless ``credential_inject=True``; inherited keys
#: are stripped from the child environment under the same flag (W3a, the F-A
#: closure: ``os.environ.copy()`` used to forward them unscanned).
#:
#: The alternatives are SEGMENT-anchored (``(?:^|_)`` ... ``(?:_|$)``) on
#: purpose.  The first cut was the unanchored
#: ``(TOKEN|SECRET|PASSWORD|KEY|PAT|CREDENTIAL)``: its substring semantics made
#: ``PAT`` match ``PATH`` (also ``PYTHONPATH``, ``HOMEPATH``, ``PATHEXT``) and
#: ``KEY`` match any ``*KEY*`` word (``KEYBOARD_LAYOUT``), so the strip
#: silently deleted ``PATH`` from every child environment.  Anchoring to the
#: ``_``/string boundary keeps the real shapes (``GITHUB_TOKEN``,
#: ``X_API_KEY``, ``GITHUB_PAT``) without those false positives.  The trailing
#: ``S?`` is load-bearing as well: it keeps the plural ``CREDENTIALS`` matching
#: (``GOOGLE_APPLICATION_CREDENTIALS``, ``CREDENTIALS_DIR``).
CREDENTIAL_SHAPED = re.compile(
    r"(?:^|_)(?:TOKEN|SECRET|PASSWORD|KEY|PAT|CREDENTIAL)S?(?:_|$)", re.I
)

#: Audit trail filename (documented contract; the tests pin it).
AUDIT_FILENAME = "host_port_audit.jsonl"

#: Env var naming an audit directory when no explicit ``audit_dir`` is passed.
AUDIT_DIR_ENV = "HOST_PORT_AUDIT_DIR"

#: Grace period between SIGTERM and SIGKILL of a timed-out process group.
_KILL_GRACE_SECONDS = 5.0

#: Exit code reported for a timed-out call.
TIMEOUT_EXIT_CODE = -9


# ═══════════════════════════════════════════════════════════════════════════
#  Result / handle types
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class HostRunResult:
    """Outcome of one :func:`host_run` call.

    ``grain`` records which permission grain was spent.  ``ok`` is the single
    success predicate: a clean exit AND no timeout (a timed-out call reports
    ``exit_code == -9``, which must never be mistaken for success).
    """

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    argv: List[str]
    grain: str

    @property
    def ok(self) -> bool:
        """``True`` only for a completed run whose exit status was 0."""
        return self.exit_code == 0 and not self.timed_out


class HostRunHandle:
    """Handle for an asynchronous host run -- **not implemented yet**.

    The synchronous :func:`host_run` is complete; the asynchronous surface is
    deliberately a signature-only stub so callers can be written against the
    eventual shape without a second migration later.
    """

    def poll(self) -> "HostRunResult":
        raise NotImplementedError(
            "HostRunHandle.poll is not implemented yet: asynchronous host "
            "execution is not owned by W3 (W3 is the credential env "
            "plumbing) and has no owning wave yet"
        )

    def wait(self, timeout: Optional[float] = None) -> "HostRunResult":
        raise NotImplementedError(
            "HostRunHandle.wait is not implemented yet: asynchronous host "
            "execution is not owned by W3 (W3 is the credential env "
            "plumbing) and has no owning wave yet"
        )

    def kill(self) -> None:
        raise NotImplementedError(
            "HostRunHandle.kill is not implemented yet: asynchronous host "
            "execution is not owned by W3 (W3 is the credential env "
            "plumbing) and has no owning wave yet"
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Small helpers: time, workspace config, audit
# ═══════════════════════════════════════════════════════════════════════════


def _timestamp() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _workspace_config(workspace_id: str) -> dict:
    """Return ``<vault>/workspaces/<id>/config.json`` as a dict, else ``{}``.

    One read serves two keys (the ``allow_host_resources`` kill switch and the
    ``root`` used for cwd containment), so the two can never disagree.  Never
    raises: a missing/undecodable/non-dict config is an empty dict, i.e. the
    fail-closed answer for both keys.
    """
    try:
        path = Path(vault_root()) / "workspaces" / str(workspace_id) / "config.json"
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _workspace_root(config: Mapping) -> Optional[str]:
    """Return the real path of the workspace ``root`` key, or ``None``."""
    raw = config.get("root")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return os.path.realpath(raw)
    except (OSError, ValueError):
        return None


def _audit_path(audit_dir: Any) -> Optional[Path]:
    """Resolve the audit file path, or ``None`` when auditing is unavailable."""
    directory: Optional[Path] = None
    if audit_dir is not None:
        try:
            directory = Path(os.fspath(audit_dir))
        except (TypeError, ValueError):
            directory = None
    if directory is None:
        env_dir = os.environ.get(AUDIT_DIR_ENV)
        if env_dir:
            directory = Path(env_dir)
    if directory is None:
        try:
            from agent._log_root import get_log_root

            root = get_log_root()
            if root is not None:
                directory = Path(os.fspath(root))
        except Exception:  # pragma: no cover - import/environment dependent
            directory = None
    if directory is None:
        return None
    return directory / AUDIT_FILENAME


def _write_audit(audit_dir: Any, record: Dict[str, Any]) -> None:
    """Append one JSON line to the audit trail.  Never raises."""
    try:
        path = _audit_path(audit_dir)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
    except Exception:  # pragma: no cover - auditing is best-effort by contract
        return


def _argv_preview(command: Any, shell: bool) -> Optional[List[str]]:
    """Best-effort argv for a refusal audit line (validation has not run yet)."""
    try:
        if isinstance(command, str):
            return [command]
        if isinstance(command, Sequence):
            return [item if isinstance(item, str) else str(item) for item in command]
    except Exception:  # pragma: no cover - exotic sequence
        return None
    return None


def _refuse(
    reason: str,
    *,
    workspace_id: Any,
    session_id: Any,
    grain: Any,
    argv: Optional[List[str]],
    audit_dir: Any,
) -> None:
    """Write one ``refused`` audit line, then raise ``PermissionError``."""
    record: Dict[str, Any] = {
        "timestamp": _timestamp(),
        "event": "refused",
        "workspace_id": workspace_id,
        "session_id": session_id,
        "grain": grain,
        "decision": "deny",
        "reason": reason,
    }
    if argv is not None:
        record["argv"] = argv
    _write_audit(audit_dir, record)
    raise PermissionError(reason)


# ═══════════════════════════════════════════════════════════════════════════
#  Check steps (fixed order — see host_run)
# ═══════════════════════════════════════════════════════════════════════════


def _check_identity(
    workspace_id: str,
    session_id: str,
    effective_permissions: Optional[Mapping],
    *,
    grain: str,
    argv: Optional[List[str]],
    audit_dir: Any,
) -> None:
    """Refuse a session recorded against a different workspace.

    An unknown session is acceptable ONLY when the caller injected the
    effective permissions (an id-less/test caller with its own authority);
    otherwise the call fails closed, because the port has nothing to resolve
    grants from.
    """
    record: Any = None
    try:
        from session.session_registry import SessionRegistry

        record = SessionRegistry.get_default().get(session_id)
    except Exception:
        record = None

    if record is None:
        if effective_permissions is None:
            _refuse(
                f"session {session_id} is not registered and no "
                "effective_permissions were supplied; cannot resolve grants "
                "for workspace "
                f"{workspace_id} -- failing closed",
                workspace_id=workspace_id,
                session_id=session_id,
                grain=grain,
                argv=argv,
                audit_dir=audit_dir,
            )
        return

    if isinstance(record, Mapping):
        recorded_workspace = record.get("workspace_id")
    else:  # pragma: no cover - defensive: registry rows are dicts
        recorded_workspace = getattr(record, "workspace_id", None)

    if recorded_workspace != workspace_id:
        _refuse(
            f"session {session_id} does not belong to workspace "
            f"{workspace_id}: it is registered against workspace "
            f"{recorded_workspace}",
            workspace_id=workspace_id,
            session_id=session_id,
            grain=grain,
            argv=argv,
            audit_dir=audit_dir,
        )


def _resolve_permissions(
    workspace_id: str,
    session_id: str,
    effective_permissions: Optional[Mapping],
    *,
    grain: str,
    argv: Optional[List[str]],
    audit_dir: Any,
) -> Dict[str, Any]:
    """Return the effective permission map, or refuse (fail-closed).

    Injected ``effective_permissions`` win; otherwise the grants are resolved
    from the vault for ``(workspace_id, session_id)`` through the security
    gate's disk mode.  ANY failure there is a refusal, never a default grant.
    """
    if effective_permissions is not None:
        try:
            return dict(effective_permissions)
        except (TypeError, ValueError) as exc:
            _refuse(
                f"effective_permissions could not be read as a mapping: {exc}",
                workspace_id=workspace_id,
                session_id=session_id,
                grain=grain,
                argv=argv,
                audit_dir=audit_dir,
            )

    try:
        from security.security_gate import (
            get_effective_permissions,
            get_workspace_capabilities,
        )
        from thoughtmachine.security import SessionPermissions

        return dict(
            get_effective_permissions(
                SessionPermissions(),
                get_workspace_capabilities(workspace_id),
                session_id=session_id,
                workspace_id=workspace_id,
            )
        )
    except Exception as exc:
        _refuse(
            "could not resolve effective permissions for workspace "
            f"{workspace_id} / session {session_id} ({type(exc).__name__}: "
            f"{exc}); failing closed",
            workspace_id=workspace_id,
            session_id=session_id,
            grain=grain,
            argv=argv,
            audit_dir=audit_dir,
        )
    raise AssertionError("unreachable")  # pragma: no cover


def _resolve_cwd(
    cwd: Any,
    workspace_root: Optional[str],
    *,
    workspace_id: str,
    session_id: str,
    grain: str,
    argv: Optional[List[str]],
    audit_dir: Any,
) -> str:
    """Return the real cwd, refusing anything outside the workspace root."""
    if workspace_root is None:
        _refuse(
            f"workspace {workspace_id} has no usable 'root' in its config.json; "
            "cannot contain cwd -- failing closed",
            workspace_id=workspace_id,
            session_id=session_id,
            grain=grain,
            argv=argv,
            audit_dir=audit_dir,
        )

    if cwd is None:
        return workspace_root

    try:
        resolved = os.path.realpath(os.fspath(cwd))
    except (TypeError, ValueError) as exc:
        _refuse(
            f"cwd {cwd!r} is not a usable path ({exc})",
            workspace_id=workspace_id,
            session_id=session_id,
            grain=grain,
            argv=argv,
            audit_dir=audit_dir,
        )

    if resolved == workspace_root or resolved.startswith(
        workspace_root + os.sep
    ):
        return resolved

    _refuse(
        f"cwd {cwd!r} resolves to {resolved!r}, which is outside the workspace "
        f"root {workspace_root!r}",
        workspace_id=workspace_id,
        session_id=session_id,
        grain=grain,
        argv=argv,
        audit_dir=audit_dir,
    )
    raise AssertionError("unreachable")  # pragma: no cover


def _build_argv(command: Any, shell: bool) -> List[str]:
    """Validate/normalise *command* into an argv list (``TypeError`` on misuse).

    ``shell=False`` (the default) is list form only: a string is a bug, not a
    convenience -- ``"echo a;b"`` silently splitting on the shell metacharacter
    is exactly the injection this port exists to prevent.  ``shell=True``
    accepts a command string or an all-string sequence (``Popen`` reads the
    first element as the command line).
    """
    if shell:
        if isinstance(command, str):
            return [command]
        if isinstance(command, Sequence):
            argv = list(command)
            if not all(isinstance(item, str) for item in argv):
                raise TypeError(
                    "shell=True requires every element of the command "
                    f"sequence to be a str; got {command!r}"
                )
            return argv
        raise TypeError(
            "shell=True requires a command string or a sequence of strings; "
            f"got {type(command).__name__}"
        )

    if isinstance(command, str):
        raise TypeError(
            "shell=False requires a sequence of arguments (argv list), not a "
            f"command string; got {command!r}. Split it explicitly."
        )
    if not isinstance(command, Sequence):
        raise TypeError(
            "shell=False requires a sequence of arguments (argv list); got "
            f"{type(command).__name__}"
        )
    argv = list(command)
    if not all(isinstance(item, str) for item in argv):
        raise TypeError(
            f"shell=False requires every argv element to be a str; got {command!r}"
        )
    return argv


# ═══════════════════════════════════════════════════════════════════════════
#  Process-group termination
# ═══════════════════════════════════════════════════════════════════════════


def _signal_group(proc: "subprocess.Popen[str]", sig: int) -> None:
    """Send *sig* to the whole process group of *proc* (never raises).

    The child is started with ``start_new_session=True``, so its process group
    id equals its pid; signalling the group kills grandchildren the child
    backgrounded, which is what a timeout must guarantee (no survivors).
    """
    try:
        pgid: Optional[int] = os.getpgid(proc.pid)
    except OSError:  # pragma: no cover - already reaped
        pgid = None

    if pgid is None:
        try:
            proc.kill()
        except OSError:  # pragma: no cover
            pass
        return

    try:
        os.killpg(pgid, sig)
    except OSError:  # pragma: no cover - group already gone
        pass


# ═══════════════════════════════════════════════════════════════════════════
#  The port
# ═══════════════════════════════════════════════════════════════════════════


def host_run(
    command: Any,
    *,
    workspace_id: str,
    session_id: str,
    grain: str,
    shell: bool = False,
    timeout: float = 60,
    cwd: Any = None,
    env: Optional[Mapping] = None,
    credential_inject: bool = False,
    client_type: Optional[str] = None,
    effective_permissions: Optional[Mapping] = None,
    audit_dir: Any = None,
) -> HostRunResult:
    """Run *command* on the host, subject to the full permission contract.

    The checks below run in a FIXED order; each one can only refuse:

    1. ``grain`` is a known grain (a key of :data:`AUTHORIZED_VALUES`);
    2. ``workspace_id`` and ``session_id`` are non-empty strings;
    3. the session registry does not put ``session_id`` in another workspace;
    4. the workspace operator kill switch ``allow_host_resources`` is JSON ``true``;
    5. the effective grant for ``grain`` is one of its authorized values;
    6. ``grain == "mcp"`` requires ``client_type == "mcp"``;
    7. no supplied ``env`` key looks credential-shaped, unless
       ``credential_inject=True``;
    7b. every supplied ``env`` value is a plain ``str`` when
       ``credential_inject=True`` (``TypeError``, not a refusal);
    8. ``cwd`` (default: the workspace root) resolves inside the workspace root;
    9. ``shell``/``command`` are a sane pair (``TypeError``, not a refusal).

    ``credential_inject`` is the credential path's explicit opt-in, and it
    gates BOTH sides of the credential boundary:

    * caller-supplied credential-shaped keys are refused by check (7) when it
      is ``False`` (unchanged behaviour, unchanged reason) and forwarded when
      it is ``True``;
    * INHERITED credential-shaped keys -- the ones check (7) never looked at --
      are stripped from the child environment when it is ``False`` and their
      NAMES are recorded in the PRE audit line under ``stripped_env_keys``;
      when it is ``True`` they are forwarded and the PRE line records
      ``credential_inject: true`` so the bypass is auditable.

    Env VALUES are never written to the audit trail, a log, or an exception
    message -- key NAMES only.

    Permitted calls are audited PRE/POST around the execution.  ``shell`` is
    off by default; a timed-out call kills the whole process group (SIGTERM,
    then SIGKILL after :data:`_KILL_GRACE_SECONDS`) and reports
    ``exit_code == -9`` / ``timed_out=True``.
    """
    preview = _argv_preview(command, shell)

    # (1) grain vocabulary -------------------------------------------------
    if grain not in AUTHORIZED_VALUES:
        _refuse(
            f"unknown permission grain {grain!r}; this port only accepts the "
            f"resource-catalog grains {sorted(AUTHORIZED_VALUES)} (a grain is a "
            "category name, never 'category:value')",
            workspace_id=workspace_id,
            session_id=session_id,
            grain=grain,
            argv=preview,
            audit_dir=audit_dir,
        )

    # (2) identity present -------------------------------------------------
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        _refuse(
            "workspace_id must be a non-empty workspace id string; host "
            "execution has no authority without a workspace",
            workspace_id=workspace_id,
            session_id=session_id,
            grain=grain,
            argv=preview,
            audit_dir=audit_dir,
        )
    if not isinstance(session_id, str) or not session_id.strip():
        _refuse(
            "session_id must be a non-empty session id string; host execution "
            "has no authority without a session",
            workspace_id=workspace_id,
            session_id=session_id,
            grain=grain,
            argv=preview,
            audit_dir=audit_dir,
        )

    # (3) identity consistency --------------------------------------------
    _check_identity(
        workspace_id,
        session_id,
        effective_permissions,
        grain=grain,
        argv=preview,
        audit_dir=audit_dir,
    )

    # (4) operator kill switch (fail-closed: only JSON true enables) ------
    config = _workspace_config(workspace_id)
    if config.get("allow_host_resources") is not True:
        _refuse(
            f"workspace {workspace_id} does not allow host resources: "
            "'allow_host_resources' is not true in "
            f"<vault>/workspaces/{workspace_id}/config.json (operator kill "
            "switch; fail-closed)",
            workspace_id=workspace_id,
            session_id=session_id,
            grain=grain,
            argv=preview,
            audit_dir=audit_dir,
        )

    # (5) effective grant for this grain ----------------------------------
    permissions = _resolve_permissions(
        workspace_id,
        session_id,
        effective_permissions,
        grain=grain,
        argv=preview,
        audit_dir=audit_dir,
    )
    granted = permissions.get(grain)
    if not isinstance(granted, str) or granted not in AUTHORIZED_VALUES[grain]:
        detail = (
            " this port cannot prompt (no event bus, queue or interactive "
            "user), so an 'ask' grant has nowhere to go"
            if grain == "host_bash"
            else ""
        )
        _refuse(
            f"permission grain {grain!r} is not authorized for host execution: "
            f"the effective grant is {granted!r} but this port requires one of "
            f"{sorted(AUTHORIZED_VALUES[grain])}.{detail}",
            workspace_id=workspace_id,
            session_id=session_id,
            grain=grain,
            argv=preview,
            audit_dir=audit_dir,
        )

    # (6) mcp grain needs an mcp client -----------------------------------
    if grain == "mcp" and client_type != "mcp":
        _refuse(
            "grain 'mcp' requires client_type='mcp' so the call is attributable "
            f"to an MCP client; got client_type={client_type!r}",
            workspace_id=workspace_id,
            session_id=session_id,
            grain=grain,
            argv=preview,
            audit_dir=audit_dir,
        )

    # (7) no credential-shaped env ---------------------------------------
    # ``credential_inject=True`` is the credential path's explicit opt-in: the
    # caller has resolved the value from the vault (``CredentialInjector``)
    # and passes it as a plain ``str``.  With the default ``False`` this
    # refusal keeps its historical behaviour and message -- a caller cannot
    # smuggle a credential-shaped key in by omission.
    if env and not credential_inject:
        for key in env:
            if CREDENTIAL_SHAPED.search(str(key)):
                _refuse(
                    f"env key {key} looks credential-shaped; use "
                    "credential_inject=True (W3)",
                    workspace_id=workspace_id,
                    session_id=session_id,
                    grain=grain,
                    argv=preview,
                    audit_dir=audit_dir,
                )

    # (7b) credential_inject demands plain-str env values ----------------
    # ``Secret`` is a ``str`` SUBCLASS whose ``__str__`` returns ``***``, so a
    # bare ``isinstance(value, str)`` check would pass and the ``str(value)``
    # coercion below would hand the mask to the child.  Reject anything that is
    # not an exact ``str`` so the caller must unwrap deliberately.
    if credential_inject and env:
        for key, value in env.items():
            if type(value) is not str:
                raise TypeError(
                    f"env value for key {key!r} must be a plain str when "
                    f"credential_inject=True; got {type(value).__name__}. A "
                    "Secret (a str subclass) stringifies to '***', so coercing "
                    "it would send the mask instead of the credential -- "
                    "unwrap it at the call site first."
                )

    # (8) cwd containment -------------------------------------------------
    cwd_resolved = _resolve_cwd(
        cwd,
        _workspace_root(config),
        workspace_id=workspace_id,
        session_id=session_id,
        grain=grain,
        argv=preview,
        audit_dir=audit_dir,
    )

    # (9) argv typing -----------------------------------------------------
    argv = _build_argv(command, shell)

    # ── environment: the F-A closure (W3a) ───────────────────────────────
    # ``os.environ.copy()`` forwards every INHERITED key to the child.  Check
    # (7) only ever inspected CALLER-supplied keys, so a credential-shaped
    # variable already present in the parent environment (an exported API
    # token, a CI job token, a cloud secret key) reached the child unscanned.
    # With ``credential_inject=False`` those keys are now removed from the
    # copy and only their NAMES are audited; ``True`` forwards them and
    # records the bypass in the PRE line.
    merged_env = os.environ.copy()
    stripped_env_keys: List[str] = []
    if not credential_inject:
        for inherited_key in [k for k in merged_env if CREDENTIAL_SHAPED.search(k)]:
            del merged_env[inherited_key]
            stripped_env_keys.append(inherited_key)
        stripped_env_keys.sort()

    # ── permitted: audit PRE, execute, audit POST ─────────────────────────
    started = time.monotonic()
    _write_audit(
        audit_dir,
        {
            "timestamp": _timestamp(),
            "event": "pre",
            "workspace_id": workspace_id,
            "session_id": session_id,
            "grain": grain,
            "argv": argv,
            "shell": shell,
            "timeout": timeout,
            "cwd": cwd_resolved,
            "decision": "allow",
            "credential_inject": credential_inject,
            "stripped_env_keys": stripped_env_keys,
        },
    )

    if env:
        merged_env.update({str(key): str(value) for key, value in env.items()})

    proc: "subprocess.Popen[str]" = subprocess.Popen(
        argv,
        shell=shell,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd_resolved,
        env=merged_env,
        start_new_session=True,
    )

    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _signal_group(proc, signal.SIGTERM)
        try:
            stdout, stderr = proc.communicate(timeout=_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _signal_group(proc, signal.SIGKILL)
            stdout, stderr = proc.communicate()

    if timed_out:
        exit_code = TIMEOUT_EXIT_CODE
        duration_ms = int(timeout * 1000)
    else:
        exit_code = proc.returncode
        duration_ms = int((time.monotonic() - started) * 1000)

    _write_audit(
        audit_dir,
        {
            "timestamp": _timestamp(),
            "event": "post",
            "workspace_id": workspace_id,
            "session_id": session_id,
            "grain": grain,
            "argv": argv,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
        },
    )

    return HostRunResult(
        exit_code=exit_code,
        stdout=stdout or "",
        stderr=stderr or "",
        duration_ms=duration_ms,
        timed_out=timed_out,
        argv=argv,
        grain=grain,
    )


def host_run_async(
    command: Any,
    *,
    workspace_id: str,
    session_id: str,
    grain: str,
    shell: bool = False,
    timeout: float = 60,
    cwd: Any = None,
    env: Optional[Mapping] = None,
    credential_inject: bool = False,
    client_type: Optional[str] = None,
    effective_permissions: Optional[Mapping] = None,
    audit_dir: Any = None,
) -> HostRunHandle:
    """Asynchronous variant of :func:`host_run` -- **not implemented yet**.

    The signature is final so callers can be written against it, including
    ``credential_inject`` (which the eventual body must apply exactly as
    :func:`host_run` does).  ASYNC EXECUTION IS NOT W3: W3 is the credential
    env plumbing, which ships without it.  No wave owns async execution yet,
    so the ``(W3)`` ownership claim this docstring used to carry was wrong.
    """
    raise NotImplementedError(
        "host_run_async is not implemented yet: asynchronous host execution is "
        "NOT owned by W3 (W3 is the credential env plumbing shipped in this "
        "wave); it has no owning wave yet"
    )
