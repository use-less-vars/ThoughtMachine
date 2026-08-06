"""
sandboxed_execution.py -- Hardened subprocess runner for the ThoughtMachine agent.

Runs an external command with a minimal, predictable environment and optional
permission gating, so callers never have to trust shell metacharacters or
ambient environment variables.

Design goals
------------
1. **Fail closed**: any ``required_category`` that the session permissions do
   not explicitly satisfy raises ``PermissionError`` *before* anything runs.
2. **No shell**: commands are executed as argument lists via
   ``subprocess.run(..., shell=False)``; shell metacharacters in arguments are
   rejected up front unless the caller explicitly opts in (``allow_shell=True``
   -- note that ``shell=True`` is NEVER used regardless).
3. **Hermetic environment**: by default the child sees only ``HOME=/dev/null``
   and a fixed ``PATH`` (plus git hardening for git commands), blocking
   ambient config/alias/credential injection.
"""

from __future__ import annotations

import getpass
import os
import subprocess
from typing import Any, Dict, List, Optional

from security.security_gate import _value_satisfies

# Shell metacharacters rejected when allow_shell=False.
# '&&'/'||' are matched before their single-character components so an
# argument containing '&&' is flagged as a single unit.
_SHELL_METACHARS = ("&&", "||", ";", "|", "`", "$(")

# Minimal, predictable environment for stripped execution.
_STRIPPED_ENV = {
    "HOME": "/dev/null",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
}

# Extra hardening for git subprocesses (mirrors tools/git_info_tool.py).
_GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
}


class SandboxedExecution:
    """
    Permission-gated, shell-free subprocess runner.

    Args:
        session_permissions:
            Dict of permission category -> allowed value (e.g. the output of
            ``SessionPermissions.to_dict()`` or ``get_effective_permissions``).
        workspace_id:
            Optional workspace identifier (informational; reserved for future
            workspace-capability merging).
        logger:
            Optional ``logging.Logger`` used to emit an audit line (command
            name and cwd only -- never the full argument list).
    """

    def __init__(
        self,
        session_permissions: Optional[Dict[str, Any]],
        workspace_id: Optional[str] = None,
        logger: Any = None,
    ):
        self.session_permissions = session_permissions
        self.workspace_id = workspace_id
        self.logger = logger

    def run(
        self,
        command: List[str],
        *,
        cwd: Optional[str] = None,
        timeout: float = 30,
        allow_shell: bool = False,
        required_category: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
        strip_env: bool = True,
        input: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        """
        Execute ``command`` under the sandbox policy.

        Args:
            command: The argv list to execute (never interpreted by a shell).
            cwd: Working directory for the child process.
            timeout: Seconds before the process is killed and
                ``subprocess.TimeoutExpired`` is raised.
            allow_shell: When False (default), any argument containing a shell
                metacharacter raises ``ValueError``. When True, the metachar
                scan is skipped -- but execution is STILL shell-free.
            required_category: ``'<category>:<level>'`` requirement. If set and
                not satisfied by ``session_permissions``, raises
                ``PermissionError`` before anything runs.
            extra_env: Environment variables merged on top of the computed
                environment (overrides stripped defaults).
            strip_env: When True (default), the child inherits only a minimal
                environment (``HOME=/dev/null`` + fixed ``PATH``, plus git
                hardening for ``git`` commands). When False, the child inherits
                ``os.environ``.
            input: Optional string written to the child's stdin (text mode --
                ``subprocess.run(..., text=True)``). ``None`` (default) leaves
                stdin unconnected, preserving existing caller behaviour.

        Returns:
            ``subprocess.CompletedProcess`` from ``subprocess.run``.

        Raises:
            PermissionError: if ``required_category`` is set and not satisfied.
            ValueError: if ``command`` is empty/non-list, or if
                ``allow_shell`` is False and an argument contains a shell
                metacharacter.
            subprocess.TimeoutExpired: if the process exceeds ``timeout``
                (propagated from ``subprocess.run``).
        """
        if not command or not isinstance(command, list):
            raise ValueError("command must be a non-empty list of strings")

        # ---- 1. Permission check (fail closed) ----
        if required_category is not None:
            self._check_permission(required_category)

        # ---- 2. Shell metacharacter rejection ----
        if not allow_shell:
            self._reject_shell_metachars(command)

        # ---- 3. Environment ----
        env = self._build_env(command, strip_env=strip_env, extra_env=extra_env)

        # ---- 4. Audit log (command name only -- never args) ----
        if self.logger is not None:
            self.logger.info(
                "SandboxedExecution: %s in %s (user=%s)",
                command[0],
                cwd or os.getcwd(),
                getpass.getuser(),
            )

        # ---- 5. Execute (never shell=True) ----
        return subprocess.run(
            command,
            cwd=cwd,
            timeout=timeout,
            env=env,
            capture_output=True,
            text=True,
            input=input,
        )

    # -- internal helpers ------------------------------------------------

    def _check_permission(self, required_category: str) -> None:
        """Enforce a ``'category:level'`` requirement against session perms."""
        if not isinstance(required_category, str) or ":" not in required_category:
            raise PermissionError(
                f"Malformed required category: {required_category!r}"
            )
        category, required_level = required_category.split(":", 1)
        if not self.session_permissions or category not in self.session_permissions:
            raise PermissionError(
                f"Permission denied: unknown or missing category {category!r}"
            )
        allowed = self.session_permissions.get(category)
        result = _value_satisfies(required_level, allowed)
        if result is False or result == "ASK":
            raise PermissionError(
                f"Permission denied: requires {required_category}, "
                f"but session allows {category}:{allowed}"
            )

    def _reject_shell_metachars(self, command: List[str]) -> None:
        """Raise ``ValueError`` if any command string contains metacharacters."""
        offending = []
        for arg in command:
            for token in _SHELL_METACHARS:
                if token in arg:
                    offending.append(arg)
                    break
        if offending:
            raise ValueError(
                "Shell metacharacters are not allowed in arguments: "
                + ", ".join(repr(a) for a in offending)
            )

    def _build_env(
        self,
        command: List[str],
        *,
        strip_env: bool,
        extra_env: Optional[Dict[str, str]],
    ) -> Dict[str, str]:
        """Compute the child environment, then merge ``extra_env`` on top."""
        if strip_env:
            env: Dict[str, str] = dict(_STRIPPED_ENV)
            if command[0] == "git":
                env.update(_GIT_ENV)
        else:
            env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        return env
