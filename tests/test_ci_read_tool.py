"""Tests for the read-only CI (GitHub Actions) tool ``CiReadTool`` (``ci_read``).

Hermetic by construction: the tool's SOLE egress seam
(``tools.ci_read_tool._http_get``) and its owner/repo resolution
(``CiReadTool._resolve_owner_repo``) are monkeypatched in every test that could
otherwise reach the network, so NO live request is ever made.  The captured URL
and headers are asserted to prove the containment posture: exactly one request
per call, and NO ``Authorization``/token is ever sent.

Registration tests pin the REAL posture: ``ci_read`` is a first-class tool
(``TOOL_CLASSES`` / ``SIMPLIFIED_TOOL_CLASSES``) and is deliberately absent from
the ``AGENT_TOOLS`` / ``ENGINEER_TOOLS`` presets.  That omission does NOT make it
default-off: the default ``enabled_tools`` is derived from
``SIMPLIFIED_TOOL_CLASSES`` (agent/config/models.py default_factory), which
contains ``ci_read``, so the preset omission alone does not exclude it from the
derived default.  The default-off guard lives in the permission layer: the
``ci`` grain defaults to ``banned``, so the read is denied fail-closed until a
session grants ``ci:read``.
"""

import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import tools.ci_read_tool as ci_read_tool
from tools.ci_read_tool import CiReadTool, parse_origin_owner_repo
from tools import TOOL_CLASSES, SIMPLIFIED_TOOL_CLASSES
from tools.utils import model_to_openai_tool

from session.tool_presets import AGENT_TOOLS, ENGINEER_TOOLS, _ALL_TOOLS

from thoughtmachine.security import SessionPermissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities
from security.security_gate import (
    check_required_categories,
    get_effective_permissions,
)

_OWNER_REPO = ("octocat", "hello-world")


class _Recorder:
    """Monkeypatch stand-in for ``_http_get`` that records every call."""

    def __init__(self, payload=None):
        self.calls = []
        self.payload = payload if payload is not None else {"workflow_runs": []}

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        return self.payload


def _install(monkeypatch, payload=None):
    """Patch the egress seam + owner/repo resolution; return the recorder."""
    recorder = _Recorder(payload)
    monkeypatch.setattr(ci_read_tool, "_http_get", recorder)
    monkeypatch.setattr(
        CiReadTool, "_resolve_owner_repo", lambda self: _OWNER_REPO
    )
    return recorder


def _eff(session=None):
    return get_effective_permissions(
        session if session is not None else SessionPermissions(),
        WorkspaceCapabilities(),
    )


# ---------------------------------------------------------------------------
# Identity / permission-category declaration
# ---------------------------------------------------------------------------


def test_tool_name_is_ci_read():
    assert CiReadTool.tool_name() == "ci_read"


def test_required_categories_is_ci_read_exactly():
    assert CiReadTool.get_required_categories() == ["ci:read"]
    assert CiReadTool.get_required_categories({}) == ["ci:read"]


def test_ci_read_requirement_allowed_with_grant():
    eff = get_effective_permissions(
        SessionPermissions(ci="read"), WorkspaceCapabilities()
    )
    ok, msg = check_required_categories(
        CiReadTool.get_required_categories(),
        eff,
        "CiReadTool",
        {},
        "read ci",
        event_bus=None,
    )
    assert ok is True and msg == ""


def test_ci_read_requirement_denied_without_grant():
    # ci defaults to banned.
    ok, msg = check_required_categories(
        CiReadTool.get_required_categories(),
        _eff(),
        "CiReadTool",
        {},
        "read ci",
        event_bus=None,
    )
    assert ok is False
    assert msg == (
        "Permission denied: Tool requires ci:read, but session allows ci:banned"
    )


def test_ci_requirement_splits_on_first_colon_only():
    # "ci: read" (leading space after the colon) does NOT match a ci:read grant.
    eff = get_effective_permissions(
        SessionPermissions(ci="read"), WorkspaceCapabilities()
    )
    ok, msg = check_required_categories(
        ["ci: read"], eff, "CiReadTool", {}, "d", event_bus=None
    )
    assert ok is False
    assert msg == (
        "Permission denied: Tool requires ci: read, but session allows ci:read"
    )


# ---------------------------------------------------------------------------
# Egress posture: one request, exact URL, no credentials
# ---------------------------------------------------------------------------


def test_execute_branch_single_request_url_and_headers(monkeypatch):
    recorder = _install(monkeypatch)
    out = CiReadTool(branch="main", per_page=10).execute()

    # Exactly ONE network request.
    assert len(recorder.calls) == 1
    url, headers = recorder.calls[0]

    assert url == (
        "https://api.github.com/repos/octocat/hello-world/actions/runs"
        "?branch=main&per_page=10"
    )

    # No credential is ever sent.
    lower_keys = {k.lower() for k in headers}
    assert "authorization" not in lower_keys
    assert not any("token" in k.lower() for k in headers)
    assert headers.get("User-Agent") == "thoughtmachine-ci-read"
    assert headers.get("Accept") == "application/vnd.github+json"

    assert isinstance(out, str)


def test_execute_pr_single_request_url(monkeypatch):
    recorder = _install(monkeypatch)
    CiReadTool(pr=5).execute()

    assert len(recorder.calls) == 1
    url, headers = recorder.calls[0]
    assert url == (
        "https://api.github.com/repos/octocat/hello-world/actions/runs"
        "?per_page=20"
    )
    assert "authorization" not in {k.lower() for k in headers}


def test_execute_pr_filters_to_matching_pull_request(monkeypatch):
    payload = {
        "workflow_runs": [
            {"run_number": 1, "pull_requests": [{"number": 5}]},
            {"run_number": 2, "pull_requests": [{"number": 9}]},
        ]
    }
    recorder = _install(monkeypatch, payload)
    out = CiReadTool(pr=5).execute()

    assert len(recorder.calls) == 1
    assert "#1" in out
    assert "#2" not in out


# ---------------------------------------------------------------------------
# branch/pr XOR validation (no request issued on the error paths)
# ---------------------------------------------------------------------------


def test_execute_rejects_both_branch_and_pr(monkeypatch):
    recorder = _install(monkeypatch)
    out = CiReadTool(branch="main", pr=5).execute()

    assert "exactly one" in out
    assert recorder.calls == []


def test_execute_rejects_neither_branch_nor_pr(monkeypatch):
    recorder = _install(monkeypatch)
    out = CiReadTool().execute()

    assert "exactly one" in out
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Response handling
# ---------------------------------------------------------------------------


def test_execute_formats_successful_runs(monkeypatch):
    payload = {
        "workflow_runs": [
            {
                "name": "CI",
                "run_number": 42,
                "status": "completed",
                "conclusion": "success",
                "head_sha": "abcdef1234567890",
                "html_url": "https://github.com/octocat/hello-world/actions/runs/42",
            }
        ]
    }
    _install(monkeypatch, payload)
    out = CiReadTool(branch="main").execute()

    assert "octocat/hello-world" in out
    assert "#42 CI: completed/success" in out
    assert "https://github.com/octocat/hello-world/actions/runs/42" in out


def test_execute_reports_empty_runs(monkeypatch):
    _install(monkeypatch, {"workflow_runs": []})
    out = CiReadTool(branch="main").execute()
    assert "No CI workflow runs found" in out


def test_execute_wraps_http_error_as_string(monkeypatch):
    def _boom(url, headers):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(ci_read_tool, "_http_get", _boom)
    monkeypatch.setattr(
        CiReadTool, "_resolve_owner_repo", lambda self: _OWNER_REPO
    )
    out = CiReadTool(branch="main").execute()

    assert isinstance(out, str)
    assert out.startswith("Error executing ci_read:")


def test_execute_reports_valueerror_from_resolution(monkeypatch):
    def _no_origin(self):
        raise ValueError("could not determine the 'origin' remote URL")

    monkeypatch.setattr(CiReadTool, "_resolve_owner_repo", _no_origin)
    monkeypatch.setattr(
        ci_read_tool, "_http_get", _Recorder()
    )
    out = CiReadTool(branch="main").execute()

    assert out.startswith("Error: ")
    assert "origin" in out


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/octocat/hello-world", ("octocat", "hello-world")),
        ("https://github.com/octocat/hello-world.git", ("octocat", "hello-world")),
        ("http://host/owner/repo.git", ("owner", "repo")),
        ("ssh://git@github.com/octocat/hello-world.git", ("octocat", "hello-world")),
        ("git@github.com:octocat/hello-world.git", ("octocat", "hello-world")),
        ("git@github.com:octocat/hello-world", ("octocat", "hello-world")),
        # Extra leading path segments collapse to the last two components.
        ("https://gitlab.example.com/group/sub/repo.git", ("sub", "repo")),
        # Degenerate inputs -> None.
        ("", None),
        ("   ", None),
        ("https://github.com/onlyowner", None),
        (123, None),
        (None, None),
    ],
)
def test_parse_origin_owner_repo(url, expected):
    assert parse_origin_owner_repo(url) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (5, 5),
        (0, 1),
        (-3, 1),
        (1000, 100),
        (100, 100),
        (None, 20),
        ("abc", 20),
        ("7", 7),
    ],
)
def test_clamp_per_page(value, expected):
    assert CiReadTool._clamp_per_page(value) == expected


# ---------------------------------------------------------------------------
# Registration / preset posture
# ---------------------------------------------------------------------------


def test_ci_read_registered_as_real_tool():
    assert CiReadTool in TOOL_CLASSES


def test_ci_read_in_simplified_toolset():
    # CiReadTool MUST be present in SIMPLIFIED_TOOL_CLASSES: agent/config/models.py
    # builds the runtime tool set EXCLUSIVELY from SIMPLIFIED_TOOL_CLASSES
    # (models.py:302 `tool_classes = list(SIMPLIFIED_TOOL_CLASSES)`), so a class
    # only in TOOL_CLASSES could never be instantiated/executed in any mode --
    # excluding it here would be dead code.  NOTE: this same list is what the
    # default `enabled_tools` is derived from (see
    # test_ci_read_default_enablement_is_not_never below).  The default-off
    # guard is the ci:banned permission default, NOT the AGENT/ENGINEER presets.
    assert CiReadTool in SIMPLIFIED_TOOL_CLASSES


def test_ci_read_absent_from_default_presets():
    assert "ci_read" not in AGENT_TOOLS
    assert "ci_read" not in ENGINEER_TOOLS


def test_ci_read_default_enablement_is_not_never():
    # Pin the REAL posture (there is no "never enabled by default" property):
    # the default `enabled_tools` is derived from SIMPLIFIED_TOOL_CLASSES
    # (agent/config/models.py default_factory), which contains ci_read; the
    # AGENT/ENGINEER presets only omit it for those two modes.  The genuine
    # default-off guard is the ci:banned permission default (asserted at the
    # end), NOT the preset omission.
    #
    # We assert the derivation list directly rather than
    # ``AgentConfig(mode="custom").enabled_tools``: models.py binds
    # ``from tools import SIMPLIFIED_TOOL_CLASSES`` while ``tools/__init__``
    # REASSIGNS that global late, so under the pytest import order (models
    # reached via the agent.config.defaults -> agent.config.models -> tools
    # cycle) models can capture the pre-population [] and the custom-mode
    # default comes out empty.  That import-order fragility is a separate
    # latent issue; the robust, order-independent fact pinned here is that
    # ci_read IS part of the SIMPLIFIED_TOOL_CLASSES-derived default.
    from agent.config.models import AgentConfig

    derived_default = [cls.tool_name() for cls in SIMPLIFIED_TOOL_CLASSES]
    assert "ci_read" in derived_default

    # `mode` defaults to "agent", whose preset omits ci_read (a stock config).
    assert AgentConfig().mode == "agent"
    assert "ci_read" not in AgentConfig().enabled_tools
    assert "ci_read" not in AgentConfig(mode="engineer").enabled_tools

    # ...yet the read is still denied fail-closed until ci:read is granted.
    ok, _ = check_required_categories(
        CiReadTool.get_required_categories(),
        _eff(),
        "CiReadTool",
        {},
        "read ci",
        event_bus=None,
    )
    assert ok is False


def test_ci_read_in_canonical_all_tools():
    assert "ci_read" in _ALL_TOOLS


def test_ci_read_not_in_worker_blocklist():
    # Pin the INTENDED posture: ci_read is advertised to default workers.
    # _WORKER_BLOCKLIST is a frozenset of CLASS NAMES ("Worker", "CheckSystem",
    # "KnowledgeBaseTool") -- see tools/workspace/worker_thread.py:621 -- so the
    # key form is cls.__name__ (NOT tool_name()): the call sites are
    # `cls.__name__ not in _WORKER_BLOCKLIST` (lines 1624/1628/4031).
    from tools.workspace.worker_thread import _WORKER_BLOCKLIST

    # Class-name form: ci_read is NOT hard-blocked for workers.
    assert CiReadTool.__name__ not in _WORKER_BLOCKLIST

    # Mirror the default-worker derivation verbatim
    # (tools/workspace/worker_thread.py:1626-1628):
    #   from tools import SIMPLIFIED_TOOL_CLASSES
    #   enabled_tools = [cls.__name__ for cls in SIMPLIFIED_TOOL_CLASSES
    #                    if cls.__name__ not in _WORKER_BLOCKLIST]
    # -> ci_read is part of the tool set a default worker is advertised.
    default_worker_tools = [
        cls for cls in SIMPLIFIED_TOOL_CLASSES
        if cls.__name__ not in _WORKER_BLOCKLIST
    ]
    assert CiReadTool in default_worker_tools


def test_ci_read_openai_schema_name():
    schema = model_to_openai_tool(CiReadTool)
    assert schema["function"]["name"] == "ci_read"
