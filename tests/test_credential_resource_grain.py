"""RED-first tests for the ``credential`` session-permission resource grain.

These pin the full contract of the ``credential`` resource *before* it exists.
Nothing here is implementation-coupled beyond the seams the rest of the
permission system already exposes (``SessionPermissions`` -> ``RESOURCE_CATALOG``
-> ``get_effective_permissions`` -> ``check_required_categories``), so each
failure below is a missing-feature failure, never a collection error.

The contract:

1. ``credential`` is a canonical session resource on ``SessionPermissions``,
   defaulting to ``banned`` (deny-by-default, like every other grain).
2. ``credential: use`` permits a tool that injects credential *values*.
3. ``credential: read`` permits *metadata* (listing credential names) but NOT
   retrieval/use of an actual secret value -- the read/use split is the whole
   point of a non-boolean grain.  The denial must use the standard
   ``check_required_categories`` wording so operators see ONE message format.
4. An unreadable on-disk permission store fails CLOSED to ``banned`` for
   ``credential``, exactly like the other resources (both sentinels:
   ``_DISK_FAIL_CLOSED_SESSION`` and ``_DISK_FAIL_CLOSED_CEILING``).
5. Driving the REAL executor path (``ToolExecutor._execute_single_tool`` with a
   ``{{credential:<name>}}`` placeholder in the tool args) emits exactly ONE
   ``CREDENTIAL_USE`` audit event carrying the credential NAME (never the
   value), the tool name and the session id -- the serialized payload must not
   contain the raw secret string.
6. The workspace ``credential`` ceiling is NOT inert: a workspace ceiling below
   the session grant actually caps it, and a ceiling above the session grant
   never widens it (regression for the ``_WORKSPACE_RESOURCE_MAP`` omission,
   which makes ``apply_workspace_ceiling`` silently skip the key).
7. The frontend vocab mirror (``web_ui/frontend/src/data/permissionVocab.js``)
   declares ``credential`` as a session resource and knows the ``use`` level, so
   the UI can render and set it.
8. The ``use`` rank is identical in ``GRANT_LEVEL_RANKS`` and
   ``WORKSPACE_CEILING_LEVELS_RANKS`` -- a session grant and a workspace ceiling
   must rank the same level the same way or the cap comparison is meaningless.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Pytest prepends the test directory to ``sys.path``; make the repo root win so
# ``import security`` resolves to ``<repo>/security`` (not ``tests/security``).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. The grain exists and is deny-by-default
# ---------------------------------------------------------------------------


def test_credential_grain_defaults_to_banned():
    """``credential`` is declared on ``SessionPermissions`` and defaults banned.

    Deny-by-default is the invariant every other grain obeys; a session that
    never mentions credentials must not be able to inject one.
    """
    from thoughtmachine.security import SessionPermissions

    perms = SessionPermissions()
    assert perms.credential == "banned"
    assert "credential" in type(perms).model_fields


# ---------------------------------------------------------------------------
# 2./3. ``use`` vs ``read`` -- the two-level split
# ---------------------------------------------------------------------------


def test_credential_use_allows_injection():
    """``credential: use`` grants a tool that injects credential values."""
    from thoughtmachine.security import SessionPermissions
    from thoughtmachine.workspace_capabilities import WorkspaceCapabilities
    from security.security_gate import (
        check_required_categories,
        get_effective_permissions,
    )

    assert "credential" in SessionPermissions.model_fields

    eff = get_effective_permissions(
        SessionPermissions(credential="use"), WorkspaceCapabilities()
    )
    assert eff["credential"] == "use"

    ok, msg = check_required_categories(
        ["credential:use"],
        eff,
        "DummyCredentialInjectTool",
        {},
        "inject credential",
        event_bus=None,
    )
    assert (ok, msg) == (True, "")


def test_credential_read_allows_metadata_but_not_value():
    """``credential: read`` lists NAMES but cannot use the VALUE.

    Metadata (``credential:read``) is allowed; value retrieval/use
    (``credential:use``) must be denied with the standard denial string.
    """
    from thoughtmachine.security import SessionPermissions
    from thoughtmachine.workspace_capabilities import WorkspaceCapabilities
    from security.security_gate import (
        check_required_categories,
        get_effective_permissions,
    )

    assert "credential" in SessionPermissions.model_fields

    eff = get_effective_permissions(
        SessionPermissions(credential="read"), WorkspaceCapabilities()
    )
    assert eff["credential"] == "read"

    ok, msg = check_required_categories(
        ["credential:read"],
        eff,
        "DummyCredentialListTool",
        {},
        "list credential names",
        event_bus=None,
    )
    assert (ok, msg) == (True, "")

    ok, msg = check_required_categories(
        ["credential:use"],
        eff,
        "DummyCredentialInjectTool",
        {},
        "use credential value",
        event_bus=None,
    )
    assert ok is False
    assert msg == (
        "Permission denied: Tool requires credential:use, "
        "but session allows credential:read"
    )


# ---------------------------------------------------------------------------
# 4. Disk-read failure fails CLOSED to ``banned``
# ---------------------------------------------------------------------------


def test_credential_disk_read_failure_fails_closed_to_banned():
    """Both fail-closed sentinels ban ``credential``.

    An unreadable session/workspace permission store must resolve to the
    most-restrictive value for ``credential`` too -- otherwise a corrupt vault
    silently *grants* credential access, which is the worst possible direction
    for this resource.
    """
    from security import security_gate as sg
    from security.security_gate import apply_workspace_ceiling

    session_sentinel = sg._DISK_FAIL_CLOSED_SESSION.model_dump()
    assert "credential" in session_sentinel
    assert session_sentinel["credential"] == "banned"

    assert sg._DISK_FAIL_CLOSED_CEILING["credential"] == "banned"

    # Behavioural: as a ceiling over a permissive grant it must cap to banned.
    capped = apply_workspace_ceiling(
        sg._DISK_FAIL_CLOSED_CEILING, {"credential": "use"}
    )
    assert capped["credential"] == "banned"


# ---------------------------------------------------------------------------
# 5. Audit event: name + tool + session, never the value
# ---------------------------------------------------------------------------


def test_credential_use_emits_event_with_name_only(tmp_path, monkeypatch):
    """``_execute_single_tool`` emits exactly ONE ``CREDENTIAL_USE`` event.

    The real injection seam is the executor: it resolves the placeholders and
    then must report WHAT was used (credential name), WHO used it (tool name)
    and in WHICH session -- and must never carry the secret itself.  Note
    ``Secret`` only redacts ``repr``/``str``/``format``, so ``json.dumps`` of a
    payload holding the raw value (or a pydantic-coerced plain ``str`` copy of
    it) would leak the secret.
    """
    from typing import ClassVar, List

    import agent.credentials.injector as injector_mod
    from agent.core.tool_executor import ToolExecutor
    from agent.events import EventBus, EventType
    from tools.base import ToolBase

    assert hasattr(
        EventType, "CREDENTIAL_USE"
    ), "agent.events.EventType has no CREDENTIAL_USE member"

    # Hermetic vault: the injector resolves through ``vault_root()``.
    monkeypatch.setattr(injector_mod, "vault_root", lambda: tmp_path)
    scope = tmp_path / "credentials" / "ws-audit"
    scope.mkdir(parents=True)
    raw_value = "planted-value-must-not-be-logged"
    (scope / "demo_key").write_text(raw_value + "\n", encoding="utf-8")

    class CredentialEchoTool(ToolBase):
        """Minimal tool that declares the args the injector rewrites."""

        tool: str = "CredentialEchoTool"
        required_categories: ClassVar[List[str]] = []
        workspace_id: str = ""
        api_key: str = ""

        def execute(self) -> str:
            # The value reaching the tool proves the injection ran for real.
            return "injected" if self.api_key == raw_value else "unresolved"

    class FakeConfig:
        workspace_path = None
        tool_output_token_limit = None
        session_permissions = None

    class FakeState:
        security_config = None

    bus = EventBus()
    captured = []
    bus.subscribe(None, lambda event: captured.append(event))

    executor = ToolExecutor(
        tool_classes=[CredentialEchoTool],
        config=FakeConfig(),
        state=FakeState(),
        logger=None,
        security_available=False,
        agent=None,
        event_bus=bus,
    )
    result = executor._execute_single_tool(
        CredentialEchoTool,
        {"workspace_id": "ws-audit", "api_key": "{{credential:demo_key}}"},
        "CredentialEchoTool",
        0,
        lambda: False,
        lambda: None,
        lambda: 0,
        session_id="sess-audit-1",
        workspace_id="ws-audit",
    )

    # Injection still worked (the placeholder was resolved before dispatch).
    assert result["result"] == "injected"

    events = [
        event
        for event in captured
        if getattr(event, "type", None) == EventType.CREDENTIAL_USE
    ]
    assert len(events) == 1

    data = events[0].data
    assert data["credential_name"] == "demo_key"
    assert data["tool_name"] == "CredentialEchoTool"
    assert data["session_id"] == "sess-audit-1"

    serialized = json.dumps(data, default=str)
    assert raw_value not in serialized


# ---------------------------------------------------------------------------
# 6. The workspace ceiling is not inert
# ---------------------------------------------------------------------------


def test_credential_workspace_ceiling_is_not_inert():
    """A workspace ``credential`` ceiling actually caps the session grant.

    ``apply_workspace_ceiling`` silently ``continue``s for a resource missing
    from ``_WORKSPACE_RESOURCE_MAP`` -- i.e. the ceiling is inert and a
    workspace-restricted session keeps a wider grant than its workspace allows.
    """
    from security import security_gate as sg
    from security.security_gate import apply_workspace_ceiling

    assert "credential" in sg._WORKSPACE_RESOURCE_MAP

    # Ceiling BELOW the session grant: the cap must bite (an unregistered
    # resource would leave ``use`` untouched here -- that is the regression).
    capped = apply_workspace_ceiling({"credential": "read"}, {"credential": "use"})
    assert capped["credential"] == "read"

    # Ceiling ABOVE the session grant: a ceiling may never widen a grant.
    not_widened = apply_workspace_ceiling(
        {"credential": "use"}, {"credential": "read"}
    )
    assert not_widened["credential"] == "read"


# ---------------------------------------------------------------------------
# 7./8. Vocabulary parity across the two rank tables and the frontend mirror
# ---------------------------------------------------------------------------


def test_permission_vocab_includes_credential():
    """The frontend vocab mirror declares ``credential`` and the ``use`` level."""
    vocab = (
        REPO_ROOT / "web_ui" / "frontend" / "src" / "data" / "permissionVocab.js"
    ).read_text(encoding="utf-8")

    session_start = vocab.index("export const SESSION_RESOURCE_VOCAB")
    session_block = vocab[session_start : vocab.index("};", session_start)]
    assert "credential" in session_block
    assert "use" in session_block

    rank_start = vocab.index("export const PERMISSION_RANK_ORDER")
    rank_block = vocab[rank_start : vocab.index("};", rank_start)]
    assert "use" in rank_block


def test_credential_use_rank_agrees_across_paths():
    """``use`` ranks identically as a session grant and as a workspace ceiling."""
    from security.resource_catalog import (
        GRANT_LEVEL_RANKS,
        WORKSPACE_CEILING_LEVELS_RANKS,
    )

    assert "use" in GRANT_LEVEL_RANKS
    assert "use" in WORKSPACE_CEILING_LEVELS_RANKS
    assert GRANT_LEVEL_RANKS["use"] == WORKSPACE_CEILING_LEVELS_RANKS["use"]
