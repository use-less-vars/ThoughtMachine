"""
Workspace ceilings never fabricate interactive 'ask' grants (prompt gating).

The workspace permission ceiling (``workspace_permissions``) may cap a
session grant BELOW the session level, but a ceiling may never CREATE an
effective ``ask`` value: interactive prompting is reserved for genuine
session-level ``ask`` grants, which rank at the ceiling and pass through
unchanged.  An ``ask`` ceiling over a more-permissive session grant caps to
the most permissive tier below ask (``read`` where a read tier exists, else
``banned``) -- ``host_bash`` (no read tier) always caps to ``banned``, never
``ask``.

Mechanism under test: ``apply_workspace_ceiling`` (ceiling × session merge)
and ``check_required_categories`` with a ``NullEventBus`` (worker context) --
an effective ``ask`` requirement must resolve to a hard worker-context
denial ("ask requires interactive approval; not available in worker
context"), never to a blocking interactive prompt inside tests.

NOTE: the gate requirement for a fully-granted ``host_bash`` session value is
spelled ``host_bash:allow`` (exact match -- the host_bash scale has no
ranked ladder, so ``host_bash:execute`` has no level and is never satisfied
by an ``allow`` grant); ``host_bash:execute`` is used only as the display
spelling that an *ask* grant routes toward the prompt flow.

Full battery (this file + the neighbouring ceiling suites) is green:

    python -m pytest -q tests/security/test_ceiling_prompt_gating.py \
        tests/security/test_ceiling_resource_denial_messages.py \
        tests/security/test_ceiling_denial_messages.py \
        tests/security/test_security_gate.py tests/test_security_gate_disk.py \
        tests/test_workspace_permissions.py tests/test_tool_executor.py \
        tests/test_worker_disk_mode_inheritance.py \
        tests/test_host_bash_tool.py
"""

from __future__ import annotations

import json
import sys

# ── Fix sys.path for the Docker sandbox (same preamble as test_security_gate.py) ──
_bad_prefix = "/workspace/tests"
sys.path = [p for p in sys.path if not p.startswith(_bad_prefix)]
_stubs_path = "/tmp/stubs"
if _stubs_path in sys.path:
    sys.path.remove(_stubs_path)
if "/workspace" in sys.path:
    sys.path.remove("/workspace")
sys.path.insert(0, _stubs_path)
sys.path.insert(1, "/workspace")

from agent.events import NullEventBus
from security.security_gate import (
    apply_workspace_ceiling,
    check_required_categories,
    get_effective_permissions,
)
from thoughtmachine.security import SessionPermissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities

_PERMISSIVE_CAPS = WorkspaceCapabilities()  # fully permissive defaults


def _run_gate(required, effective, **kwargs):
    """Run a single-requirement gate check through the worker NullEventBus."""
    return check_required_categories(
        required,
        effective,
        tool_name="ProbeTool",
        tool_args={},
        description="probe",
        event_bus=NullEventBus(),
        **kwargs,
    )


# ──────────────────────────────────────────────────────────────────────────
# 1. Full-grant levels execute without any prompt
# ──────────────────────────────────────────────────────────────────────────
class TestAllowLevelExecutesWithoutPrompt:
    def test_host_bash_allow_grant_runs_without_prompt(self, monkeypatch):
        # A host-resource-enabled workspace is assumed: the orthogonal
        # allow_host_resources policy is stubbed True to isolate the
        # allow/ceiling mechanics under test.
        monkeypatch.setattr(
            "tools.host_resource_policy.workspace_allows_host_resources",
            lambda _ws: True,
        )
        # No ceiling -> plain dict on the common path. The requirement is
        # 'host_bash:allow' (exact match: host_bash has no ranked ladder, so
        # the display spelling 'host_bash:execute' would never be satisfied
        # by an allow grant -- the tool gates that spelling in-tool).
        eff = get_effective_permissions(
            SessionPermissions(host_bash="allow", network="write"),
            _PERMISSIVE_CAPS,
        )
        assert type(eff) is dict
        ok, msg = _run_gate(["host_bash:allow"], eff)
        assert ok is True
        assert msg == ""

    def test_network_write_grant_runs_without_prompt(self):
        eff = get_effective_permissions(
            SessionPermissions(host_bash="allow", network="write"),
            _PERMISSIVE_CAPS,
        )
        ok, msg = _run_gate(["network:write"], eff)
        assert ok is True
        assert msg == ""


# ──────────────────────────────────────────────────────────────────────────
# 2. Genuine session-level ask survives an equal ceiling, and worker context
#    denies it as a prompt-gated requirement (never blocks silently).
# ──────────────────────────────────────────────────────────────────────────
class TestGenuineAskPreservedAndWorkerDenied:
    def test_equal_ask_ceiling_passes_session_ask_through(self, monkeypatch):
        # Host-resource-enabled workspace assumed (stub the orthogonal
        # allow_host_resources policy) to isolate the ceiling mechanics.
        monkeypatch.setattr(
            "tools.host_resource_policy.workspace_allows_host_resources",
            lambda _ws: True,
        )
        assert apply_workspace_ceiling(
            {"host_bash": "ask"}, {"host_bash": "ask"}
        ) == {"host_bash": "ask"}

        eff = get_effective_permissions(
            SessionPermissions(host_bash="ask"),
            _PERMISSIVE_CAPS,
            {"host_bash": "ask"},
        )
        assert eff["host_bash"] == "ask"
        # Equal ceiling -> no restriction applied -> plain dict (no
        # _ceiling_annotations provenance to carry).
        assert type(eff) is dict

    def test_ask_grant_in_worker_context_denies_without_blocking(self, monkeypatch):
        # Host-resource-enabled workspace assumed (stub the orthogonal
        # allow_host_resources policy) to isolate the worker-context ask path.
        monkeypatch.setattr(
            "tools.host_resource_policy.workspace_allows_host_resources",
            lambda _ws: True,
        )
        eff = get_effective_permissions(
            SessionPermissions(host_bash="ask"),
            _PERMISSIVE_CAPS,
            {"host_bash": "ask"},
        )
        ok, msg = _run_gate(["host_bash:execute"], eff)
        assert ok is False
        assert (
            "ask requires interactive approval; not available in worker context"
            in msg
        )
        # The ask itself is legitimate: no hard "session allows banned" style
        # denial is fabricated by the ceiling merge.
        assert "session allows" not in msg


# ──────────────────────────────────────────────────────────────────────────
# 3. A banned ceiling denies outright, never routing into the prompt flow.
# ──────────────────────────────────────────────────────────────────────────
class TestBannedCeilingDeniesWithoutPrompt:
    def test_banned_ceiling_over_allow_grant_denies_directly(self):
        assert apply_workspace_ceiling(
            {"host_bash": "banned"}, {"host_bash": "allow"}
        ) == {"host_bash": "banned"}

        eff = get_effective_permissions(
            SessionPermissions(host_bash="allow"),
            _PERMISSIVE_CAPS,
            {"host_bash": "banned"},
        )
        assert eff["host_bash"] == "banned"

        ok, msg = _run_gate(["host_bash:allow"], eff)
        assert ok is False
        assert (
            "Session permission for host_bash is allow, but workspace ceiling "
            "is banned." in msg
        )
        # Hard denial — the ask/prompt branch must never be reached.
        assert "ask requires interactive" not in msg


# ──────────────────────────────────────────────────────────────────────────
# 4. An ask ceiling over a more-permissive grant never fabricates 'ask'
#    (interactive prompting stays reserved for genuine session-level ask).
# ──────────────────────────────────────────────────────────────────────────
class TestAskCeilingNeverFabricatesPrompt:
    def test_host_bash_ask_ceiling_caps_allow_grant_to_banned(self):
        # host_bash has no read tier: an ask ceiling over an allow grant
        # must cap to 'banned' -- never emit 'ask' (which would route every
        # host_bash call into the interactive prompt flow and effectively
        # RAISE the operator's ceiling).
        assert apply_workspace_ceiling(
            {"host_bash": "ask"}, {"host_bash": "allow"}
        ) == {"host_bash": "banned"}

    def test_network_ask_ceiling_caps_write_grant_to_banned(self):
        # Generic-scale analog: network has no read tier either, so an ask
        # ceiling over a write grant caps to 'banned'.
        assert apply_workspace_ceiling(
            {"network": "ask"}, {"network": "write"}
        ) == {"network": "banned"}

    def test_capped_allow_annotates_ask_ceiling_but_stays_banned(self):
        eff = get_effective_permissions(
            SessionPermissions(host_bash="allow"),
            _PERMISSIVE_CAPS,
            {"host_bash": "ask"},
        )
        assert eff["host_bash"] == "banned"
        ann = getattr(eff, "_ceiling_annotations", None)
        assert ann is not None
        # Provenance says the pre-ceiling grant was allow and the restricting
        # ceiling level was ask -- but the effective value is banned, so no
        # prompt can ever be fabricated from it.
        assert ann["host_bash"] == {"pre": "allow", "level": "ask"}
        ok, msg = _run_gate(["host_bash:allow"], eff)
        assert ok is False
        assert "ask requires interactive" not in msg
        assert (
            "Session permission for host_bash is allow, but workspace ceiling "
            "is ask." in msg
        )


# ──────────────────────────────────────────────────────────────────────────
# 5. A ceiling-caused denial message names the ceiling level.
# ──────────────────────────────────────────────────────────────────────────
class TestCeilingDenialMessageCarriesSuffix:
    def test_network_ceiling_caused_denial_names_ceiling(self):
        eff = get_effective_permissions(
            SessionPermissions(network="write"),
            _PERMISSIVE_CAPS,
            {"network": "ask"},
        )
        assert eff["network"] == "banned"
        assert getattr(eff, "_ceiling_annotations", {}).get("network") == {
            "pre": "write",
            "level": "ask",
        }

        ok, msg = _run_gate(["network:write"], eff)
        assert ok is False
        assert (
            "Session permission for network is write, but workspace ceiling "
            "is ask." in msg
        )
        assert json.loads(json.dumps(msg)) == msg  # JSON-safe string
