"""Contract test: DockerCodeRunner must never mount cwd when workspace resolution fails.

Task 6 (security remediation): the workspace mount path must come from the
workspace registry. When the registry lookup fails (returns None), DockerCodeRunner
must raise DockerSetupError instead of silently falling back to ``os.getcwd()`` —
the current directory may be outside the workspace, and mounting it would leak host
files into the container.

Call path under test:
    DockerCodeRunner.execute()                        (tools/docker_code_runner.py)
      -> self._resolve_registry_workspace()           (tools/base.py:241; patched -> None)
      -> raise DockerSetupError("workspace path could not be resolved; refusing to mount cwd")

``_resolve_registry_workspace`` (inherited from ToolBase) is the seam: it queries
SessionRegistry + WorkspaceRegistry and returns None when no workspace path can be
resolved. It is patched directly, which is the closest seam to the registry lookup
(after the lookup, the only other fallback — the deprecated AgentConfig.workspace_path
field — also lives inside that method, so patching it to None covers "registry
resolution failed" end to end).

DOCKER_AVAILABLE is patched to True so execute() passes the SDK-availability guard
and reaches the resolution step (the code under test runs before any real Docker SDK
use). See tests/security/conftest.py for the sys.path fix that makes the real
``tools.docker_code_runner`` / ``thoughtmachine.security`` importable.
"""
import os
from unittest.mock import patch

import pytest

from tools.docker_code_runner import DockerCodeRunner, DockerSetupError


class TestDockerMountPath:
    def test_raises_docker_setup_error_when_registry_lookup_fails(self):
        """Registry resolution returning None must raise, not fall back to cwd."""
        tool = DockerCodeRunner(command="echo hi")
        with (
            patch.object(DockerCodeRunner, "_resolve_registry_workspace", return_value=None),
            patch("tools.docker_code_runner.DOCKER_AVAILABLE", True),
            patch.object(os, "getcwd", wraps=os.getcwd) as mock_getcwd,
        ):
            with pytest.raises(DockerSetupError) as excinfo:
                tool.execute()

        msg = str(excinfo.value)
        assert "workspace path could not be resolved" in msg
        assert "refusing to mount cwd" in msg
        # The whole point: os.getcwd() must never be consulted as a fallback.
        mock_getcwd.assert_not_called()

    def test_docker_setup_error_reuses_security_layer_exception(self):
        """The tool raises the security layer's DockerSetupError (thoughtmachine.security)."""
        from thoughtmachine.security import DockerSetupError as SecurityDockerSetupError

        assert DockerSetupError is SecurityDockerSetupError
